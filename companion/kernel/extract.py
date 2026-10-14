"""kernel.extract —— 抽出材料ゲートと claim 遷移の純粋規則（write-path §3）

Event Log → 抽出プロセッサの入口フィルタ（echo 防御・機械的許可リスト）と、
claims/claim_events の状態遷移規則を決定的に定義する。
LLM 抽出本体は adapter 側が注入する（kernel はゲートのみ）。

材料許可（write-path §3.2）:
  許可: conversation 系（message/response/inner）・ingest・登録済み外部取得系
        tool_result
  禁止: mutation 系・context_assembly・approval・write_proposal・
        candidate_review・policy_change・migration・archive・blob_register・
        job_transition・skipped_part・ingress_gap・その他運用系
さらに origin=injected（context_assembly と digest 突合で機械付与）・
companion_artifact 印・sanity_flag 付きは材料から除外する（汚染伝播）。

poison: 同一イベントで抽出が POISON_MAX_ATTEMPTS 回失敗したら
extraction_poisoned を刻印して水位を進める（「捨てないが故に止まる」を防ぐ）。
"""

EXTRACTABLE_TYPES = frozenset({
    "message", "response", "inner", "ingest", "tool_result",
})

# 材料から除外する origin（normalizer/注入突合が機械付与したもの）
BLOCKED_ORIGINS = frozenset({"injected", "imported"})

POISON_MAX_ATTEMPTS = 3

# claims.status 語彙（schema.py claims 表の CHECK ではなく設計語彙）
CLAIM_STATUS = frozenset({
    "claimed", "stored", "no_lesson", "cancelled", "stalled",
})

# 許容遷移（from → {to}）。claimed 以外からの再遷移は release 経路のみ
# （poison_release/quarantine_release は運用経路で cancelled→claimed 等を復帰
# させる —— ここでは通常遷移のみ定義）
_CLAIM_TRANSITIONS = {
    "claimed": {"stored", "no_lesson", "cancelled", "stalled"},
    "stalled": {"claimed"},           # poison_release での復帰
    "cancelled": {"claimed"},         # quarantine_release での復帰
}


def material_ok(event_desc: dict):
    """イベントが抽出材料か。戻り値 (ok, reason_code)。

    event_desc: {event_type, actor, origin?, companion_artifact?,
                 sanity_flag?} —— adapter が events 行から組み立てる描述子。
    """
    et = event_desc.get("event_type")
    if et not in EXTRACTABLE_TYPES:
        return False, "type_not_extractable"
    if (event_desc.get("origin") or "") in BLOCKED_ORIGINS:
        return False, "blocked_origin"
    if event_desc.get("companion_artifact"):
        return False, "companion_artifact"
    if event_desc.get("sanity_flag"):
        return False, "sanity_flag"
    return True, "ok"


def claim_transition_ok(from_status: str, to_status: str) -> bool:
    return to_status in _CLAIM_TRANSITIONS.get(from_status, set())


def poisoned(attempts: int, max_attempts: int = POISON_MAX_ATTEMPTS) -> bool:
    """試行回数が poison 閾値に達したか。"""
    return attempts >= max_attempts
