"""adapters.cognitive —— 認知機構の不純な orchestration（evolution §3-§6）

- `register_rules`: 版付き規則を kernel_table/policy_state に pin。
  版が変わったら `policy_change` イベントを刻印（D-D2）。
- `decay_sweep`: 全 active・非 pinned memory の retrieval_strength を
  規則版で導出し、min_delta 超なら `memory_update{op:param_update}`・
  床到達なら `op:demote` の proposal を admission 経由で発行
  （規則駆動 producer = review 免除・SD-72）。
- `boost` は書かない: context_assembly receipt が anchor 記録を兼ねる
  （evolution §3.2）。読み時導出 `strength_at` が正値。
- `reflection_check`: 累積 importance ≥ 閾値で `reflection_trigger`
  イベント（SD-79。夜間 reflect.py とは併存・canonical 側の事象駆動）。
- `suspect_scan`: 受理直後 memory の同 subject+type 近傍に対して
  suspector callback（LLM 差替え可）を呼び suspicion proposal を発行
  （Q-O66 初期案: 受理時限定・夜間は走らない・max_pairs cap）。
- `prediction_sweep`: horizon 到来の pending prediction を
  auto-resolve proposal（deterministic 採点のみ機械解決・SD-78）。
- `project_inner`: state レコード基線＋state_change イベント群から
  「今の気分」を投影（時間復帰は投影規則・偽イベントは発行しない）。
- `simulate`: 規則版 a/b の差分レポート（書き込まない試算）。

発火主体の棲み分け（AGENTS.md 発火表参照）: 現行 jobs.py は live path、
本モジュールは canonical 側の規則駆動 producer。live 配線は P8-B 以降。
"""

import json
from datetime import datetime, timezone

from ..kernel import canon, rules

PRODUCER_DECAY = "decay_sweep"
PRODUCER_SUSPECT = "suspect"
PRODUCER_PREDICT = "prediction_sweep"


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


