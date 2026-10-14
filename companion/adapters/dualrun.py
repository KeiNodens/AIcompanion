"""adapters.dualrun —— L-MIG Phase 1 dual-run shadow の統括。

canonical 側は「影」として本物の記録を積み上げる（read 権威は mem0 のまま）。
このモジュールが担うのは:

- shadow pipeline: extraction → admission（実書込み・読み権威なし）
- mem0 verbatim 写し（infer=False）の seam 呼び出し点
- G1 投影一致: canonical が保持する記録と mem0 verbatim item の比較
- shadow 供給: supply_context(purpose="dual_run") の receipt 発行
- seal(): 起動ガード —— core.db 不接続なら mem0 書込み路を開かない
"""

import logging
from datetime import datetime, timezone

from ..kernel import canon, migration as mig
from ..kernel.writer import open_core

log = logging.getLogger("companion")


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


def verbatim_extractor(ev: dict) -> list:
    """dual-run の verbatim 抽出器。

    message/response event をそのまま memory{scope:user, type:episode}
    proposal に写す（mem0 verbatim infer=False と同じ原料を canonical
    が持つ形に揃える。本番の LLM 抽出器は配線時に差し替える）。
    """
    if ev.get("event_type") not in ("message", "response"):
        return []
    body = (ev.get("payload") or {}).get("body") or ""
    if not body.strip():
        return []
    now = canon.canon_ts(datetime.now(timezone.utc))
    rid = canon.hash_tuple("pcore.verbatim_mem.v1",
                           ev.get("event_id") or "?")
    return [{
        "record_family": "memory",
        "target_id": None,
        "new_state": {
            "record_id": f"mem_v_{rid[:20]}",
            "record_family": "memory",
            "scope": "user",
            "status": "active",
            "record_version": 1,
            "memory_type": "episode",
            "content": body,
            "content_hash": canon.content_hash(body),
            "sensitivity": "normal",
            "importance": 3,
            "retrieval_strength": 0.5,
            "tier": "hot",
            "pinned": False,
            "created_at": ev.get("occurred_at") or now,
            "updated_at": now,
        },
        "basis_events": [{"event_id": ev.get("event_id")}],
        "generated_by": "extraction",
        "producer": "extraction",
        "generator": {"method": "verbatim", "version": "dualrun-v1"},
    }]


def seal(path) -> object:
    """起動ガード: core.db が開けなければ raise（呼び出し側は mem0
    書込み路を開かない）。開ければ健全な writer を返す（sanity 内蔵）。"""
    return open_core(path)


class DualRun:
    """dual-run shadow のオーケストレーション。"""

    def __init__(self, writer, extraction, admission, supply=None, *,
                 mem0=None, clock=None):
        self.writer = writer
        self.extraction = extraction
        self.admission = admission
        self.supply = supply
        self.mem0 = mem0            # memory seam（verbatim store 用。None 可）
        self._clock = clock or _now
        self.report = {"proposals": 0, "accepted": 0, "pending": 0,
                       "quarantined": 0, "rejected": 0, "noop": 0,
                       "stale": 0, "errors": 0}

    # ------------------------------------------------- shadow pipeline
    def process_pending(self, limit=100) -> dict:
        """claim 滞留の抽出→admission(shadow)。verdict を集計して返す。

        proposal→admission は Extraction.run 内部で行われる
        （write-path: 供給は必ず検査列を通る）。ここでは累計 report を
        更新するだけ。"""
        res = self.extraction.run(limit=limit)
        for v, n in (res.get("verdicts") or {}).items():
            key = {"accept": "accepted", "pending": "pending",
                   "quarantine": "quarantined", "reject": "rejected",
                   "noop_duplicate": "noop", "stale": "stale"}.get(v)
            self.report[key if key else "errors"] += n
        self.report["proposals"] += res.get("proposed", 0)
        return {"scanned": res.get("scanned", 0),
                "proposed": res.get("proposed", 0),
                "verdicts": res.get("verdicts") or {},
                "report": dict(self.report)}

    # --------------------------------------------------- mem0 verbatim
    def store_verbatim(self, text: str, *, scope="user",
                       user_id=None, agent_id=None,
                       metadata=None) -> dict:
        """mem0 への verbatim 書込み（infer=False・seam 経由のみ）。"""
        if self.mem0 is None:
            return {"ok": False, "error": "mem0 seam not attached"}
        try:
            res = self.mem0.add(
                [{"role": "user", "content": text}],
                user_id=user_id, agent_id=agent_id,
                infer=False, metadata=metadata or {})
            return {"ok": True, "result": res}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    # ------------------------------------------------------ G1 投影一致
    def canonical_memories(self, conn, scope: str) -> list:
        """canonical 側の active memory record（比較対象の投影）。"""
        out = []
        for r in conn.execute(
                "SELECT state_json FROM records WHERE scope=?"
                " AND record_family='memory' AND status='active'",
                (scope,)):
            st = canon.loads(r["state_json"])
            out.append(st)
        return out

    def g1_report(self, mem0_items: list, scope: str, *,
                  sim_threshold=0.8) -> dict:
        """G1 投影一致: mem0 verbatim item が canonical 側に写っているか。

        mem0_items: seam.get_all で取った生 item dict の列。
        """
        with self.writer.transaction() as conn:
            canon_items = self.canonical_memories(conn, scope)
        cov = mig.equiv_coverage(mem0_items, canon_items,
                                 sim_threshold=sim_threshold)
        return {"ok": cov["coverage"] >= 1.0, "scope": scope,
                "mem0_total": cov["total"], "covered": cov["covered"],
                "coverage": cov["coverage"], "missed": cov["missed"],
                "canonical_total": len(canon_items),
                "shadow_report": dict(self.report)}

    # ------------------------------------------------------ shadow 供給
    def shadow_supply(self, *, scopes, query=None) -> dict:
        """purpose=dual_run の供給 receipt を刻んで blocks を返す。"""
        if self.supply is None:
            return {"ok": False, "error": "supply not attached"}
        return self.supply.supply_context(
            scopes=scopes, query=query, purpose="dual_run")
