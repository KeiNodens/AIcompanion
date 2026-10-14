"""adapters.admission —— 単一 admission の不純なオーケストレーション

kernel.admit（純粋な検査列）に渡す ctx を writer 接続から prefetch し、
verdict に応じて write_proposal/candidate_review イベント・candidate・
memory/record mutation を同一 tx で commit する。

書込み効果:
  accept          → mutation（memory_write/record_write）＋現在値 upsert
  pending         → candidate(pending)＋review 要求（approvals 面は呼び出し側）
  quarantine      → candidate(quarantined)
  reject          → candidate(rejected)（tombstone 一致棄却は tombstone 刻印も可）
  noop_duplicate  → receipt のみ（同一内容の再言及は確証として events に残る）
  stale           → receipt のみ（再読・再提案を要求）

承認経路（core-api §4・write-path §5.3）:
  request_approval → approval record(pending) 登録＋approval イベント
  decide           → 構造化決裁面（ decided_by=master 必須・gate.inputs 再計算）
  on_master_message→ 会話内承認の照合（最初の master message を語彙照合）
"""

from datetime import datetime, timedelta, timezone

from ..kernel import admit, approval as appr_kernel, canon, ids


class AdmissionError(ValueError):
    pass


# candidate 終端の suppression TTL（実装時パラメータ —— impl 記録に明記）
SUPPRESS_TTL_DAYS = 30


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


