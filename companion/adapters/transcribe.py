"""adapters.transcribe —— L-MIG Phase 2 の staged transcription。

移行元資産 → canonical 形式への写し変え（D-H1）。LLM 再抽出はしない
（「同じものが移ったか」の検証を成立させるため）。工程は:

  inventory（adapter 呼び出し側が集めた source 行の正規化）
    → stage（staged_events/staged_records/staged_blobs＋deliver_state）
    → commit（import_key 冪等で canonical へ移入＋migration_map 記録）

検証（migration-m5 §5.1）は adapters.migverify が担う。
"""

import logging
from datetime import datetime, timezone

from ..kernel import canon, ids, migration as mig
from .migration import DeliverState

log = logging.getLogger("companion")


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


# ------------------------------------------------------------ inventory
class Transcriber:
    """移行元資産の staged transcription + commit を司る。"""

    def __init__(self, writer, *, clock=None):
        self.writer = writer
        self._clock = clock or _now
        self.deliver = DeliverState(writer, clock=self._clock)

    # ---------------------------------------------------- 目録の正規化
    def build_inventory(self, *, mem0_items=(), history_rows=(),
                        turn_rows=(), files=(), db_rows=None,
                        state_values=None, blobs=(), message_rows=()
                        ) -> list:
        """source 行を {source_table, source_id, import_key, expect, row}
        の正規形に畳む。写像表外の source は skip 扱い（安全側）。"""
        inv = []

        def add(source_table, source_id, row):
            inv.append({
                "source_table": source_table,
                "source_id": str(source_id),
                "import_key": mig.import_key(source_table, source_id),
                "expect": mig.source_kind(source_table),
                "row": row,
            })

        for it in mem0_items:
            scope = mig.mem0_scope(it)
            add(f"mem0:{scope}", it.get("id"), it)
        for r in history_rows:
            add("history", r.get("id") or r.get("memory_id"), r)
        for r in message_rows:
            add("messages", r.get("id"), r)
        for r in turn_rows:
            add("turns", r.get("id"), r)
        if isinstance(files, dict):
            files = [{"name": k, "content": v}
                     for k, v in files.items() if v is not None]
        for f in files:
            add(f"file:{f['name']}", f["name"], f)
        for table, rows in (db_rows or {}).items():
            for r in rows:
                sid = r.get("id")
                if sid is None:
                    sid = canon.hash_tuple(
                        "pcore.src_row.v1", table,
                        canon.dumps(r, ensure_ascii=False))[:16]
                add(table, sid, r)
        for k, v in (state_values or {}).items():
            add(f"state:{k}", k, v)
        for b in blobs:
            add(b.get("source_table") or "docs",
                b.get("path") or b.get("hash"), b)
        return inv

    # ------------------------------------------------------------- stage
    def stage(self, inventory, run_id: str) -> dict:
        """inventory → staged_*＋deliver_state(staged|skipped|quarantined)。"""
        now = self._clock()
        counts = {"staged_events": 0, "staged_records": 0,
                  "staged_blobs": 0, "skipped": 0, "quarantined": 0}
        with self.writer.transaction() as conn:
            ev_i = 0
            for row in inventory:
                ikey = row["import_key"]
                ex = row["expect"]
                kind = ex["kind"]
                try:
                    if kind == "record":
                        st = self._record_state(row, now)
                        conn.execute(
                            "INSERT INTO staged_records(run_id, table_name,"
                            " scope, record_id, row_json, import_key,"
                            " status) VALUES (?,?,?,?,?,?, 'staged')"
                            " ON CONFLICT(run_id, table_name, scope,"
                            " record_id) DO UPDATE SET"
                            " row_json=excluded.row_json,"
                            " import_key=excluded.import_key,"
                            " status='staged'",
                            (run_id, "records", st["scope"],
                             st["record_id"], canon.dumps(st), ikey))
                        counts["staged_records"] += 1
                        self.deliver.mark(
                            conn, ikey, row["source_table"], row["source_id"],
                            "records", target_id=st["record_id"],
                            status="staged", run_id=run_id)
                    elif kind == "record+event" or kind == "event":
                        evs = self._event_descs(row, now)
                        for ev in evs:
                            ev_i += 1
                            conn.execute(
                                "INSERT INTO staged_events(run_id, seq,"
                                " event_id, event_type, row_json, status)"
                                " VALUES (?,?,?,?,?, 'staged')"
                                " ON CONFLICT(run_id, seq) DO UPDATE SET"
                                " event_id=excluded.event_id,"
                                " event_type=excluded.event_type,"
                                " row_json=excluded.row_json,"
                                " status='staged'",
                                (run_id, ev_i,
                                 ev.pop("_event_id", None) or
                                 ids.record_id(
                                     "evt", canon.hash_bytes(
                                         "pcore.staged_event.v1",
                                         f"{run_id}:{ev_i}".encode())[:24]),
                                 ev["event_type"], canon.dumps(ev)))
                            counts["staged_events"] += 1
                        if kind == "record+event":
                            st = self._record_state(row, now)
                            conn.execute(
                                "INSERT INTO staged_records(run_id,"
                                " table_name, scope, record_id, row_json,"
                                " import_key, status) VALUES (?,?,?,?,?,?"
                                ", 'staged') ON CONFLICT(run_id,"
                                " table_name, scope, record_id) DO UPDATE"
                                " SET row_json=excluded.row_json,"
                                " status='staged'",
                                (run_id, "records", st["scope"],
                                 st["record_id"], canon.dumps(st), ikey))
                            counts["staged_records"] += 1
                        self.deliver.mark(
                            conn, ikey, row["source_table"], row["source_id"],
                            "records" if kind == "record+event" else "events",
                            target_id=(st["record_id"]
                                       if kind == "record+event" else None),
                            status="staged", run_id=run_id)
                    elif kind == "blob":
                        b = row["row"]
                        conn.execute(
                            "INSERT INTO staged_blobs(run_id, hash,"
                            " pack_path, size, status) VALUES (?,?,?,?,"
                            " 'staged') ON CONFLICT(run_id, hash) DO"
                            " UPDATE SET pack_path=excluded.pack_path,"
                            " status='staged'",
                            (run_id, b.get("hash") or "",
                             b.get("path"), int(b.get("size") or 0)))
                        counts["staged_blobs"] += 1
                        self.deliver.mark(
                            conn, ikey, row["source_table"], row["source_id"],
                            "blob", target_id=b.get("hash"),
                            status="staged", run_id=run_id)
                    else:
                        counts["skipped"] += 1
                        self.deliver.mark(
                            conn, ikey, row["source_table"], row["source_id"],
                            "skip", status="skipped",
                            detail={"kind": kind}, run_id=run_id)
                except Exception as e:
                    counts["quarantined"] += 1
                    self.deliver.mark(
                        conn, ikey, row["source_table"], row["source_id"],
                        "quarantine", status="quarantined",
                        detail={"error": str(e)[:300]}, run_id=run_id)
                    log.warning("transcribe quarantine %s: %s", ikey, e)
        return counts

    # ------------------------------------------------------------ commit
    def commit(self, run_id: str) -> dict:
        """staged → canonical。import_key/(actor,ingress_key) 冪等。

        - staged_events: 記述子どおりに append（migration 打刻＋
          ingress_key=import_key|part で二重化防止）
        - staged_records: apply_mutation(record_write) で正式移入
        - staged_blobs: blob_register イベント＋blobs 行（実体の写しは
          呼び出し側の blob mover が担う。ここでは参照の確定のみ）
        - migration_map に import_key→record 対応を記録
        - deliver_state を delivered へ。失敗分は failed で残す
        """
        now = self._clock()
        res = {"events": 0, "records": 0, "blobs": 0, "dedup": 0,
               "failed": [], "quarantined": 0}
        with self.writer.transaction() as conn:
            for r in conn.execute(
                    "SELECT * FROM staged_events WHERE run_id=?"
                    " AND status='staged' ORDER BY seq",
                    (run_id,)).fetchall():
                desc = canon.loads(r["row_json"])
                ikey = desc.get("import_key")
                try:
                    rec = self.writer._append_locked(
                        desc["event_type"], desc.get("payload") or {},
                        actor=desc.get("actor") or "system",
                        occurred_at=desc.get("occurred_at"),
                        session=desc.get("session"),
                        correlation=desc.get("correlation"),
                        source_meta=desc.get("source_meta"),
                        ingress_key=desc.get("ingress_key"),
                        migration=desc.get("migration"))
                    conn.execute(
                        "UPDATE staged_events SET status='committed'"
                        " WHERE run_id=? AND seq=?", (run_id, r["seq"]))
                    if rec.get("dedup"):
                        res["dedup"] += 1
                    else:
                        res["events"] += 1
                except Exception as e:
                    res["failed"].append(
                        {"kind": "event", "seq": r["seq"],
                         "import_key": ikey, "error": str(e)[:200]})
                    conn.execute(
                        "UPDATE staged_events SET status='failed'"
                        " WHERE run_id=? AND seq=?", (run_id, r["seq"]))
                    self._fail(conn, ikey, e, run_id)

            # event 系行の deliver_state: 同一 import_key の全 part が
            # committed になったときだけ delivered へ（一部失敗なら据置き）
            for r in conn.execute(
                    "SELECT DISTINCT json_extract(row_json,'$.import_key') ik"
                    " FROM staged_events WHERE run_id=? AND status='committed'",
                    (run_id,)).fetchall():
                ik = r["ik"]
                if not ik:
                    continue
                pending = conn.execute(
                    "SELECT COUNT(*) c FROM staged_events WHERE run_id=?"
                    " AND json_extract(row_json,'$.import_key')=?"
                    " AND status IN ('staged','failed')",
                    (run_id, ik)).fetchone()["c"]
                if pending == 0:
                    self._mark_delivered(conn, ik, run_id, "events", None)

            for r in conn.execute(
                    "SELECT * FROM staged_records WHERE run_id=?"
                    " AND status='staged' ORDER BY scope, record_id",
                    (run_id,)).fetchall():
                st = canon.loads(r["row_json"])
                ikey = r["import_key"]
                try:
                    fam = st["record_family"]
                    payload = ({"memory_id": st["record_id"],
                                "new_state": st,
                                "reason": mig.SEED_REASON,
                                "basis_events": []}
                               if fam == "memory" else
                               {"record_family": fam,
                                "record_id": st["record_id"],
                                "new_state": st,
                                "reason": mig.SEED_REASON,
                                "basis_events": []})
                    et = "memory_write" if fam == "memory" else "record_write"
                    rec = self.writer.apply_mutation(
                        et, payload, actor="system",
                        ingress_key=f"mig:{ikey}",
                        occurred_at=st.get("created_at"),
                        source_meta={"import_key": ikey,
                                     "run_id": run_id,
                                     "migrated": True})
                    conn.execute(
                        "UPDATE staged_records SET status='committed'"
                        " WHERE run_id=? AND table_name=? AND scope=?"
                        " AND record_id=?",
                        (run_id, r["table_name"], r["scope"],
                         r["record_id"]))
                    conn.execute(
                        "INSERT INTO migration_map(import_key,"
                        " source_table, source_id, record_id, scope, seq,"
                        " created_at) VALUES (?,?,?,?,?,?,?)"
                        " ON CONFLICT(import_key) DO NOTHING",
                        (ikey, self._ds_get(conn, ikey, "source_table"),
                         self._ds_get(conn, ikey, "source_id"),
                         st["record_id"], st["scope"], rec["seq"], now))
                    if rec.get("dedup"):
                        res["dedup"] += 1
                    else:
                        res["records"] += 1
                    self._mark_delivered(conn, ikey, run_id,
                                         "records", st["record_id"])
                except Exception as e:
                    res["failed"].append(
                        {"kind": "record", "record_id": r["record_id"],
                         "import_key": ikey, "error": str(e)[:200]})
                    conn.execute(
                        "UPDATE staged_records SET status='failed'"
                        " WHERE run_id=? AND table_name=? AND scope=?"
                        " AND record_id=?",
                        (run_id, r["table_name"], r["scope"],
                         r["record_id"]))
                    self._fail(conn, ikey, e, run_id)

            for r in conn.execute(
                    "SELECT * FROM staged_blobs WHERE run_id=?"
                    " AND status='staged'", (run_id,)).fetchall():
                # deliver_state の行は stage 時の source 由来 import_key
                # で作られている —— staged_blobs には列が無いので
                # target_id=hash で引き戻す（再計算はキーがずれる）
                row = conn.execute(
                    "SELECT import_key FROM deliver_state WHERE run_id=?"
                    " AND target='blob' AND target_id=?",
                    (run_id, r["hash"])).fetchone()
                ikey = (row["import_key"] if row else
                        mig.import_key("blobs", r["hash"]))
                try:
                    self.writer.append_event(
                        "blob_register",
                        {"hash": r["hash"], "ref": r["pack_path"],
                         "size": r["size"], "import_key": ikey},
                        actor="system",
                        ingress_key=f"migblob:{ikey}",
                        migration={"source": "blob",
                                   "reason": mig.SEED_REASON})
                    conn.execute(
                        "UPDATE staged_blobs SET status='committed'"
                        " WHERE run_id=? AND hash=?",
                        (run_id, r["hash"]))
                    res["blobs"] += 1
                    self._mark_delivered(conn, ikey, run_id,
                                         "blob", r["hash"])
                except Exception as e:
                    res["failed"].append(
                        {"kind": "blob", "hash": r["hash"],
                         "error": str(e)[:200]})
                    conn.execute(
                        "UPDATE staged_blobs SET status='failed'"
                        " WHERE run_id=? AND hash=?",
                        (run_id, r["hash"]))
                    self._fail(conn, ikey, e, run_id)

            qu = conn.execute(
                "SELECT COUNT(*) c FROM deliver_state WHERE run_id=?"
                " AND status='quarantined'", (run_id,)).fetchone()["c"]
            res["quarantined"] = qu
            self.writer._append_locked(
                "migration",
                {"action": "transcribe_commit", "run_id": run_id,
                 "counts": {k: v for k, v in res.items()
                            if isinstance(v, int)},
                 "failed": len(res["failed"])},
                actor="system")
        return res

    # ------------------------------------------------- state/desc builders
    def _record_state(self, row: dict, now: str) -> dict:
        """source 行 → record の完全スナップショット。"""
        src, data, ex, ikey = (row["source_table"], row["row"],
                               row["expect"], row["import_key"])
        if src.startswith("mem0:"):
            return mig.mem0_item_to_state(
                data, ex["scope"], ikey, created_now=now)
        fam = ex["family"]
        base = {
            "record_family": fam,
            "record_id": mig.migrated_record_id(ikey, fam[:4]),
            "scope": ex["scope"],
            "status": "active",
            "record_version": 1,
            "import_key": ikey,
            "provenance": {
                "evidence_basis": "migrated_unknown",
                "provenance_gap": "structural",
                "generated_by": mig.PRODUCER,
                "source": {"system": "companion.db", "table": src,
                           "row_id": row["source_id"]},
            },
            "created_at": str(data.get("created_at") or now)
            if isinstance(data, dict) else now,
            "updated_at": now,
        }
        if fam == "identity":
            base["identity_kind"] = ex["identity_kind"]
            base["content"] = data.get("content") or ""
            base["content_hash"] = canon.content_hash(base["content"])
        elif fam == "task":
            base.update(title=data.get("title"), due=data.get("due"),
                        note=data.get("note"),
                        status="completed" if data.get("done") else "active")
        elif fam == "list":
            base.update(list_name=data.get("list_name"),
                        item=data.get("item"), note=data.get("note"))
        elif fam == "reminder":
            base.update(text=data.get("text"), due_at=data.get("due_at"),
                        channel_id=data.get("channel_id"),
                        status=("fired" if data.get("fired") else "active"))
        elif fam == "approval":
            base.update(job_id=data.get("job_id"),
                        content_hash=data.get("content_hash"),
                        requester=data.get("requester"),
                        decided_by=data.get("decided_by"),
                        decided_at=data.get("decided_at"),
                        expires_at=data.get("expires_at"),
                        status=data.get("status") or "pending")
        elif fam == "job":
            base.update(job_kind="delegate", request=data.get("request"),
                        prompt=data.get("prompt"),
                        workdir=data.get("workdir"), model=data.get("model"),
                        effort=data.get("effort"),
                        exit_code=data.get("exit_code"),
                        started_at=data.get("started_at"),
                        finished_at=data.get("finished_at"),
                        status=data.get("status") or "done")
        elif fam == "state":
            base.update(state_key=row["source_id"], value=data)
        elif fam == "memory":
            base.update(
                memory_type=ex.get("memory_type") or "fact",
                content=canon.dumps(data, ensure_ascii=False)
                if not isinstance(data, str) else data,
                importance=3, retrieval_strength=0.5,
                tier="hot", pinned=False)
            base["content_hash"] = canon.content_hash(base["content"])
        else:
            base["value"] = data
        return base

    def _event_descs(self, row: dict, now: str) -> list:
        """source 行 → staged event 記述子列。"""
        src, data, ikey = row["source_table"], row["row"], row["import_key"]
        if src == "turns":
            out = []
            for ev in mig.turn_row_to_events(data, ikey):
                ev["correlation"] = {"turn_id": str(data.get("id"))}
                ev["session"] = {"channel": data.get("source") or "unknown",
                                 "session_id": data.get("session_key")}
                ev["ingress_key"] = f"mig:{ev['part']}"
                ev["migration"] = {"source": "turns",
                                   "import_key": ikey,
                                   "reason": mig.SEED_REASON}
                ev["source_meta"] = {"import_key": ikey, "migrated": True}
                ev["import_key"] = ikey
                out.append(ev)
            return out
        if src == "history":
            ev = mig.history_row_to_event(data, ikey)
            ev["ingress_key"] = f"mig:{ikey}"
            ev["migration"] = {"source": "history", "import_key": ikey,
                               "reason": mig.SEED_REASON}
            ev["source_meta"] = {"import_key": ikey, "migrated": True}
            ev["import_key"] = ikey
            return [ev]
        if src == "messages":
            return [{
                "event_type": "migration",
                "payload": {"action": "messages_archive",
                            "source": "messages",
                            "message_id": data.get("id"),
                            "session_scope": data.get("session_scope"),
                            "role": data.get("role"),
                            "content": data.get("content"),
                            "import_key": ikey},
                "occurred_at": data.get("created_at") or now,
                "actor": "system",
                "ingress_key": f"mig:{ikey}",
                "migration": {"source": "messages",
                              "import_key": ikey,
                              "reason": mig.SEED_REASON},
                "source_meta": {"import_key": ikey, "migrated": True},
                "import_key": ikey,
            }]
        if src == "git:persona":
            return [{
                "event_type": "migration",
                "payload": {"action": "git_history",
                            "file": data.get("file"),
                            "versions": data.get("versions"),
                            "import_key": ikey},
                "occurred_at": data.get("created_at") or now,
                "actor": "system",
                "ingress_key": f"mig:{ikey}",
                "migration": {"source": "git:persona",
                              "import_key": ikey,
                              "reason": mig.SEED_REASON},
                "import_key": ikey,
            }]
        if src == "delegate_jobs":
            return [{
                "event_type": "job_transition",
                "payload": {"job_family": "delegate",
                            "job_id": str(data.get("id")),
                            "to": data.get("status") or "done",
                            "exit_code": data.get("exit_code"),
                            "import_key": ikey},
                "occurred_at": data.get("finished_at")
                or data.get("created_at") or now,
                "actor": "system",
                "ingress_key": f"mig:{ikey}:jt",
                "migration": {"source": "delegate_jobs",
                              "import_key": ikey,
                              "reason": mig.SEED_REASON},
                "import_key": ikey,
            }]
        if src == "state:inner":
            return [{
                "event_type": "state_change",
                "payload": {"key": "inner", "value": data,
                            "import_key": ikey},
                "occurred_at": now, "actor": "system",
                "ingress_key": f"mig:{ikey}:sc",
                "migration": {"source": "state:inner",
                              "import_key": ikey,
                              "reason": mig.SEED_REASON},
                "import_key": ikey,
            }]
        raise ValueError(f"no event mapping for source {src}")

    # --------------------------------------------------------- helpers
    def _ds_get(self, conn, ikey, col):
        r = conn.execute(
            f"SELECT {col} FROM deliver_state WHERE import_key=?",
            (ikey,)).fetchone()
        return r[col] if r else None

    def _mark_delivered(self, conn, ikey, run_id, target, target_id):
        self.deliver.mark(
            conn, ikey, self._ds_get(conn, ikey, "source_table") or "?",
            self._ds_get(conn, ikey, "source_id") or "?",
            target, target_id=target_id, status="delivered", run_id=run_id)

    def _fail(self, conn, ikey, e, run_id):
        self.deliver.mark(
            conn, ikey, self._ds_get(conn, ikey, "source_table") or "?",
            self._ds_get(conn, ikey, "source_id") or "?",
            "quarantine", status="failed",
            detail={"error": str(e)[:300]}, run_id=run_id)
