"""adapters.consolidate —— 近重複記憶の統合 producer（C-5、Hindsight
observations に相当する工程の canonical 版）

夜間 cycle で scope の active memory を走査し、本文がほぼ同じ記憶の
ペアを検出する。見つかったペアは「勝者を残して敗者を merged 状態に
遷移させる」merge 提案を admission に流す —— ここで直接 records を
書き換えないのが canonical の約束（candidate→admission→承認の正規
ルートを通る）。

設計:

- producer="consolidation"（規則駆動語彙 RULE_PRODUCERS に「含めない」
  選択 —— 記憶を消す側の操作なので証拠資格・concreteness・
  review.required の門を全て通る。importance 4+ の記憶や basis の無い
  記憶は pending/quarantine に流れて人間の目に届く）
- 検出は決定的: kernel.admit.trigram_jaccard（tombstone 類似照合と
  同じ尺度）≥ SIMILARITY。LLM 不使用
- 冪等: cycle_id=con-YYYY-MM-DD を tier_cycles に刻み同日再実行は no-op
- survivor 選定: importance → retrieval_strength → valid_from の順で
  高い方。pinned record は敗者にしない（残すべき印を尊重）
- 証跡: loser の provenance.source_events を basis_events として引く
  （basis が空の記憶は辿れないので提案自体を出さない）
"""

import logging
import os
from datetime import datetime, timezone

from ..kernel import admit, canon

log = logging.getLogger("companion")

CONSOLIDATE_VERSION = "consolidate-v1"
_SIMILARITY = 0.8           # trigram jaccard 閾値（tombstone similar と同値）
_SCAN_LIMIT = 400           # 走査対象の active memory 上限（直近順）
_PAIR_LIMIT = 20            # 1 cycle で提案するペア上限


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


def _text_of(st: dict) -> str:
    return str(st.get("content") or st.get("text") or "")


def _basis_of(st: dict) -> list:
    return [b for b in ((st.get("provenance") or {})
                        .get("source_events") or [])
            if isinstance(b, dict) and b.get("event_id")]


def _survivor(a: dict, b: dict) -> tuple:
    """(winner, loser) の組を返す。優先: pinned > importance >
    retrieval_strength > valid_from/created_at。"""
    def key(st):
        return (1 if st.get("pinned") else 0,
                st.get("importance") or 0,
                st.get("retrieval_strength") or 0.0,
                st.get("valid_from") or st.get("created_at") or "")
    return (a, b) if key(a) >= key(b) else (b, a)


class Consolidation:
    """scope 別の統合 cycle。TierCycle と同じ発見→提案→台帳の型。"""

    def __init__(self, writer, admission, *, clock=None):
        self.writer = writer
        self.admission = admission
        self._clock = clock or _now

    def run(self, *, scope: str, day: str | None = None,
            similarity: float | None = None,
            scan_limit: int | None = None,
            pair_limit: int | None = None) -> dict:
        sim = similarity if similarity is not None else float(
            os.getenv("CONSOLIDATE_SIM", str(_SIMILARITY)))
        scan = scan_limit or int(
            os.getenv("CONSOLIDATE_SCAN", str(_SCAN_LIMIT)))
        plim = pair_limit or int(
            os.getenv("CONSOLIDATE_PAIRS", str(_PAIR_LIMIT)))
        day = day or self._clock()[:10]
        cid = f"con-{day}"
        now = self._clock()
        out = {"cycle_id": cid, "scope": scope, "scanned": 0,
               "pairs": 0, "proposed": 0, "skipped_no_basis": 0,
               "proposals": []}

        with self.writer.transaction() as conn:
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
            records = [canon.loads(r["state_json"]) for r in conn.execute(
                "SELECT state_json FROM records WHERE scope=?"
                " AND record_family='memory' AND status='active'"
                " ORDER BY updated_at DESC LIMIT ?", (scope, scan))]

        out["scanned"] = len(records)
        texts = {st["record_id"]: canon.norm_text(_text_of(st))
                 for st in records}
        by_id = {st["record_id"]: st for st in records}

        # ---- 近重複ペア検出（決定的 trigram jaccard・tx 外で計算）
        pairs = []
        ids = sorted(texts)
        for i, rid_a in enumerate(ids):
            if len(pairs) >= plim * 4:    # 先に多めに拾って後で絞る
                break
            for rid_b in ids[i + 1:]:
                st_a, st_b = by_id[rid_a], by_id[rid_b]
                if (st_a.get("sensitivity") or "normal") != \
                        (st_b.get("sensitivity") or "normal"):
                    continue   # 感度の違う記憶は統合しない
                if not texts[rid_a] or not texts[rid_b]:
                    continue
                j = admit.trigram_jaccard(texts[rid_a], texts[rid_b])
                if j >= sim:
                    pairs.append((j, rid_a, rid_b))
        pairs.sort(key=lambda p: -p[0])
        out["pairs"] = len(pairs)

        # ---- merge 提案（admission の門を通す）
        consumed = set()
        for j, rid_a, rid_b in pairs[:plim]:
            if rid_a in consumed or rid_b in consumed:
                continue
            winner, loser = _survivor(by_id[rid_a], by_id[rid_b])
            if loser.get("pinned"):
                continue   # pinned は敗者にしない（保険 —— key でも防ぐ）
            # basis は両記憶の source_events 和集合 —— loser 単独の
            # source だけだと basis_coverage（Q-O82）が「既受理」と
            # 判定して noop になり merge が適用されない
            basis, seen = [], set()
            for b in _basis_of(loser) + _basis_of(winner):
                if b["event_id"] not in seen:
                    seen.add(b["event_id"])
                    basis.append(b)
            if not basis:
                out["skipped_no_basis"] += 1
                consumed.add(loser["record_id"])
                continue
            new_state = dict(loser)
            new_state["status"] = "merged"
            new_state["merged_into"] = winner["record_id"]
            new_state["record_version"] = int(
                loser.get("record_version") or 1) + 1
            new_state["updated_at"] = now
            prop = {
                "record_family": "memory", "op": "merge",
                "target_id": loser["record_id"], "new_state": new_state,
                "expected_fingerprint": admit.fingerprint(loser),
                "basis_events": basis,
                "reason": f"consolidation {cid}: merge into "
                          f"{winner['record_id']} (j={j:.3f})",
                "generated_by": "consolidation",
                "producer": "consolidation",
                "generator": {"method": "trigram-jaccard",
                              "version": CONSOLIDATE_VERSION,
                              "similarity": round(j, 4),
                              "threshold": sim,
                              "survivor": winner["record_id"]},
            }
            try:
                res = self.admission.submit(prop, actor="system")
                out["proposed"] += 1
                out["proposals"].append({
                    "loser": loser["record_id"],
                    "survivor": winner["record_id"],
                    "similarity": round(j, 4),
                    "verdict": res.get("verdict")})
                consumed.add(loser["record_id"])
            except Exception as e:
                log.warning("consolidation proposal failed %s: %s",
                            loser["record_id"], e)
                out["proposals"].append({
                    "loser": loser["record_id"],
                    "error": str(e)[:200]})

        with self.writer.transaction() as conn:
            conn.execute(
                "UPDATE tier_cycles SET counts_json=? WHERE cycle_id=?"
                " AND scope=?",
                (canon.dumps({"scanned": out["scanned"],
                              "pairs": out["pairs"],
                              "proposed": out["proposed"]}),
                 cid, scope))
        return out