class Admission:
    def __init__(self, writer, *, clock=None):
        self.writer = writer
        self._clock = clock or _now
        # 受理時発火 hook（suspect lane 等 —— Q-O66）。既定 None で挙動不変。
        self.on_accept = None

    # ------------------------------------------------------------- ctx
    def _build_ctx(self, conn) -> dict:
        """検査列が見る読み取り専用ビューを core.db から組み立てる。"""
        ev = {}
        for r in conn.execute(
                "SELECT event_id, event_type, actor, payload_json,"
                " source_meta_json, seq FROM events"):
            sm = canon.loads(r["source_meta_json"] or "{}")
            pl = canon.loads(r["payload_json"])
            ev[r["event_id"]] = {
                "event_id": r["event_id"], "event_type": r["event_type"],
                "actor": r["actor"], "origin": sm.get("origin"),
                "sanity_flag": bool((pl or {}).get("sanity_flag")),
                "companion_artifact": bool(sm.get("companion_artifact")),
                "cause_event_id": (pl or {}).get("cause_event_id"),
                "turn_id": (pl or {}).get("turn_id"),
                "seq": r["seq"],
            }
        records = {}
        by_hash = {}
        mut_seqs = {}
        for r in conn.execute(
                "SELECT scope, record_id, state_json, status, content_hash,"
                " last_mutation_seq FROM records"):
            st = canon.loads(r["state_json"])
            records[(r["scope"], r["record_id"])] = st
            mut_seqs[(r["scope"], r["record_id"])] = r["last_mutation_seq"]
            if r["content_hash"]:
                by_hash.setdefault(
                    (r["scope"], r["content_hash"]), []).append(
                    (r["record_id"], r["status"]))
        tombs = []
        for r in conn.execute(
                "SELECT scope, pattern_hash, pattern, status"
                " FROM tombstones"):
            tombs.append({"scope": r["scope"],
                          "pattern_hash": r["pattern_hash"],
                          "pattern": r["pattern"], "status": r["status"]})
        terminal = {}
        for r in conn.execute(
                "SELECT scope, suppression_key, suppression_until, status"
                " FROM candidates WHERE suppression_key IS NOT NULL"):
            terminal[(r["scope"], r["suppression_key"])] = {
                "until": r["suppression_until"], "status": r["status"]}
        links = [(r["source_id"], r["target_id"], r["link_type"],
                  r["blocking"]) for r in conn.execute(
                      "SELECT source_id, target_id, link_type, blocking"
                      " FROM links")]
        # superseded 版の content_hash 履歴（oscillation 検出用）。
        # mutation イベント列を辿り「最新でない版」の hash を集める
        hist_cur, hist_past = {}, {}
        for r in conn.execute(
                "SELECT payload_json FROM events WHERE event_type IN"
                " ('memory_write','memory_update','record_write',"
                "'record_update') ORDER BY seq"):
            p = canon.loads(r["payload_json"])
            st = (p or {}).get("new_state") or {}
            key = (st.get("scope"), st.get("record_id"))
            h = st.get("content_hash")
            prev = hist_cur.get(key)
            if prev is not None and h != prev:
                hist_past.setdefault(key, []).append(prev)
            hist_cur[key] = h
        hist = {k: v[-5:] for k, v in hist_past.items()}
        scopes = {r["scope"] for r in conn.execute(
            "SELECT scope FROM scope_registry WHERE status='active'")}
        head = conn.execute(
            "SELECT MAX(seq) FROM events").fetchone()[0] or 0
        # param_policy_ref は規則版文字列（"decay-v1"）を指すので、
        # rule_id と version の両方で引けるようにする。
        pols = {}
        for r in conn.execute(
                "SELECT rule_id, version, content_digest FROM policy_state"):
            pols[r["rule_id"]] = r["content_digest"]
            if r["version"]:
                pols[r["version"]] = r["content_digest"]

        # Q-O82 被覆集合: 受理済み memory（record_source）＋非終端 pending
        # candidate の basis 集合。{scope: [(owner, {event_id,...})]}
        cov_map = {}
        for r in conn.execute(
                "SELECT scope, record_id, event_id FROM record_source"):
            cov_map.setdefault((r["scope"], r["record_id"]),
                               set()).add(r["event_id"])
        covers = {}
        for (scope, rid), s in cov_map.items():
            covers.setdefault(scope, []).append((f"record:{rid}", s))
        for r in conn.execute(
                "SELECT scope, candidate_id, proposal_json FROM candidates"
                " WHERE status='pending'"):
            cp = canon.loads(r["proposal_json"]) or {}
            s = {b.get("event_id") for b in cp.get("basis_events") or []
                 if isinstance(b, dict) and b.get("event_id")}
            if s:
                covers.setdefault(r["scope"], []).append(
                    (f"candidate:{r['candidate_id']}", s))

        # SD-60 汚染伝播: quarantined candidate の proposal basis（直接辺）
        # ＋その cause_event_id 閉包（2段目）。
        contaminated = set()
        direct = set()
        for r in conn.execute(
                "SELECT proposal_json FROM candidates"
                " WHERE status='quarantined'"):
            cp = canon.loads(r["proposal_json"]) or {}
            for b in cp.get("basis_events") or []:
                if isinstance(b, dict) and b.get("event_id"):
                    direct.add(b["event_id"])
        contaminated |= direct
        for eid in direct:
            cause = (ev.get(eid) or {}).get("cause_event_id")
            if cause:
                contaminated.add(cause)

        return {"events": ev, "records": records,
                "by_content_hash": by_hash, "tombstones": tombs,
                "terminal_candidates": terminal, "links": links,
                "history_hashes": hist, "scopes": scopes,
                "head_seq": head, "policy_digests": pols,
                "mutation_seqs": mut_seqs,
                "source_covers": covers,
                "contaminated_events": contaminated}

    # ------------------------------------------------------------ submit
    def submit(self, proposal: dict, *, actor: str = "companion") -> dict:
        """proposal → 検査列 → verdict → 効果確定を1 tx で行う。"""
        with self.writer.transaction() as conn:
            ctx = self._build_ctx(conn)
            v = admit.evaluate(proposal, ctx)
            now = self._clock()

            prop_receipt = self.writer._append_locked(
                "write_proposal",
                {"proposal": proposal, "verdict": v["verdict"],
                 "inputs_hash": v["inputs_hash"]},
                actor=actor)
            verdict_receipt = self.writer._append_locked(
                "candidate_review",
                {"verdict": v["verdict"], "fired_rules": v["fired_rules"],
                 "inputs_hash": v["inputs_hash"],
                 "gate_inputs": v["gate_inputs"],
                 "checks": v["checks"],
                 "target": {"family": proposal.get("record_family"),
                            "record_id": proposal.get("target_id") or
                            (proposal.get("new_state") or {}).get(
                                "record_id"),
                            "scope": (proposal.get("new_state") or {})
                            .get("scope")},
                 "proposal_event": prop_receipt["event_id"]},
                actor="system")

            st = proposal.get("new_state") or {}
            out = {"verdict": v["verdict"], "inputs_hash": v["inputs_hash"],
                   "checks": v["checks"],
                   "proposal_event": prop_receipt["event_id"],
                   "verdict_event": verdict_receipt["event_id"]}

            if v["verdict"] == "accept":
                mut = self._apply_accept(proposal, st, v, now)
                out["mutation"] = mut
                # suspect lane 等の受理時発火 hook（Q-O66 駆動条件）。
                # None 既定で挙動不変。hook 内で submit してもよい
                # （transaction() は再入可能）。
                if self.on_accept and st.get("record_family") == "memory":
                    self.on_accept(st)
            elif v["verdict"] in ("pending", "quarantine", "reject"):
                cand = self._apply_candidate(proposal, st, v, now)
                out["candidate"] = cand
            return out

    def _apply_accept(self, proposal, st, v, now):
        st = dict(st)
        st.setdefault("provenance", {})
        st["provenance"]["evidence_basis"] = v["evidence_basis"]
        st["provenance"]["source_events"] = [
            {"event_id": b["event_id"], "cite": b.get("cite")}
            for b in proposal.get("basis_events") or []
            if isinstance(b, dict) and b.get("event_id")]
        st["provenance"]["generated_by"] = proposal.get("generated_by")
        st["provenance"]["generator"] = proposal.get("generator")
        fam = st["record_family"]
        if fam == "memory":
            et = "memory_update" if proposal.get("op") else "memory_write"
            payload = {"memory_id": st["record_id"], "new_state": st,
                       "reason": proposal.get("reason", "admission accept"),
                       "basis_events": proposal.get("basis_events") or []}
            if proposal.get("op"):
                payload["op"] = proposal["op"]
        else:
            et = "record_update" if proposal.get("op") else "record_write"
            payload = {"record_family": fam, "record_id": st["record_id"],
                       "new_state": st,
                       "reason": proposal.get("reason", "admission accept"),
                       "basis_events": proposal.get("basis_events") or []}
            if proposal.get("op"):
                payload["op"] = proposal["op"]
        return self.writer.apply_mutation(et, payload, actor="companion")

    def _apply_candidate(self, proposal, st, v, now, *, status=None):
        cand_status = status or {
            "pending": "pending", "quarantine": "quarantined",
            "reject": "rejected"}[v["verdict"]]
        cand_id = ids.record_id("candidate",
                                canon.hash_bytes(
                                    "pcore.cand_id.v1",
                                    canon.dumps_bytes(proposal))[:24])
        cand_state = {
            "record_id": cand_id, "record_family": "candidate",
            "scope": st.get("scope", "user"), "status": cand_status,
            "record_version": 1, "created_at": now, "updated_at": now,
            "proposal": proposal, "verdicts": v["checks"],
            "gate_inputs": v["gate_inputs"],
            "review": {"required": cand_status == "pending"},
            "suppression": {"key": v["suppression_key"], "until": None,
                            "lifted_by": None}
            if cand_status in ("rejected", "expired")
            else {"key": v["suppression_key"], "until": None,
                  "lifted_by": None},
            "provenance": {"generated_by": proposal.get("generated_by"),
                           "source_events": [
                               b for b in proposal.get("basis_events") or []
                               if isinstance(b, dict)]},
        }
        if cand_status in ("rejected", "expired"):
            cand_state["suppression"]["until"] = canon.canon_ts(
                datetime.now(timezone.utc)
                + timedelta(days=SUPPRESS_TTL_DAYS))
        receipt = self.writer.apply_mutation(
            "record_write",
            {"record_family": "candidate", "record_id": cand_id,
             "new_state": cand_state,
             "reason": f"admission verdict: {v['verdict']}",
             "basis_events": proposal.get("basis_events") or []},
            actor="system")
        return dict(receipt, candidate_id=cand_id, status=cand_status)

    # ---------------------------------------------------------- approval
    def request_approval(self, target: dict, content_hash: str,
                         requester: str = "companion", expires_at=None,
                         scope: str = "user") -> dict:
        """approval record(pending) の登録（承認要求）。"""
        now = self._clock()
        aid = ids.record_id("approval",
                            canon.hash_bytes("pcore.apr_id.v1",
                                             (canon.dumps(target)
                                              + now).encode())[:24])
        state = {"record_id": aid, "record_family": "approval",
                 "scope": scope, "status": "pending",
                 "record_version": 1, "created_at": now, "updated_at": now,
                 "target": target, "content_hash": content_hash,
                 "requester": requester, "expires_at": expires_at,
                 "decided_by": None, "decided_at": None}
        with self.writer.transaction():
            self.writer._append_locked(
                "approval",
                {"approval_id": aid, "action": "request",
                 "target": target, "content_hash": content_hash,
                 "expires_at": expires_at, "requester": requester},
                actor="companion")
            self.writer.apply_mutation(
                "record_write",
                {"record_family": "approval", "record_id": aid,
                 "new_state": state, "reason": "approval request"},
                actor="system")
        return {"approval_id": aid, "status": "pending"}

    def decide(self, approval_id: str, verdict: str, decided_by: str,
               conn=None) -> dict:
        """構造化決裁面。decided_by=master 必須・gate.inputs 再計算。"""
        if verdict not in ("approved", "rejected"):
            raise AdmissionError(f"verdict must be approved|rejected: "
                                 f"{verdict}")
        if decided_by != "master":
            raise AdmissionError("decided_by must be master")
        with self.writer.transaction() as c:
            conn = conn or c
            row = conn.execute(
                "SELECT state_json FROM records WHERE record_family='approval'"
                " AND record_id=?", (approval_id,)).fetchone()
            if row is None:
                raise AdmissionError(f"approval not found: {approval_id}")
            st = canon.loads(row["state_json"])
            if st.get("status") != "pending":
                return {"status": st["status"], "stale": True,
                        "detail": "already decided"}
            # gate.inputs 再計算（candidate が pin した inputs と現状の照合）
            target = st.get("target") or {}
            if target.get("kind") == "candidate" and target.get("ref"):
                crow = conn.execute(
                    "SELECT state_json FROM candidates WHERE candidate_id=?",
                    (target["ref"],)).fetchone()
                if crow:
                    ctx = self._build_ctx(conn)
                    cst = canon.loads(crow["state_json"])
                    fresh = admit.gate_inputs_fresh(cst, ctx)
                    if canon.dumps(fresh) != canon.dumps(
                            cst.get("gate_inputs") or {}):
                        return {"status": "stale", "rejected": True,
                                "detail": "gate.inputs drifted; re-present"}

            now = self._clock()
            st = dict(st, status=verdict, decided_by=decided_by,
                      decided_at=now, updated_at=now,
                      record_version=st["record_version"] + 1)
            self.writer._append_locked(
                "approval",
                {"approval_id": approval_id, "action": "decide",
                 "verdict": verdict, "decided_by": decided_by},
                actor="master")
            self.writer.apply_mutation(
                "record_update",
                {"record_family": "approval", "record_id": approval_id,
                 "op": "decide", "new_state": st,
                 "reason": f"decided_by={decided_by}"},
                actor="system")
            return {"status": verdict, "approval_id": approval_id}

    def on_master_message(self, text: str, *, registered_after_seq=None):
        """会話内承認照合（write-path §5.3）。

        pending approval のうち最も古いものに対し、直後の master message を
        語彙照合。不成立なら None（pending 残留）。成立すれば decide を刻み、
        対象 candidate の admission を再駆動する。"""
        with self.writer.transaction() as conn:
            row = conn.execute(
                "SELECT record_id, state_json FROM records"
                " WHERE record_family='approval' AND status='pending'"
                " ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            st = canon.loads(row["state_json"])
            verdict = appr_kernel.match_decision(text)
            if verdict is None:
                return {"approval_id": st["record_id"], "matched": False}
            res = self.decide(st["record_id"], verdict, "master", conn=conn)
            out = {"approval_id": st["record_id"], "matched": True,
                   "decision": res}
            if res.get("status") == "approved" and \
                    (st.get("target") or {}).get("kind") == "candidate":
                out["promotion"] = self._promote_candidate(
                    conn, (st["target"] or {}).get("ref"))
            return out

    def _promote_candidate(self, conn, cand_id):
        """承認済み candidate の proposal を再 admission して昇格させる。"""
        row = conn.execute(
            "SELECT state_json FROM candidates WHERE candidate_id=?",
            (cand_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "candidate missing"}
        cst = canon.loads(row["state_json"])
        if cst.get("status") != "pending":
            return {"ok": False, "status": cst.get("status")}
        proposal = cst.get("proposal") or {}
        # 昇格時にも gate.inputs を再計算して drift 検知（§4.5）
        ctx = self._build_ctx(conn)
        fresh = admit.gate_inputs_fresh(cst, ctx)
        if canon.dumps(fresh) != canon.dumps(cst.get("gate_inputs") or {}):
            return {"ok": False, "stale": True}
        v = admit.evaluate(proposal, ctx)
        # マスター決裁済み: fail 級（reject/quarantine/stale）のみ阻止し、
        # pending 級（review_required 等の「人間判断待ち」）は決裁が
        # 判断を果たしたとして昇格させる（write-path §5.3）。
        if v["verdict"] in ("reject", "quarantine", "stale"):
            return {"ok": False, "verdict": v["verdict"]}
        mut = None
        if v["verdict"] != "noop_duplicate":
            mut = self._apply_accept(proposal, proposal.get("new_state")
                                     or {}, v, self._clock())
        # candidate の状態遷移も必ず mutation イベントで刻む（REQ-D2。
        # 直接 UPDATE は replay 照合を壊すため禁止）
        now = self._clock()
        approved = dict(cst, status="approved", updated_at=now,
                        record_version=cst["record_version"] + 1)
        self.writer.apply_mutation(
            "record_update",
            {"record_family": "candidate", "record_id": cand_id,
             "op": "approve", "new_state": approved,
             "reason": "approved via approval decision"},
            actor="system")
        return {"ok": True, "mutation": mut,
                "verdict_on_recheck": v["verdict"]}
