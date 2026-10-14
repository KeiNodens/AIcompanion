"""adapters.migration —— L-MIG の工程台帳・突合・G1 同等性ゲート。

deliver_state: 移行元の行ごとの配送状況（import_key 冪等）。
reconciler: 移行元目録 ↔ deliver_state ↔ canonical の3面突合。
EquivGate: 旧系・新系への代表クエリ応答の構造的等価判定（Q-O18 具体化）。
"""

import logging
from datetime import datetime, timezone

from ..kernel import canon, migration as mig

log = logging.getLogger("companion")


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


class DeliverState:
    """deliver_state 表の台帳口（writer.transaction 経由のみ）。"""

    def __init__(self, writer, *, clock=None):
        self.writer = writer
        self._clock = clock or _now

    def mark(self, conn, import_key, source_table, source_id, target,
             *, target_id=None, status, detail=None, run_id=None):
        """行の配送状態を upsert（同一 import_key の再実行は上書きしない
        —— delivered 済みを staged へ戻す等の退行は呼び出し側が制御）。"""
        now = self._clock()
        conn.execute(
            "INSERT INTO deliver_state(import_key, source_table, source_id,"
            " target, target_id, status, detail_json, run_id, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(import_key) DO UPDATE SET"
            " target=excluded.target, target_id=excluded.target_id,"
            " status=excluded.status, detail_json=excluded.detail_json,"
            " run_id=excluded.run_id, updated_at=excluded.updated_at",
            (import_key, source_table, str(source_id), target, target_id,
             status, canon.dumps(detail) if detail is not None else None,
             run_id, now, now))

    def get(self, conn, import_key):
        r = conn.execute(
            "SELECT * FROM deliver_state WHERE import_key=?",
            (import_key,)).fetchone()
        return dict(r) if r else None

    def counts(self, conn, run_id=None):
        sql = "SELECT status, COUNT(*) c FROM deliver_state"
        args = ()
        if run_id:
            sql += " WHERE run_id=?"
            args = (run_id,)
        sql += " GROUP BY status"
        return {r["status"]: r["c"] for r in conn.execute(sql, args)}

    def rows(self, conn, *, source_table=None, status=None, run_id=None,
             limit=None):
        sql, args = "SELECT * FROM deliver_state WHERE 1=1", []
        if source_table:
            sql += " AND source_table=?"; args.append(source_table)
        if status:
            sql += " AND status=?"; args.append(status)
        if run_id:
            sql += " AND run_id=?"; args.append(run_id)
        sql += " ORDER BY created_at, import_key"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in conn.execute(sql, args)]


