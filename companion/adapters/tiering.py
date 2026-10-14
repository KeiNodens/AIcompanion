"""adapters.tiering —— L-TIER の cycle 実行（FA §6.2）

夜間 reflection と同周期の tier 評価 cycle。各 scope について:

- tier_policies の policy を読み（無ければ kernel 既定値を登録）
- tier_cycles に cycle_id を INSERT（PK 衝突 = 実行済みで冪等スキップ）
- scope の active memory record を走査し kernel.tier.evaluate で評価
- tier_eval（運用表）に desired/streak を滞留（hysteresis の連続判定）
- 遷移確定したものだけ proposal（param_update＋cause 刻印）
- archive 行きは tier_archive_pending check が強制 pending にするので
  ここでは proposal を出すだけ —— apply は翌朝承認面の決裁に委ねる

dwell は state.tier_cycle_no（直前遷移の cycle 番号）で判定し、
遷移が commit されたら新しい値を new_state に刻む。
"""

import logging
from datetime import datetime, timezone

from ..kernel import canon, tier

log = logging.getLogger("companion")


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


def _cycle_no(day: str) -> int:
    """cycle 番号 = 日の通算（YYYY-MM-DD → ordinal）。"""
    try:
        return datetime.strptime(str(day)[:10], "%Y-%m-%d").toordinal()
    except ValueError:
        return 0


