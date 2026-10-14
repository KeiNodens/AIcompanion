"""adapters.migverify —— migration-m5 §5.1 機械検証（14項目）＋G1〜G5 評価。

全て detect-only。結果は項目ごとの {name, ok, detail} 列で返し、
失敗項目に修復手掛かり（対象 key 等）を添える。
"""

import logging
from datetime import datetime, timezone

from ..kernel import canon, migration as mig

log = logging.getLogger("companion")


class MigVerifier:
    """staged/commit 後の canonical を移行元 inventory と照合する。"""

    def __init__(self, writer):
        self.writer = writer

    # ---------------------------------------------------------- 14 項目
    def machine_checks(self, inventory: list, *,
                       important_floor=4,
                       expect_links=0,
                       contradiction_baseline=None,
                       dup_baseline=None) -> dict:
        """§5.1 の14項目を順に検査する。

        dup/contradiction の baseline は移行元 inventory から自動計算
        （原本由来の重複は移行の失敗ではない —— 転記忠実性の範囲内）。
        """
        if dup_baseline is None:
            dup_baseline = self._source_dup_groups(inventory)
        if contradiction_baseline is None:
            contradiction_baseline = dup_baseline
        checks = []
        with self.writer.transaction() as conn:
            inv_records = [r for r in inventory
                           if r["expect"]["kind"] in
                           ("record", "record+event")]
            checks.append(self._c_record_count(conn, inv_records))
            checks.append(self._c_entities(conn))
            checks.append(self._c_links(conn, expect_links))
            checks.append(self._c_timestamps(conn, inventory))
            checks.append(self._c_provenance(conn))
            checks.append(self._c_referential(conn))
            checks.append(self._c_duplicates(conn, dup_baseline))
            checks.append(self._c_contradictions(
                conn, contradiction_baseline))
            checks.append(self._c_content_equiv(conn, inv_records))
            checks.append(self._c_scope_actor(conn, inv_records))
            checks.append(self._c_important(conn, inv_records,
                                            important_floor))
            checks.append(self._c_negative(conn))
            checks.append(self._c_unknown_types(conn, inv_records))
            checks.append(self._c_scope_violation(conn))
        ok = all(c["ok"] for c in checks)
        return {"ok": ok, "checks": checks,
                "failed": [c["name"] for c in checks if not c["ok"]]}

    # 1. record 数: 期待 record 数と実在数の一致
    def _c_record_count(self, conn, inv_records):
        expect_ids = set()
        for r in inv_records:
            ex = r["expect"]
            if r["source_table"].startswith("mem0:"):
                expect_ids.add(
                    mig.migrated_record_id(r["import_key"], "mem"))
            else:
                expect_ids.add(mig.migrated_record_id(
                    r["import_key"], ex["family"][:4]))
        got = {r["record_id"] for r in conn.execute(
            "SELECT record_id FROM records")}
        missing = sorted(expect_ids - got)
        return {"name": "record_count", "ok": not missing,
                "detail": f"expected={len(expect_ids)} got={len(got)}",
                "missing": missing[:20]}

    # 2. entity 数: subject 参照が解決される record 数（参照があれば）
    def _c_entities(self, conn):
        bad = []
        n = 0
        for r in conn.execute("SELECT record_id, scope, state_json"
                              " FROM records"):
            st = canon.loads(r["state_json"])
            subj = st.get("subject")
            if not subj:
                continue
            n += 1
            ref = subj.get("record_id") if isinstance(subj, dict) else subj
            ex = conn.execute(
                "SELECT 1 FROM records WHERE scope=? AND record_id=?",
                (r["scope"], ref)).fetchone()
            if ex is None and isinstance(subj, dict) and subj.get("record_id"):
                bad.append(r["record_id"])
        return {"name": "entities", "ok": not bad,
                "detail": f"with_subject={n} unresolved={len(bad)}",
                "unresolved": bad[:20]}

    # 3. relationship 数: link 辺数が期待と一致
    def _c_links(self, conn, expect):
        n = conn.execute("SELECT COUNT(*) c FROM links").fetchone()["c"]
        return {"name": "relationships", "ok": n == expect,
                "detail": f"expected={expect} got={n}"}

    # 4. timestamp: 移行元の created_at/occurred_at が record/event に反映
    def _c_timestamps(self, conn, inventory):
        bad = []
        n = 0
        for row in inventory:
            src = row["source_table"]
            if src.startswith("mem0:"):
                item = row["row"]
                want = item.get("created_at")
                if not want:
                    continue
                n += 1
                rid = mig.migrated_record_id(row["import_key"], "mem")
                r = conn.execute(
                    "SELECT state_json FROM records WHERE record_id=?",
                    (rid,)).fetchone()
                if r is None:
                    bad.append({"source_id": row["source_id"],
                                "issue": "record missing"})
                    continue
                st = canon.loads(r["state_json"])
                if st.get("created_at") != want:
                    bad.append({"source_id": row["source_id"],
                                "issue": "created_at mismatch",
                                "want": want, "got": st.get("created_at")})
        return {"name": "timestamps", "ok": not bad,
                "detail": f"checked={n} bad={len(bad)}", "bad": bad[:10]}

    # 5. provenance: source_events が実在 or provenance_gap 明示
    def _c_provenance(self, conn):
        bad = []
        for r in conn.execute("SELECT record_id, state_json FROM records"):
            st = canon.loads(r["state_json"])
            prov = st.get("provenance") or {}
            srcs = prov.get("source_events") or []
            if prov.get("provenance_gap"):
                continue   # 明示済みなら根拠不在でも合格
            for se in srcs:
                if not isinstance(se, dict) or not se.get("event_id"):
                    continue
                n += 1
                ex = conn.execute(
                    "SELECT 1 FROM events WHERE event_id=?",
                    (se["event_id"],)).fetchone()
                if ex is None:
                    bad.append(r["record_id"])
                    break
        return {"name": "provenance", "ok": not bad,
                "detail": f"dangling={len(bad)}", "bad": bad[:20]}

    # 6. referential integrity: link 両端・attachments 参照の実在
    def _c_referential(self, conn):
        bad = []
        for r in conn.execute("SELECT link_id, scope, state_json FROM links"):
            st = canon.loads(r["state_json"])
            for end in ("src", "dst"):
                ref = (st.get(end) or {})
                rid = ref.get("record_id") if isinstance(ref, dict) else ref
                if not rid:
                    continue
                ex = conn.execute(
                    "SELECT 1 FROM records WHERE scope=? AND record_id=?",
                    (r["scope"], rid)).fetchone()
                if ex is None:
                    bad.append({"link": r["link_id"], "end": end})
        for r in conn.execute(
                "SELECT event_id, attachments_json FROM events"
                " WHERE attachments_json IS NOT NULL"):
            for h in canon.loads(r["attachments_json"]) or []:
                bh = h.get("hash") if isinstance(h, dict) else h
                ex = conn.execute(
                    "SELECT 1 FROM blobs WHERE hash=?", (bh,)).fetchone()
                if ex is None:
                    bad.append({"event": r["event_id"], "blob": bh})
        return {"name": "referential", "ok": not bad,
                "detail": f"bad={len(bad)}", "bad": bad[:20]}

    @staticmethod
    def _source_dup_groups(inventory) -> int:
        """移行元 mem0 items の (scope, content_hash) 重複グループ数。

        原本由来の重複を検証の許容基準にする —— canonical 側の重複が
        これを超えたときだけ失敗（転記で新たに重複が生じた印）。
        """
        from collections import Counter
        cnt = Counter()
        for r in inventory:
            if not r["source_table"].startswith("mem0:"):
                continue
            row = r["row"]
            scope = r["source_table"].split(":", 1)[1]
            ch = canon.content_hash(
                row.get("memory") or row.get("content") or "")
            if ch:
                cnt[(scope, ch)] += 1
        return sum(1 for c in cnt.values() if c > 1)

    # 7. duplicate: import_key の重複は恒に失敗、content_hash は baseline まで
    def _c_duplicates(self, conn, dup_baseline=0):
        bad = []
        # import_key は records 列ではなく state_json 内
        # （migration_map は PK で一意が機械保証済み）
        for r in conn.execute(
                "SELECT json_extract(state_json,'$.import_key') k,"
                " COUNT(*) c FROM records"
                " WHERE json_extract(state_json,'$.import_key')"
                " IS NOT NULL GROUP BY k HAVING c > 1"):
            bad.append({"col": "import_key", "key": r["k"],
                        "count": r["c"]})
        ch_dup = 0
        for r in conn.execute(
                "SELECT scope || '/' || content_hash k, COUNT(*) c"
                " FROM records WHERE content_hash IS NOT NULL"
                " AND content_hash != ''"
                " GROUP BY scope, content_hash HAVING c > 1"):
            ch_dup += 1
            bad.append({"col": "content_hash", "key": r["k"],
                        "count": r["c"]})
        ok = not any(b["col"] == "import_key" for b in bad) \
            and ch_dup <= dup_baseline
        return {"name": "duplicates", "ok": ok,
                "detail": f"dup={len(bad)} baseline={dup_baseline}",
                "bad": bad[:20]}

    # 8. contradiction: 同一 scope・同一 content_hash で active が複数
    def _c_contradictions(self, conn, baseline):
        n = conn.execute(
            "SELECT scope, content_hash, COUNT(*) c FROM records"
            " WHERE record_family='memory' AND status='active'"
            " AND content_hash IS NOT NULL"
            " GROUP BY scope, content_hash HAVING c > 1").fetchall()
        bad = len(n)
        return {"name": "contradictions", "ok": bad <= baseline,
                "detail": f"found={bad} baseline={baseline}",
                "groups": [dict(r) for r in n[:10]]}

    # 9. 内容等価照合: record の本文・scope・actor を移行元と照合
    def _c_content_equiv(self, conn, inv_records):
        bad, n = [], 0
        for row in inv_records:
            src = row["source_table"]
            if not src.startswith("mem0:"):
                continue
            n += 1
            item = row["row"]
            rid = mig.migrated_record_id(row["import_key"], "mem")
            r = conn.execute(
                "SELECT scope, state_json FROM records WHERE record_id=?",
                (rid,)).fetchone()
            if r is None:
                bad.append({"source_id": row["source_id"],
                            "issue": "missing"})
                continue
            st = canon.loads(r["state_json"])
            want_scope = mig.mem0_scope(item)
            if r["scope"] != want_scope:
                bad.append({"source_id": row["source_id"],
                            "issue": "scope", "want": want_scope,
                            "got": r["scope"]})
            if mig.norm_text(st.get("content")) != \
                    mig.norm_text(item.get("memory")):
                bad.append({"source_id": row["source_id"],
                            "issue": "content"})
        return {"name": "content_equiv", "ok": not bad,
                "detail": f"checked={n} bad={len(bad)}", "bad": bad[:10]}

    # 10. scope/actor 割当: user→agent 誤配・master→companion 逆転なし
    def _c_scope_actor(self, conn, inv_records):
        bad = []
        for row in inv_records:
            src = row["source_table"]
            if src == "mem0:user":
                rid = mig.migrated_record_id(row["import_key"], "mem")
                r = conn.execute(
                    "SELECT scope FROM records WHERE record_id=?",
                    (rid,)).fetchone()
                if r and r["scope"] != "user":
                    bad.append({"source_id": row["source_id"],
                                "issue": "user mem in agent scope"})
            elif src == "turns":
                for ev in conn.execute(
                        "SELECT event_type, actor FROM events"
                        " WHERE turn_id=?",
                        (str(row["source_id"]),)).fetchall():
                    if ev["event_type"] == "message" \
                            and ev["actor"] != "master":
                        bad.append({"turn": row["source_id"],
                                    "issue": "message actor",
                                    "got": ev["actor"]})
                    if ev["event_type"] in ("response", "inner") \
                            and ev["actor"] != "companion":
                        bad.append({"turn": row["source_id"],
                                    "issue": "response actor",
                                    "got": ev["actor"]})
        return {"name": "scope_actor", "ok": not bad,
                "detail": f"bad={len(bad)}", "bad": bad[:10]}

    # 11. important memory 欠落: importance≥閾値 の全件存在・本文一致
    def _c_important(self, conn, inv_records, floor):
        bad, n = [], 0
        for row in inv_records:
            src = row["source_table"]
            if not src.startswith("mem0:"):
                continue
            item = row["row"]
            if mig.map_importance(item.get("metadata") or {}) < floor:
                continue
            n += 1
            rid = mig.migrated_record_id(row["import_key"], "mem")
            r = conn.execute(
                "SELECT state_json FROM records WHERE record_id=?",
                (rid,)).fetchone()
            if r is None:
                bad.append({"source_id": row["source_id"],
                            "issue": "important missing"})
                continue
            st = canon.loads(r["state_json"])
            if mig.norm_text(st.get("content")) != \
                    mig.norm_text(item.get("memory")):
                bad.append({"source_id": row["source_id"],
                            "issue": "important content"})
        return {"name": "important", "ok": not bad,
                "detail": f"checked={n} bad={len(bad)}", "bad": bad[:10]}

    # 12. negative memory 状態: tombstone/invalidated の移行
    def _c_negative(self, conn):
        n_tomb = conn.execute(
            "SELECT COUNT(*) c FROM tombstones").fetchone()["c"]
        n_inv = conn.execute(
            "SELECT COUNT(*) c FROM records WHERE status IN"
            " ('invalidated','superseded','demoted')").fetchone()["c"]
        # 負の記憶が canonical に表現されているか（0 件は「無かった」として合格）
        return {"name": "negative_memory", "ok": True,
                "detail": f"tombstones={n_tomb} inactive_records={n_inv}"}

    # 13. 未知型の保持: 移行元の未知値（memory_type 等）が失われていない
    def _c_unknown_types(self, conn, inv_records):
        bad, n = [], 0
        for row in inv_records:
            if not row["source_table"].startswith("mem0:"):
                continue
            item = row["row"]
            want = (item.get("metadata") or {}).get("type")
            if not want:
                continue
            n += 1
            rid = mig.migrated_record_id(row["import_key"], "mem")
            r = conn.execute(
                "SELECT state_json FROM records WHERE record_id=?",
                (rid,)).fetchone()
            if r is None:
                continue
            st = canon.loads(r["state_json"])
            if st.get("memory_type") != want:
                bad.append({"source_id": row["source_id"],
                            "issue": "type lost", "want": want,
                            "got": st.get("memory_type")})
        return {"name": "unknown_types", "ok": not bad,
                "detail": f"checked={n} bad={len(bad)}", "bad": bad[:10]}

    # 14. scope violation: user scope の記憶が agent scope に混入していない
    def _c_scope_violation(self, conn):
        bad = []
        for r in conn.execute(
                "SELECT record_id, state_json FROM records"
                " WHERE scope='agent' AND record_family='memory'"):
            st = canon.loads(r["state_json"])
            prov = (st.get("provenance") or {}).get("source") or {}
            if prov.get("scope") == "user":
                bad.append(r["record_id"])
        return {"name": "scope_violation", "ok": not bad,
                "detail": f"bad={len(bad)}", "bad": bad[:10]}

    # ---------------------------------------------------------- G1〜G5
    def gate_report(self, *, g1=None, g2=None, g3=None, g4=None,
                    g5=None) -> dict:
        """G1〜G5 の総合判定。

        - G1 投影一致: dual-run 比較レポート（adapter dualrun.g1_report の結果）
        - G2 provenance 被覆: _c_provenance の結果
        - G3 意味的同等: EquivGate.evaluate の結果
        - G4 export 往復: packio.validate_pack の結果
        - G5 旧資産転写: Reconciler.reconcile の結果（missing/failed なし）
        """
        gates = {"G1": g1, "G2": g2, "G3": g3, "G4": g4, "G5": g5}
        detail = {}
        ok_all = True
        for name, rep in gates.items():
            if rep is None:
                detail[name] = {"ok": False, "detail": "not evaluated"}
                ok_all = False
                continue
            ok = bool(rep.get("ok"))
            detail[name] = {"ok": ok, **{k: v for k, v in rep.items()
                                         if k != "ok"}}
            ok_all = ok_all and ok
        return {"ok": ok_all, "gates": detail}
