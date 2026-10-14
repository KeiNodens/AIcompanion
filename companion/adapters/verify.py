"""adapters.verify —— L-VER 検証層（detect-only sweep・verdict 再導出・
adapter conformance・oracle parity のオーケストレーション）。

構成（phase8-plan.md P8-5）:
- `oracle_parity`: kernel.oracle（独立第2実装）と fold の意味層一致
- `rederive_verdicts`: 全 write_proposal をイベント列から再構成した ctx で
  admit.evaluate に掛け直し、刻印済み verdict との乖離を検出する。
  ctx は「その write_proposal の直前 seq 時点」の物化状態で、
  admission._build_ctx と同じ情報源を Event Log からだけ再構成する
  （scope_registry は operational でイベント化されないため現値を使う。
   scope 追加があると歴史的 verdict を変え得る → 既知の限界として記録）。
- `sweep`: 12種の detect-only 検査。全て読み取りのみ・修復しない。
- `conformance`: adapter の宣言（emit 可能 event 型）と実観測の照合、
  canonical 書込み経路の構造的検査（append-only trigger・第2接続遮断・
  receipt-first・gate-as-floor の事後不変条件）。

重要: このモジュールは「検査」しかしない。異常を見つけても直さない
（修復は人間判断。detect-only が設計要件）。
"""

import sqlite3
from datetime import datetime, timezone

from ..kernel import admit, canon, events, fold, oracle

# ------------------------------------------------------------ 宣言表

# adapter → その adapter が emit し得る event_type の宣言。
# intake の正規化 event は envelope 宣言型に追随する（動的）ため
# ここには marker 系のみ載せる。open-world なので「未登録型の出現」は
# reject ではなく検査シグナル（conformance の declared_coverage）。
DECLARED_EMISSIONS = {
    "intake": {"skipped_part", "ingress_gap"},
    "admission": {"write_proposal", "candidate_review", "approval",
                  "memory_write", "memory_update", "record_write",
                  "record_update"},
    "extraction": {"extraction_poisoned", "extraction_scanned"},
    "supply": {"context_assembly"},
    "derived": {"migration"},
    "cognitive": {"policy_change", "reflection_trigger"},
    "writer": set(),  # writer は append のみで独自型を emit しない
}

# tombstone 類似検査の件数上限（sweep の detect-only レポート用）
_SWEEP_SAMPLE = 20


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