class TierCycle:
    """scope 別 tier policy の登録・cycle 実行・発見性計測を担う。"""

    def __init__(self, writer, admission, *, clock=None):
        self.writer = writer
        self.admission = admission
        self._clock = clock or _now

    # ---------------------------------------------------------- policy
    def install_defaults(self) -> dict:
        """scope 既定 policy を tier_policies に upsert する（初回構築）。"""
        now = self._clock()
        done = {}
        with self.writer.transaction() as conn:
            for scope, pol in tier.DEFAULT_TIER_POLICIES.items():
                dig = tier.policy_digest(pol)
                conn.execute(
                    "INSERT INTO tier_policies(scope, policy_json,"
                    " policy_digest, updated_at) VALUES (?,?,?,?)"
                    " ON CONFLICT(scope) DO UPDATE SET"
                    " policy_json=excluded.policy_json,"
                    " policy_digest=excluded.policy_digest,"
                    " updated_at=excluded.updated_at",
                    (scope, canon.dumps(pol), dig, now))
                done[scope] = dig
        return done

    def policy_for(self, conn, scope: str) -> dict:
        r = conn.execute(
            "SELECT policy_json FROM tier_policies WHERE scope=?",
            (scope,)).fetchone()
        if r:
            return canon.loads(r["policy_json"])
        return dict(tier.DEFAULT_TIER_POLICIES.get(
            scope, tier.DEFAULT_TIER_POLICIES["user"]))

    # ----------------------------------------------------------- cycle
    def run(self, *, scope: str, day: str | None = None) -> dict:
        """1 scope の tier cycle 実行。cycle_id 冪等（同日再実行は no-op）。

        戻り値: {cycle_id, scope, evaluated, streak_updates, transitions,
                 archive_pending, skipped, idempotent?}
        """
        day = day or self._clock()[:10]
        cid = tier.cycle_id_for(day)
        cno = _cycle_no(day)
        now = self._clock()
        out = {"cycle_id": cid, "scope": scope, "evaluated": 0,
               "streak_updates": 0, "transitions": 0,
               "archive_pending": 0, "skipped": 0, "proposals": []}

        with self.writer.transaction() as conn:
            # 冪等: 同一 cycle_id+scope の run 刻印があれば即座に返す
            dup = conn.execute(
                "SELECT 1 FROM tier_cycles WHERE cycle_id=? AND scope=?",
                (cid, scope)).fetchone()
            if dup:
                out["idempotent"] = True
                return out
            conn.execute(
                "INSERT INTO tier_cycles(cycle_id, scope, ran_at,"
                " counts_json) VALUES (?,?,?,NULL)",
                (cid, scope, now))
            pol = self.policy_for(conn, scope)
            if not pol.get("enabled"):
                out["disabled"] = True
                conn.execute(
                    "UPDATE tier_cycles SET counts_json=? WHERE cycle_id=?"
                    " AND scope=?", (canon.dumps(out), cid, scope))
                return out

            evals = {r["record_id"]: dict(r) for r in conn.execute(
                "SELECT record_id, desired_tier, streak FROM tier_eval"
                " WHERE scope=?", (scope,))}
            records = [canon.loads(r["state_json"]) for r in conn.execute(
                "SELECT state_json FROM records WHERE scope=?"
                " AND record_family='memory' AND status='active'",
                (scope,))]

        # ---- 評価（tx の外で決定的判定 —— proposal submit が別 tx を張る）
        eval_rows = []     # (state, eval_result) — tier_eval 書戻し用
        proposals = []     # (state, eval_result) — submit 用
        for st in records:
            rid = st["record_id"]
            ev = tier.evaluate(st, pol, cycle_no=cno,
                               eval_row=evals.get(rid))
            out["evaluated"] += 1
            eval_rows.append((st, ev))
            if ev["transition"]:
                proposals.append((st, ev))
            elif ev["streak"] != (evals.get(rid) or {}).get("streak"):
                out["streak_updates"] += 1

        with self.writer.transaction() as conn:
            for st, ev in eval_rows:
                conn.execute(
                    "INSERT INTO tier_eval(scope, record_id, desired_tier,"
                    " streak, last_cycle, updated_at) VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(scope, record_id) DO UPDATE SET"
                    " desired_tier=excluded.desired_tier,"
                    " streak=excluded.streak,"
                    " last_cycle=excluded.last_cycle,"
                    " updated_at=excluded.updated_at",
                    (scope, st["record_id"], ev["desired"],
                     ev["streak"], cid, now))
            conn.execute(
                "UPDATE tier_cycles SET counts_json=? WHERE cycle_id=?"
                " AND scope=?", (canon.dumps(
                    {"evaluated": out["evaluated"],
                     "proposals": len(proposals)}), cid, scope))

        # ---- 遷移 proposal（admission 経由。archive は kernel が強制 pending）
        for st, ev in proposals:
            to = ev["desired"]
            direction = ev["direction"]
            cause = ("decay_streak" if direction == "down"
                     else "context_reheat")
            new_state = dict(st)
            new_state["tier"] = to
            new_state["tier_cycle_no"] = cno
            new_state["tier_change"] = {
                "from": ev["current"], "to": to, "cause": cause,
                "cycle_id": cid, "streak": ev["streak"],
            }
            new_state["record_version"] = int(
                st.get("record_version") or 1) + 1
            new_state["updated_at"] = now
            prop = {
                "record_family": "memory", "op": "param_update",
                "target_id": st["record_id"], "new_state": new_state,
                "tier_change": {"from": ev["current"], "to": to,
                                "cause": cause, "cycle_id": cid},
                "reason": f"tier cycle {cid}: {ev['current']}→{to}",
                "basis_events": [],
                "generated_by": "tier_cycle", "producer": "tier_cycle",
                "generator": {"rule": tier.TIER_VERSION,
                              "policy_digest": tier.policy_digest(pol),
                              "cycle_id": cid},
            }
            try:
                res = self.admission.submit(prop, actor="system")
                out["proposals"].append({
                    "record_id": st["record_id"], "to": to,
                    "verdict": res.get("verdict")})
                if to == "archive" and res.get("verdict") == "pending":
                    out["archive_pending"] += 1
                else:
                    out["transitions"] += 1
            except Exception as e:
                log.warning("tier proposal failed %s: %s",
                            st["record_id"], e)
                out["skipped"] += 1
                out["proposals"].append({
                    "record_id": st["record_id"], "to": to,
                    "error": str(e)[:200]})
        return out

    # -------------------------------------------------- 発見性の計測
    def cue_report(self, derived, *, sample: int = 20) -> dict:
        """cue 索引・deep_recall の発見性計測（Q-O45 の記録面）。

        cold/archive の memory を sample 件取り、先頭12文字をクエリにして
        cue（mem_fts）検索で自 record が見つかるかを数える。
        見つからなければ deep_recall でも発見不能 —— 発見性の床。
        """
        acon, gen = derived.open_active()
        if acon is None:
            return {"ok": False, "reason": "no active derived"}
        try:
            cold = acon.execute(
                "SELECT record_id, norm_text FROM mem_search"
                " WHERE tier IN ('cold','archive') LIMIT ?",
                (sample,)).fetchall()
            total = acon.execute(
                "SELECT COUNT(*) FROM mem_search"
                " WHERE tier IN ('cold','archive')").fetchone()[0]
            hit, miss = 0, []
            for r in cold:
                probe = (r["norm_text"] or "")[:12]
                if not probe:
                    miss.append({"record_id": r["record_id"],
                                 "reason": "empty probe"})
                    continue
                terms = derived.fts_terms(probe)
                found = False
                if terms:
                    match = " OR ".join(f'"{t}"' for t in terms[:8])
                    rows = acon.execute(
                        "SELECT record_id FROM mem_search WHERE rowid IN"
                        " (SELECT rowid FROM mem_fts WHERE mem_fts MATCH ?)"
                        " LIMIT 50", (match,)).fetchall()
                    found = any(x["record_id"] == r["record_id"]
                                for x in rows)
                else:   # 3文字未満は LIKE fallback
                    found = bool(acon.execute(
                        "SELECT 1 FROM mem_search WHERE norm_text LIKE ?"
                        " AND record_id=?",
                        (f"%{probe}%", r["record_id"])).fetchone())
                if found:
                    hit += 1
                else:
                    miss.append({"record_id": r["record_id"],
                                 "reason": "cue miss"})
            n = len(cold)
            return {"ok": True, "gen": gen, "cold_archive_total": total,
                    "sampled": n, "cue_hits": hit,
                    "hit_rate": (hit / n) if n else 1.0,
                    "misses": miss[:20]}
        finally:
            acon.close()