class CognitiveJobs:
    def __init__(self, writer, admission, *, clock=None):
        self.writer = writer
        self.admission = admission
        self._clock = clock or _now
        self._rules_ready = False

    def _ensure_rules(self):
        """規則が pin 済みでなければ登録する（冪等）。

        param_policy_ref は policy_state 未登録だと admission が
        pending にするので、sweep 系は実行前に必ず pin を確認する。
        """
        if not self._rules_ready:
            self.register_rules()
            self._rules_ready = True

    # ------------------------------------------------- 規則版 registry
    def register_rules(self) -> dict:
        """RULES を kernel_table + policy_state に pin。

        version 変化は `policy_change` イベントで刻印（as-of 読みが
        seq 時点の版で解釈できるようイベントが権威・evolution §7）。
        """
        manifest = rules.rules_manifest()
        now = self._clock()
        head = self.writer._conn.execute(
            "SELECT MAX(seq) FROM events").fetchone()[0] or 0
        stamped = []
        with self.writer.transaction() as conn:
            for rid, m in manifest.items():
                conn.execute(
                    "INSERT INTO kernel_table(algo, version, digest,"
                    " params_json, registered_seq, registered_at)"
                    " VALUES (?,?,?,?,?,?) ON CONFLICT(algo, version)"
                    " DO NOTHING",
                    (f"rule:{rid}", m["version"], m["digest"],
                     canon.dumps(m["params"]), head, now))
                cur = conn.execute(
                    "SELECT version FROM policy_state WHERE rule_id=?",
                    (rid,)).fetchone()
                if cur is None:
                    conn.execute(
                        "INSERT INTO policy_state(rule_id, version,"
                        " params_json, content_digest, activated_seq,"
                        " updated_at) VALUES (?,?,?,?,?,?)",
                        (rid, m["version"], canon.dumps(m["params"]),
                         m["digest"], head, now))
                    stamped.append({"rule": rid, "from": None,
                                    "to": m["version"]})
                elif cur["version"] != m["version"]:
                    conn.execute(
                        "UPDATE policy_state SET version=?, params_json=?,"
                        " content_digest=?, activated_seq=?, updated_at=?"
                        " WHERE rule_id=?",
                        (m["version"], canon.dumps(m["params"]),
                         m["digest"], head, now, rid))
                    stamped.append({"rule": rid, "from": cur["version"],
                                    "to": m["version"]})
            for ch in stamped:
                self.writer._append_locked(
                    "policy_change",
                    {"rule": ch["rule"], "from_version": ch["from"],
                     "to_version": ch["to"],
                     "params": manifest[ch["rule"]]["params"],
                     "digest": manifest[ch["rule"]]["digest"]},
                    actor="system")
        return {"stamped": stamped}

    # ------------------------------------------------- receipt → boost anchor
    def _assembly_rows(self, since_seq: int = 0):
        for r in self.writer._conn.execute(
                "SELECT seq, payload_json, occurred_at, recorded_at"
                " FROM events WHERE event_type='context_assembly'"
                " AND seq>? ORDER BY seq", (since_seq,)):
            yield r["seq"], canon.loads(r["payload_json"]), \
                r["occurred_at"] or r["recorded_at"]

    def boost_times_for(self, record_id: str, scope: str,
                        since_ts: str = None) -> list:
        """record を実注入した context_assembly の時刻列（SD-10 適格性済み）。"""
        out = []
        since = since_ts
        for _seq, pl, ts in self._assembly_rows():
            if not rules.boost_eligible(pl, scope):
                continue
            if record_id in rules.injected_memory_ids(pl):
                if since is None or ts > since:
                    out.append(ts)
        return out

    def strength_at(self, state: dict, at_ts: str = None) -> float:
        """読み時導出の正値（anchor + receipt boost + 経過減衰）。"""
        at = at_ts or self._clock()
        boosts = self.boost_times_for(
            state["record_id"], state.get("scope", "user"),
            since_ts=state.get("last_recalled_at"))
        return rules.effective_strength(state, boosts, at)

    # ------------------------------------------------- decay sweep
    def decay_sweep(self, at_ts: str = None) -> dict:
        """決定的 sweep。param_update/demote の proposal を admission へ。

        戻り値 {stamped, demoted, skipped, proposals}。規則駆動 producer
        なので review 免除（admit.RULE_PRODUCERS）。
        """
        self._ensure_rules()
        at = at_ts or self._clock()
        out = {"stamped": 0, "demoted": 0, "skipped": 0, "proposals": []}
        for r in self.writer._conn.execute(
                "SELECT state_json FROM records WHERE record_family='memory'"
                " AND status='active' AND pinned=0"):
            st = canon.loads(r["state_json"])
            derived = self.strength_at(st, at)
            if rules.demote_check(derived):
                op, new_st = "demote", dict(st)
                new_st["status"] = "demoted"
                new_st["retrieval_strength"] = round(derived, 6)
            elif rules.stamp_needed(st.get("retrieval_strength"), derived):
                op, new_st = "param_update", dict(st)
                new_st["retrieval_strength"] = round(derived, 6)
            else:
                out["skipped"] += 1
                continue
            new_st["param_policy_ref"] = rules.version("decay")
            new_st["record_version"] = st["record_version"] + 1
            new_st["updated_at"] = at
            prop = {
                "record_family": "memory", "op": op,
                "target_id": st["record_id"], "new_state": new_st,
                "producer": PRODUCER_DECAY,
                "generated_by": "decay_sweep",
                "generator": {"method": "rules", "model": "none",
                              "version": rules.version("decay")},
                "reason": f"decay sweep: strength→{derived:.4f}",
                "basis_seq": None,
            }
            res = self.admission.submit(prop, actor="system")
            out["proposals"].append({"record_id": st["record_id"],
                                     "op": op, "verdict": res["verdict"]})
            if res["verdict"] == "accept":
                out["demoted" if op == "demote" else "stamped"] += 1
        return out

    # ------------------------------------------------- reflection（SD-79）
    def reflection_check(self) -> dict:
        """最後の reflection_trigger 以後の注入 importance 累積が閾値超なら
        reflection_trigger イベントを刻印。"""
        last = self.writer._conn.execute(
            "SELECT MAX(seq) FROM events WHERE"
            " event_type='reflection_trigger'").fetchone()[0] or 0
        acc, ids = 0.0, set()
        for _seq, pl, _ts in self._assembly_rows(last):
            for mid in rules.injected_memory_ids(pl):
                if mid in ids:
                    continue
                ids.add(mid)
                row = self.writer._conn.execute(
                    "SELECT importance FROM records WHERE record_id=?",
                    (mid,)).fetchone()
                if row and row["importance"]:
                    acc += row["importance"]
        if not rules.reflection_due(acc):
            return {"fired": False, "accumulated": acc}
        rec = self.writer.append_event(
            "reflection_trigger",
            {"kind": "cumulative_importance",
             "accumulated": acc, "threshold":
             rules.params("reflect")["importance_threshold"],
             "since_seq": last,
             "policy": rules.version("reflect")},
            actor="system")
        return {"fired": True, "accumulated": acc, "event": rec}

    # ------------------------------------------------- suspect lane（Q-O66）
    def suspect_scan(self, new_state: dict, suspector) -> dict:
        """新規受理 memory の近傍を suspector callback で検査。

        suspector(new_state, neighbor_state) -> suspicion dict | None。
        suspicion は {"suspect_of": rid, "claim": ..., "reason": ...}。
        発火条件・対象範囲は suspect-v1 規則（受理時限定・近傍 cap）。
        """
        sp = rules.params("suspect")
        rows = self.writer._conn.execute(
            "SELECT state_json FROM records WHERE record_family='memory'"
            " AND scope=? AND status='active'",
            (new_state.get("scope") or "user",)).fetchall()
        neigh = [canon.loads(r["state_json"]) for r in rows]
        pairs = rules.cap_pairs(
            [n for n in neigh
             if rules.suspect_in_scope(new_state, n)], sp)
        out = {"scanned": len(pairs), "proposals": []}
        for n in pairs:
            susp = suspector(new_state, n)
            if not susp:
                continue
            # schema_shape は完全スナップショットを要求するため、
            # suspector が new_state を返さない場合は近傍の現状写し
            # （review_hint=True で必ず pending へ流れ、承認されなければ
            # mutation は確定しない —— 疑いの表明が目的）
            ns = susp.get("new_state") or {
                **n, "record_version": n["record_version"] + 1,
                "updated_at": self._clock()}
            prop = {
                "record_family": "memory",
                "target_id": n["record_id"],
                "new_state": ns,
                "producer": PRODUCER_SUSPECT,
                "generated_by": "suspect",
                "generator": susp.get("generator") or
                {"method": "suspector", "model": "injected",
                 "version": rules.version("suspect")},
                "review_hint": True,
                "reason": susp.get("reason",
                                   "suspect lane: 近傍との疑い"),
                "suspicion": {"suspect_of": n["record_id"],
                              "claim": susp.get("claim")},
            }
            res = self.admission.submit(prop, actor="system")
            out["proposals"].append({"target": n["record_id"],
                                     "verdict": res["verdict"]})
        return out

    # ------------------------------------------------- prediction（SD-78）
    def prediction_sweep(self, at_ts: str = None) -> dict:
        """horizon 到来の pending prediction を auto-resolve。

        deterministic 採点のみ機械解決（verdict 等は人間採点で残す）。
        """
        self._ensure_rules()
        at = at_ts or self._clock()
        out = {"resolved": 0, "pending": 0, "proposals": []}
        for r in self.writer._conn.execute(
                "SELECT state_json FROM records WHERE"
                " record_family='prediction'"):
            st = canon.loads(r["state_json"])
            if not rules.prediction_due(st, at):
                if st.get("outcome") == "pending":
                    out["pending"] += 1
                continue
            if not rules.prediction_scoring_ok(st.get("scoring", "")):
                out["pending"] += 1
                continue
            # deterministic 採点: claim.outcome_check が宣言した値と
            # 現在値の一致で hit/miss（check 自体は呼び出し側評価 —
            # v1 では expected==actual の字句一致判定）
            check = (st.get("claim") or {}).get("outcome_check") or {}
            outcome = "hit" if check.get("expected") == \
                check.get("actual") else "miss"
            new_st = dict(st)
            new_st["outcome"] = outcome
            new_st["resolved_at"] = at
            new_st["record_version"] = st["record_version"] + 1
            new_st["updated_at"] = at
            new_st["param_policy_ref"] = rules.version("prediction")
            prop = {
                "record_family": "prediction", "op": "resolve",
                "target_id": st["record_id"], "new_state": new_st,
                "producer": PRODUCER_PREDICT,
                "generated_by": "prediction_sweep",
                "generator": {"method": "rules", "model": "none",
                              "version": rules.version("prediction")},
                "reason": f"horizon reached → {outcome}",
            }
            res = self.admission.submit(prop, actor="system")
            out["proposals"].append({"record_id": st["record_id"],
                                     "outcome": outcome,
                                     "verdict": res["verdict"]})
            if res["verdict"] == "accept":
                out["resolved"] += 1
        return out

    # ------------------------------------------------- state 投影（D-D3）
    def project_inner(self, scope: str = "agent",
                      at_ts: str = None) -> dict:
        """state レコードの baseline + state_change 群から現在の気分を投影。

        state レコードは kind/key='inner'・baseline を state dict に持つ。
        """
        at = at_ts or self._clock()
        row = self.writer._conn.execute(
            "SELECT state_json FROM records WHERE record_family='state'"
            " AND scope=? AND kind='inner'", (scope,)).fetchone()
        baseline = {}
        if row:
            baseline = (canon.loads(row["state_json"]).get("baseline")
                        or {})
        changes = []
        for r in self.writer._conn.execute(
                "SELECT occurred_at, payload_json FROM events WHERE"
                " event_type='state_change' ORDER BY seq"):
            pl = canon.loads(r["payload_json"])
            if (pl.get("key") or pl.get("state_key")) == "inner" \
                    and pl.get("scope", scope) == scope:
                changes.append((r["occurred_at"], pl.get("value") or {}))
        return rules.project_inner(baseline, changes, at)

    # ------------------------------------------------- simulate（差分試算）
    def simulate(self, params_b: dict, at_ts: str = None,
                 scope: str = None) -> dict:
        """decay 規則の現行 vs 候補 params の活性化前差分レポート。

        書き込まない（memory.simulate_decay と同型の試算面）。
        """
        at = at_ts or self._clock()
        pa = rules.params("decay")
        states, boosts_map = [], {}
        sql = ("SELECT state_json FROM records WHERE record_family='memory'"
               " AND status='active' AND pinned=0")
        args = []
        if scope:
            sql += " AND scope=?"
            args.append(scope)
        for r in self.writer._conn.execute(sql, args):
            st = canon.loads(r["state_json"])
            states.append(st)
            boosts_map[st["record_id"]] = self.boost_times_for(
                st["record_id"], st.get("scope", "user"),
                since_ts=st.get("last_recalled_at"))
        rep = rules.simulate_strengths(states, boosts_map, at, pa, params_b)
        rep["rule"] = "decay"
        rep["from_params"] = pa
        rep["to_params"] = params_b
        rep["evaluated_at"] = at
        return rep