class Reconciler:
    """移行元目録・deliver_state・canonical 表の3面突合。

    source_inventory: [{'source_table','source_id','import_key','expect'}]
    の列を受け、deliver_state・migration_map・records の実在を照合する。
    """

    def __init__(self, writer):
        self.writer = writer

    def reconcile(self, inventory: list) -> dict:
        out = {"delivered": [], "staged": [], "missing": [],
               "quarantined": [], "skipped": [], "failed": [],
               "no_target_row": [], "total": len(inventory)}
        with self.writer.transaction() as conn:
            for row in inventory:
                ikey = row["import_key"]
                ds = conn.execute(
                    "SELECT * FROM deliver_state WHERE import_key=?",
                    (ikey,)).fetchone()
                entry = {"import_key": ikey,
                         "source_table": row["source_table"],
                         "source_id": row["source_id"]}
                if ds is None:
                    out["missing"].append(entry)
                    continue
                st = ds["status"]
                if st == "delivered":
                    tgt = ds["target"]
                    if tgt == "events":
                        # 移行 event の実在は ingress_key 前置一致で確認
                        # （mig:<ikey>・mig:<ikey>:u 等の part 形を含む）
                        ev = conn.execute(
                            "SELECT COUNT(*) c FROM events"
                            " WHERE ingress_key LIKE 'mig:' || ? || '%'",
                            (ikey,)).fetchone()["c"]
                        if ev == 0:
                            out["no_target_row"].append(
                                {**entry, "issue": "event not found"})
                            continue
                        out["delivered"].append({**entry, "events": ev})
                        continue
                    if tgt == "blob":
                        # blob_register event の実在は payload.hash と
                        # target_id の一致で見る（ingress_key は刻印時の
                        # キー体系に依存するため照合面にしない）
                        ev = conn.execute(
                            "SELECT COUNT(*) c FROM events"
                            " WHERE event_type='blob_register'"
                            " AND json_extract(payload_json,'$.hash')=?",
                            (ds["target_id"],)).fetchone()["c"]
                        if ev == 0:
                            out["no_target_row"].append(
                                {**entry, "issue": "blob_register missing"})
                            continue
                        out["delivered"].append(
                            {**entry, "blob": ds["target_id"]})
                        continue
                    # records 系: migration_map・target 表の両方を見る
                    mm = conn.execute(
                        "SELECT record_id, scope, seq FROM migration_map"
                        " WHERE import_key=?", (ikey,)).fetchone()
                    if mm is None:
                        out["no_target_row"].append(
                            {**entry, "issue": "migration_map missing"})
                        continue
                    out["delivered"].append(
                        {**entry, "record_id": mm["record_id"],
                         "scope": mm["scope"], "seq": mm["seq"]})
                elif st == "staged":
                    out["staged"].append(entry)
                elif st == "quarantined":
                    out["quarantined"].append(entry)
                elif st == "skipped":
                    out["skipped"].append(entry)
                elif st == "failed":
                    out["failed"].append(entry)
                else:
                    out["missing"].append(
                        {**entry, "issue": f"unknown status {st}"})
        out["ok"] = not (out["missing"] or out["failed"]
                         or out["no_target_row"])
        out["counts"] = {k: len(v) for k, v in out.items()
                         if isinstance(v, list)}
        return out


class EquivGate:
    """G1 相当の意味的同等性ゲート（Q-O18 の具体化）。

    代表クエリ集合を旧系 recall_fn・新系 supply_fn の両方に投げ、
    「同じ記憶が引けるか」の構造的等価を判定する。
    - queries: [{'id','text','must_include':[...],'scope':'user'}]
    - old_recall(query) -> [item dicts]（mem0 側）
    - new_recall(query) -> [record dicts]（canonical 側）
    合格基準（実装時既定、Q-O18 初期値）:
      - 各クエリ coverage ≥ coverage_floor（既定 1.0: 旧系 item は全件
        新系で引けること）
      - must_include 指定の item は被覆必須（coverage_floor より優先）
      - 全クエリのうち fail がゼロで pass
    """

    def __init__(self, *, coverage_floor=1.0, sim_threshold=0.8):
        self.coverage_floor = coverage_floor
        self.sim_threshold = sim_threshold

    def evaluate(self, queries, old_recall, new_recall) -> dict:
        results = []
        for q in queries:
            old_items = list(old_recall(q) or [])
            new_items = list(new_recall(q) or [])
            cov = mig.equiv_coverage(
                old_items, new_items, sim_threshold=self.sim_threshold)
            must = q.get("must_include") or []
            must_missing = []
            for mi in must:
                probe = {"id": mi.get("id"),
                         "memory": mi.get("memory") or mi.get("content")}
                if not any(mig.equiv_match(
                        probe, ni, sim_threshold=self.sim_threshold)
                        for ni in new_items):
                    must_missing.append(probe["id"] or probe["memory"])
            ok = (cov["coverage"] >= self.coverage_floor
                  and not must_missing)
            results.append({
                "query_id": q.get("id") or q.get("text"),
                "ok": ok, "coverage": cov["coverage"],
                "total": cov["total"], "covered": cov["covered"],
                "missed": cov["missed"],
                "must_missing": must_missing,
            })
        fails = [r for r in results if not r["ok"]]
        return {"ok": not fails, "results": results,
                "failed": len(fails), "total": len(results),
                "coverage_floor": self.coverage_floor,
                "sim_threshold": self.sim_threshold}
