"""kernel.supply —— 供給面の純粋規則（read-path.md §4-§6）

- `supply_admit`: 供給経路の per-item 判定（SD-71/SD-47）。全供給経路
  （構造参照・recall・link 展開・deep_recall）が通る唯一の項目ゲート。
- `abstain_reason`: abstention の複合判定（SD-53）。
  「top dense < 閾値 かつ lexical 一致ゼロ」で no_match|below_threshold。
- `score_v1`: 候補 union 後の決定的リランク式（規則版 pin 対象 ——
  kernel_table に ("recall_score","score-v1") として登録される）。
- `assembly_payload`: context_assembly receipt の payload 組立て。

スコアリング係数・abstention 閾値は実装時パラメータ（Q-O166）。
RECEIPT_REQUIRED_PURPOSES 以外の purpose は供給経路に乗せない。
"""

from . import canon

SUPPLY_VERSION = "supply-v1"
SCORE_RULE_ID = "score-v1"

# score-v1 リランク係数（dense 主軸 + lexical 担保 + 揮発 strength の順）
SCORE_PARAMS = {
    "w_dense": 0.75,      # vec0 cosine 類似度（degraded: BLOB cosine）
    "w_lex": 0.15,        # fts_or の bm25 正規化値（0..1）
    "w_strength": 0.10,   # retrieval_strength（想起頻度の揮発値）
}
# abstention: top dense 類似度がこの floor 未満 かつ lexical 一致ゼロで棄権
ABSTAIN_DENSE_FLOOR = 0.45
# dense 不在（embedder 無し/vec0 不在）時は lexical のみで判定
RECALL_TOPK = 15

# supply 経路に乗せる purpose（SD-08）。inspect/as_of/browse は供給ではない。
# deep_recall は cold/archive を含む明示検索（cue 索引走査 —— FA §6.2）。
PURPOSES = frozenset({
    "turn", "reflect", "admin", "verify", "import", "migration", "dual_run",
    "deep_recall",
})

# recall 既定で見える status（read-path §5）
RECALL_VISIBLE_STATUS = frozenset({"active"})

# 既定 recall が見える tier（cold/archive は vec0 メンバーシップを持たず
# 既定供給からも除外。deep_recall は全 tier に広げる）
DEFAULT_VISIBLE_TIERS = frozenset({"hot", "warm"})
ALL_TIERS = frozenset({"hot", "warm", "cold", "archive"})


def supply_admit(state: dict, *, scope: str, hidden_path: bool = False,
                 visible_tiers=None):
    """per-item 供給判定。戻り値 (ok, omitted_code or None)。

    omitted_code（receipt の omitted 計数キーに対応）:
      scope_blocked / status_filtered / sensitivity_blocked / tier_filtered

    visible_tiers: 供給してよい tier 集合（None=hot/warm 既定。
    tier 未刻印の record は hot 扱い —— 全 hot 既定の回帰を守る）。
    """
    if state.get("scope") != scope:
        return False, "scope_blocked"
    if state.get("status") not in RECALL_VISIBLE_STATUS:
        return False, "status_filtered"
    vt = visible_tiers if visible_tiers is not None \
        else DEFAULT_VISIBLE_TIERS
    if (state.get("tier") or "hot") not in vt:
        return False, "tier_filtered"
    sens = state.get("sensitivity", "normal")
    if sens == "hidden" and not hidden_path:
        return False, "sensitivity_blocked"
    if sens == "sensitive" and not hidden_path:
        # sensitive は「scope 制約付き経路のみ供給」（SD-24）。
        # supply API は常に scope 明示のため通るが、receipt 側で区別する。
        return True, "sensitive_supplied"
    return True, None


def abstain_reason(candidates) -> str | None:
    """棄権判定。候補（[{dense_sim, lexical}] 辞書列）から理由語を返す。
    棄権しない（供給する）場合は None。

    - 候補ゼロ         → "no_match"
    - top dense < floor かつ lexical 一致ゼロ → "below_threshold"
      （dense 値が無い候補のみの場合も lexical 一致ゼロなら below_threshold）
    """
    if not candidates:
        return "no_match"
    top_dense = max((c.get("dense_sim") or 0.0) for c in candidates)
    lexical_hit = any(c.get("lexical") for c in candidates)
    if top_dense < ABSTAIN_DENSE_FLOOR and not lexical_hit:
        return "below_threshold"
    return None


def score_v1(*, dense_sim=None, lex_norm=None, retrieval_strength=None,
             params=None) -> float:
    """score-v1 リランク式。決定的・外部状態不参照。

    dense_sim: vec0 cosine 類似度（0..1、degraded 経路は BLOB cosine）
    lex_norm:  fts_or bm25 の正規化値（0..1 正規化済み）
    retrieval_strength: canonical の揮発値（0..1 想定・再計算は P8-4）
    """
    p = params or SCORE_PARAMS
    d = max(0.0, min(1.0, dense_sim or 0.0))
    l = max(0.0, min(1.0, lex_norm or 0.0))
    s = max(0.0, min(1.0, retrieval_strength or 0.0))
    return (p["w_dense"] * d) + (p["w_lex"] * l) + (p["w_strength"] * s)


def score_params_digest(params=None) -> str:
    """kernel_table/policy_ref pin 用の規則 digest。"""
    return canon.hash_obj("pcore.score_rule.v1",
                          {"rule": SCORE_RULE_ID, "params": params
                           or SCORE_PARAMS,
                           "abstain_dense_floor": ABSTAIN_DENSE_FLOOR})


# ------------------------------------------------------- receipt 組立て
_OMIT_KEYS = (
    "budget_dropped", "threshold_dropped", "scope_blocked",
    "stale_withheld", "sensitivity_blocked", "link_expansion_capped",
    "tier_filtered",
)


def assembly_payload(*, injected, omitted, purpose, consumer, basis_seq,
                     canon_algo, derived_gen, via, scopes, query,
                     inputs_hash, policy_ref=None, policy_hash=None,
                     pending_debt=None) -> dict:
    """context_assembly receipt payload の組立て（read-path §6 型）。

    injected: [{type, id, record_ids?}]
    omitted:  {キー: 件数}（_OMIT_KEYS 以外のキーは落とさずに残す）
    """
    om = {k: int(omitted.get(k, 0)) for k in _OMIT_KEYS}
    for k, v in (omitted or {}).items():
        if k not in om:
            om[k] = int(v)
    return {
        "injected": injected,
        "omitted": om,
        "purpose": purpose,
        "consumer": consumer,
        "inputs_hash": inputs_hash,
        "policy_ref": policy_ref,
        "policy_hash": policy_hash,
        "basis_seq": basis_seq,
        "canon_algo": canon_algo,
        "derived_gen": derived_gen,
        "via": via,
        "pending_debt": pending_debt,
        "scope": list(scopes),
        "query": query,
    }