class Verifier:
    """core.db に対する検査群。全メソッド detect-only（書込みなし）。"""

    def __init__(self, writer, *, derived=None, clock=None):
        self.writer = writer
        self.derived = derived
        self._clock = clock or _now

    # ==================================================== oracle parity
    def oracle_parity(self) -> dict:
        """fold vs oracle の意味層一致＋oracle 側の独立鎖検査。"""
        rows = self.writer._conn.execute(
            "SELECT * FROM events ORDER BY seq").fetchall()
        f = fold.fold_rows(rows)
        o = oracle.oracle_fold(rows)
        cmp = oracle.compare(f, o)
        chain = oracle.oracle_verify_chain(rows)
        return {"ok": cmp["ok"] and chain["ok"],
                "compare": cmp, "chain": chain,
                "events": len(rows)}

    # ============================================== verdict の再導出
    def rederive_verdicts(self) -> dict:
        """全 write_proposal を直前 seq の再構成 ctx で再評価し、
        刻印済み candidate_review verdict との乖離を列挙する。

        戻り値: {"ok": 乖離なし, "proposals": n, "mismatches": [...]}
        """
        rows = [dict(r) for r in self.writer._conn.execute(
            "SELECT * FROM events ORDER BY seq")]
        parsed = [events.parse_row(r) for r in rows]

        # write_proposal event_id → 対応する candidate_review
        reviews = {}
        for r in parsed:
            if r["event_type"] == "candidate_review":
                ref = (r["payload"] or {}).get("proposal_event")
                if ref:
                    reviews[ref] = r

        scopes = {r["scope"] for r in self.writer._conn.execute(
            "SELECT scope FROM scope_registry WHERE status='active'")}

        # 漸進 ctx（events < seq の状態を保つ）
        ev, records, mut_seqs = {}, {}, {}
        tombs, terminal, links_map = {}, {}, {}
        hist_cur, hist_past = {}, {}
        covers_map, pending_cands, quarantined = {}, {}, {}
        pols = {}
        mismatches, proposals = [], 0

        for r in parsed:
            seq, et, p = r["seq"], r["event_type"], r["payload"]

            if et == "write_proposal":
                proposals += 1
                contaminated = set()
                for prop in quarantined.values():
                    for b in prop.get("basis_events") or []:
                        if isinstance(b, dict) and b.get("event_id"):
                            contaminated.add(b["event_id"])
                            cause = (ev.get(b["event_id"]) or {}).get(
                                "cause_event_id")
                            if cause:
                                contaminated.add(cause)
                covers = {}
                for (scope, rid), s in covers_map.items():
                    covers.setdefault(scope, []).append(
                        (f"record:{rid}", set(s)))
                for (scope, cid), s in pending_cands.items():
                    covers.setdefault(scope, []).append(
                        (f"candidate:{cid}", set(s)))
                bh = {}
                for (scope, rid), rst in records.items():
                    ch = rst.get("content_hash")
                    if ch:
                        bh.setdefault((scope, ch), []).append(
                            (rid, rst.get("status")))
                ctx = {"events": dict(ev), "records": dict(records),
                       "by_content_hash": bh,
                       "tombstones": list(tombs.values()),
                       "terminal_candidates": dict(terminal),
                       "links": list(links_map.values()),
                       "history_hashes": {k: v[-5:]
                                          for k, v in hist_past.items()},
                       "scopes": set(scopes), "head_seq": seq - 1,
                       "policy_digests": dict(pols),
                       "mutation_seqs": dict(mut_seqs),
                       "source_covers": covers,
                       "contaminated_events": contaminated}
                prop = (p or {}).get("proposal") or {}
                v2 = admit.evaluate(prop, ctx)
                rec = reviews.get(r["event_id"])
                recorded = (rec["payload"] or {}).get("verdict") \
                    if rec else None
                if recorded != v2["verdict"]:
                    mismatches.append({
                        "seq": seq, "proposal_event": r["event_id"],
                        "recorded": recorded, "rederived": v2["verdict"],
                        "recorded_hash": (rec["payload"] or {}).get(
                            "inputs_hash") if rec else None,
                        "rederived_hash": v2["inputs_hash"]})

            # ---- ctx 前進（このイベントの効果を反映）
            smeta = canon.loads(r["source_meta_json"]) \
                if r.get("source_meta_json") else {}
            ev[r["event_id"]] = {
                "event_id": r["event_id"], "event_type": et,
                "actor": r["actor"], "origin": smeta.get("origin"),
                "sanity_flag": bool((p or {}).get("sanity_flag")),
                "companion_artifact": bool(
                    smeta.get("companion_artifact")),
                "cause_event_id": (p or {}).get("cause_event_id"),
                "turn_id": (p or {}).get("turn_id"), "seq": seq}

            if et in ("policy_change",):
                rid = (p or {}).get("rule")
                if rid:
                    pols[rid] = (p or {}).get("digest")
                    if (p or {}).get("to_version"):
                        pols[p["to_version"]] = p.get("digest")
                continue
            if et not in vocab_mutation():
                continue
            st = (p or {}).get("new_state") or {}
            family = ("memory" if et in ("memory_write", "memory_update")
                      else (p or {}).get("record_family"))
            scope, rid_ = st.get("scope"), st.get("record_id")
            if not (scope and rid_):
                continue
            table, row = fold.row_for(family, st, seq)

            # _build_ctx は全 mutation から履歴 hash を集める（family 不問）
            prev_h = hist_cur.get((scope, rid_))
            if prev_h is not None and prev_h != st.get("content_hash"):
                hist_past.setdefault((scope, rid_), []).append(prev_h)
            hist_cur[(scope, rid_)] = st.get("content_hash")

            # record_source は全 family の mutation で refresh される
            src = {e["event_id"] for e in
                   (st.get("provenance") or {}).get("source_events") or []
                   if isinstance(e, dict) and e.get("event_id")}
            covers_map[(scope, rid_)] = src

            if table == "records":
                records[(scope, rid_)] = st
                mut_seqs[(scope, rid_)] = seq
            elif table == "candidates":
                sk = row.get("suppression_key")
                if sk:
                    terminal[(scope, sk)] = {
                        "until": row.get("suppression_until"),
                        "status": row.get("status")}
                prop = row.get("proposal_json")
                prop = canon.loads(prop) if isinstance(prop, str) \
                    else (prop or st.get("proposal") or {})
                if row.get("status") == "pending":
                    s = {b.get("event_id") for b in
                         prop.get("basis_events") or []
                         if isinstance(b, dict) and b.get("event_id")}
                    if s:
                        pending_cands[(scope, rid_)] = s
                else:
                    pending_cands.pop((scope, rid_), None)
                if row.get("status") == "quarantined":
                    quarantined[rid_] = prop
                else:
                    quarantined.pop(rid_, None)
            elif table == "tombstones":
                tombs[(scope, rid_)] = {
                    "scope": scope,
                    "pattern_hash": row.get("pattern_hash"),
                    "pattern": row.get("pattern"),
                    "status": row.get("status")}
            elif table == "links":
                # _build_ctx は status 不問で links 全行を読む
                links_map[(scope, rid_)] = (
                    row.get("source_id"), row.get("target_id"),
                    row.get("link_type"), row.get("blocking"))

        return {"ok": not mismatches, "proposals": proposals,
                "mismatches": mismatches}

    # ==================================================== sweep（12種・detect-only）
    def sweep(self) -> dict:
        """12種の定期検査。全て読み取りのみ。戻り値は検査ごとの
        {"ok", "findings"}。ok=False は「要調査の兆候」。"""
        conn = self.writer._conn
        out = {}

        # 1. hash chain
        v = self.writer.verify_chain()
        out["chain"] = {"ok": v["ok"], "findings":
                        ([v["bad"]] if v["bad"] else [])}

        # 2. replay（fold vs 格納表）
        r = self.writer.replay_check()
        out["replay"] = {"ok": r["ok"], "findings": r["diffs"],
                         "unhandled": r["unhandled_event_types"]}

        # 3. oracle parity
        op = self.oracle_parity()
        out["oracle_parity"] = {"ok": op["ok"],
                                "findings": op["compare"]["diffs"]
                                + op["chain"]["errors"]}

        # 4. manifest pin
        from ..kernel.writer import verify_manifest
        m = verify_manifest()
        out["manifest_pin"] = {"ok": m["ok"],
                               "findings": ([m.get("detail")] if not m["ok"]
                                            else [])}

        # 5. provenance coverage（record_source が指す event が存在するか）
        missing = [dict(r) for r in conn.execute(
            "SELECT rs.scope, rs.record_id, rs.event_id FROM record_source"
            " rs LEFT JOIN events e ON e.event_id=rs.event_id"
            " WHERE e.event_id IS NULL LIMIT ?", (_SWEEP_SAMPLE,))]
        out["provenance_coverage"] = {"ok": not missing,
                                      "findings": missing}

        # 6. derived 世代の陳腐化（active gen の frozen_read_seq < head）
        gen_row = conn.execute(
            "SELECT gen, frozen_read_seq FROM derived_generations"
            " WHERE is_active=1").fetchone()
        head = conn.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0
        findings = []
        if gen_row and gen_row["frozen_read_seq"] < head:
            findings.append({"gen": gen_row["gen"],
                             "frozen": gen_row["frozen_read_seq"],
                             "head": head})
        out["index_staleness"] = {"ok": not findings, "findings": findings}

        # 7. stalled claims（poison 隔離済みで放置されている分）
        stalled = [dict(r) for r in conn.execute(
            "SELECT claim_id, processor, event_id FROM claims"
            " WHERE status='stalled' LIMIT ?", (_SWEEP_SAMPLE,))]
        out["claims_stalled"] = {"ok": not stalled, "findings": stalled}

        # 8. pending candidates（承認待ち滞留）
        pend = [dict(r) for r in conn.execute(
            "SELECT scope, candidate_id, created_at FROM candidates"
            " WHERE status='pending' LIMIT ?", (_SWEEP_SAMPLE,))]
        out["pending_candidates"] = {"ok": not pend, "findings": pend}

        # 9. tombstone residue（active memory が active tombstone pattern と
        #    hash 完全一致 = 埋葬後に同内容が書き込まれた）
        res = [dict(r) for r in conn.execute(
            "SELECT r.scope, r.record_id, t.tombstone_id FROM records r"
            " JOIN tombstones t ON t.scope=r.scope"
            " AND t.pattern_hash=r.content_hash"
            " WHERE r.status='active' AND t.status='active'"
            " LIMIT ?", (_SWEEP_SAMPLE,))]
        out["tombstone_residue"] = {"ok": not res, "findings": res}

        # 10. watermark lag（未処理 claims が残るのに水位が止まっている）
        lag = [dict(r) for r in conn.execute(
            "SELECT w.processor, w.last_done_seq FROM watermarks w"
            " WHERE w.last_done_seq < (SELECT MAX(seq) FROM events)"
            " AND EXISTS (SELECT 1 FROM claims c"
            "            WHERE c.processor=w.processor"
            "            AND c.status='claimed') LIMIT ?",
            (_SWEEP_SAMPLE,))]
        out["watermark_lag"] = {"ok": not lag, "findings": lag}

        # 11. rule pin（mutation の param_policy_ref が policy_state にあるか）
        unpin = []
        for r in conn.execute(
                "SELECT seq, payload_json FROM events"
                " WHERE event_type IN ('memory_write','memory_update',"
                "'record_write','record_update')"):
            p = canon.loads(r["payload_json"]) or {}
            pref = p.get("param_policy_ref")
            if not pref:
                continue
            hit = conn.execute(
                "SELECT 1 FROM policy_state WHERE rule_id=? OR version=?",
                (pref, pref)).fetchone()
            if not hit:
                unpin.append({"seq": r["seq"], "policy_ref": pref})
        out["rule_pin"] = {"ok": not unpin, "findings": unpin[:_SWEEP_SAMPLE]}

        # 12. envelope stuck（stored のまま normalized/quarantined に
        #     遷移していない = drain が拾えていない）
        stuck = [dict(r) for r in conn.execute(
            "SELECT envelope_key, adapter_id, status FROM ingress_envelopes"
            " WHERE status='stored' LIMIT ?", (_SWEEP_SAMPLE,))]
        out["envelope_stuck"] = {"ok": not stuck, "findings": stuck}

        return {"ok": all(c["ok"] for c in out.values()), "checks": out,
                "ran_at": self._clock()}

    # ==================================================== conformance
    def conformance(self) -> dict:
        """adapter 宣言と実観測の照合・構造的 canonical 経路の検査。"""
        conn = self.writer._conn
        checks = []

        def add(name, ok, detail=""):
            checks.append({"name": name, "ok": bool(ok),
                           "detail": detail})

        # A. append-only trigger が schema 側に存在する（接続横断で効く層）
        trigs = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        need = {n for n in trigs if "events" in n or "no_delete" in n}
        add("append_only_triggers", bool(trigs) and bool(need),
            f"triggers={sorted(trigs)}")

        # B1. 第2接続からの UPDATE/DELETE は schema trigger で遮断される
        try:
            rogue = sqlite3.connect(str(self.writer.path))
            try:
                rogue.execute("DELETE FROM events")
                rogue.commit()
                add("second_connection_blocked", False,
                    "rogue DELETE succeeded")
            except sqlite3.Error:
                rogue.rollback()
                add("second_connection_blocked", True)
            finally:
                rogue.close()
        except sqlite3.Error as e:
            add("second_connection_blocked", True, str(e))

        # B2. rogue append は trigger で防げないが、改竄コピーを開くと
        #     起動 sanity（hash chain）が fail-loud で止める = 検出可能。
        #     ※鎖を正しく計算して繋ぐ rogue append は原理的に区別不能 —
        #       既知の限界として phase8-impl に記録。
        import shutil
        import tempfile
        from pathlib import Path
        from ..kernel.writer import open_core
        self.writer._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tmp = Path(tempfile.mkdtemp()) / "rogue.db"
        shutil.copy2(self.writer.path, tmp)
        try:
            rcon = sqlite3.connect(str(tmp))
            rcon.execute(
                "INSERT INTO events(seq, event_id, event_type,"
                " recorded_at, actor, payload_json, prev_hash,"
                " row_hash, canon_algo) VALUES (999999,'rogue',"
                " 'rogue_write','2026-01-01T00:00:00.000000Z',"
                " 'companion','{}','bogus','bogus','v')")
            rcon.commit()
            rcon.close()
            try:
                w2 = open_core(tmp)
                w2.close()
                add("rogue_append_detectable", False,
                    "tampered copy opened cleanly")
            except Exception as e:
                add("rogue_append_detectable", True,
                    f"fail-loud: {type(e).__name__}")
        finally:
            try:
                tmp.unlink()
                tmp.parent.rmdir()
            except OSError:
                pass

        # C. mutation は writer.apply_mutation 経由の刻印を持つ
        #    （last_mutation_seq が実在する mutation event を指す）
        bad = []
        for r in conn.execute(
                "SELECT scope, record_id, last_mutation_seq FROM records"):
            e = conn.execute(
                "SELECT event_type FROM events WHERE seq=?",
                (r["last_mutation_seq"],)).fetchone()
            if not e or e["event_type"] not in (
                    "memory_write", "memory_update",
                    "record_write", "record_update"):
                bad.append({"scope": r["scope"],
                            "record_id": r["record_id"],
                            "seq": r["last_mutation_seq"]})
        add("mutation_wiring", not bad, f"bad={bad[:5]}")

        # D. receipt-first: context_assembly receipt の構造的健全性
        #    （injected リスト＋inputs_hash＋basis_seq が必須フィールド）
        bad_receipts = []
        for r in conn.execute(
                "SELECT seq, payload_json FROM events"
                " WHERE event_type='context_assembly'"):
            p = canon.loads(r["payload_json"]) or {}
            if not isinstance(p.get("injected"), list) \
                    or not p.get("inputs_hash") \
                    or p.get("basis_seq") is None:
                bad_receipts.append(r["seq"])
        add("receipt_shape", not bad_receipts,
            f"malformed receipts: seqs={bad_receipts[:5]}")

        # E. 実観測 event_type が vocab 登録集合内か（open-world ゆえ
        #    未登録型は受理されるが、conformance では出現をシグナル化）
        from ..kernel import vocab as _v
        observed = {r[0] for r in conn.execute(
            "SELECT DISTINCT event_type FROM events")}
        unregistered = sorted(observed - set(_v.EVENT_TYPES))
        add("vocab_registered_types", not unregistered,
            f"unregistered={unregistered}")

        # E2. adapter 宣言の一意性（同一 event_type を2 adapter が宣言する
        #     と emit 経路の帰属が曖昧になる）
        owner, dup = {}, []
        for ad, types in DECLARED_EMISSIONS.items():
            for t in types:
                if t in owner:
                    dup.append({"event_type": t,
                                "adapters": [owner[t], ad]})
                owner.setdefault(t, ad)
        add("emission_ownership_unique", not dup, f"dup={dup}")

        # F. gate-as-floor: accept 済み proposal の verdict が刻印と一致
        #    （accept 後に write 系 event が直後に続く構造の存在確認）
        orphans = []
        for r in conn.execute(
                "SELECT seq, event_id, payload_json FROM events"
                " WHERE event_type='write_proposal'"):
            p = canon.loads(r["payload_json"]) or {}
            if p.get("verdict") != "accept":
                continue
            nxt = conn.execute(
                "SELECT event_type FROM events WHERE seq=?",
                (r["seq"] + 1,)).fetchone()
            if not nxt or nxt["event_type"] != "candidate_review":
                orphans.append(r["seq"])
        add("verdict_receipt_pairing", not orphans,
            f"unpaired accept proposals: {orphans[:5]}")

        # G. derived active があれば canonical_digest が再計算と一致
        if self.derived:
            gen, path = self.derived.active()
            if gen is not None:
                acon, _ = self.derived.open_active()
                try:
                    row = acon.execute(
                        "SELECT value FROM derived_meta"
                        " WHERE key='canonical_digest'").fetchone()
                    frozen = acon.execute(
                        "SELECT value FROM derived_meta"
                        " WHERE key='frozen_read_seq'").fetchone()
                    cur = self.derived._canonical_digest(int(frozen[0]))
                    add("derived_digest_match", row and row[0] == cur,
                        f"gen={gen}")
                finally:
                    acon.close()
            else:
                add("derived_digest_match", True, "no active derived")
        else:
            add("derived_digest_match", True, "no derived store")

        # H. supply gate-as-floor: sensitivity=secret の record が
        #    context_assembly receipt の injected に混入していないか
        #    （供給面を素通りする読みが存在し得ないことの事後検査）
        leaked = []
        secret_rids = {r["record_id"] for r in conn.execute(
            "SELECT record_id FROM records"
            " WHERE json_extract(state_json,'$.sensitivity')='secret'")}
        for r in conn.execute(
                "SELECT payload_json FROM events"
                " WHERE event_type='context_assembly'"):
            p = canon.loads(r["payload_json"]) or {}
            for item in p.get("injected") or []:
                rid = item.get("id") if isinstance(item, dict) else None
                if rid in secret_rids:
                    leaked.append(rid)
        add("supply_gate_floor", not leaked,
            f"secret ids in receipts: {sorted(set(leaked))[:5]}")

        return {"ok": all(c["ok"] for c in checks), "checks": checks}


def vocab_mutation() -> set:
    from ..kernel import vocab
    return set(vocab.MUTATION_EVENT_TYPES)
