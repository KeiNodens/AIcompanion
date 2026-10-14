"""kernel.rules — 認知パラメータの版付き規則（evolution.md §3-§5）

decay/boost・state 投影・reflection_trigger・suspect lane・prediction の
決定的規則。全て時計注入の純粋関数（datetime.fromisoformat のみ使用、
now() は呼ばない）。規則版は `policy_state`/`kernel_table` に pin し、
変更は `policy_change` イベント＋版 bump の経路のみ（adapters/cognitive
が刻印を担う）。

揮発値モデル（SD-54・Q-O68 採用解釈）:
- records.state_json の retrieval_strength/last_recalled_at は
  「最後に打刻した anchor」。権威は events＋anchor＋規則版の導出。
- 想起 boost は context_assembly receipt の実注入 ids が anchor を動かす
  （mutation event は発行しない —— evolution §3.2）。
- decay sweep が確定値を `memory_update{op:param_update}` で刻印する。
"""

from . import canon
from datetime import datetime

# ------------------------------------------------- 版付き規則 registry
# params は policy_state.params_json に pin される運用パラメータ。
RULES = {
    "decay": {
        "version": "decay-v1",
        "params": {
            # 現行 memory.py の decay と同値（parity 維持）
            "weekly_factor": 0.9,
            "floor": 0.05,
            "grace_days_per_importance": 7.0,
            "grace_min_days": 7.0,
            "min_delta": 0.005,
        },
    },
    "boost": {
        "version": "boost-v1",
        "params": {
            # 現行 _boost_confidence と同型（s += rate*(1-s)）
            "rate": 0.1,
            "cap_per_day": 3,          # SD-10 期間 cap
            "scopes": ["agent"],        # SD-10 scope 既定 agent
            "purposes": ["turn"],       # boost 対象 purpose
            "basis": "search",          # recall 注入のみ（構造参照は除外）
        },
    },
    "state_project": {
        "version": "state-project-v1",
        "params": {
            # 現行 inner.py と同等: 線形復帰 k=min(hours/24,1)
            "return_hours": 24.0,
            # Q-O157 気分緩衝: 機構は導入、初版値は無緩衝（現行と等価）
            "ema_alpha": 1.0,
            "max_step": {"valence": 2, "energy": 100},
            "baseline": {"mood": "普通", "valence": 0, "energy": 60},
        },
    },
    "reflect": {
        "version": "reflect-v1",
        "params": {
            # 累積 importance トリガ（SD-79・generative-agents 系）
            "importance_threshold": 15,
        },
    },
    "suspect": {
        "version": "suspect-v1",
        "params": {
            # Q-O66 初期案: 新規受理時の同 subject+memory_type 近傍限定
            "max_pairs": 5,
            "same_subject": True,
            "same_memory_type": True,
        },
    },
    "prediction": {
        "version": "prediction-v1",
        "params": {
            # horizon 未指定時の既定（SD-78）
            "default_horizon_days": 7,
        },
    },
}

RULES_VERSION = "rules-v1"


def params(rule: str) -> dict:
    return dict(RULES[rule]["params"])


def version(rule: str) -> str:
    return RULES[rule]["version"]


# ------------------------------------------------- 時刻ユーティリティ
def _parse(ts):
    if ts is None:
        return None
    return datetime.fromisoformat(ts)


def _days(t0, t1) -> float:
    return (t1 - t0).total_seconds() / 86400.0


# ------------------------------------------------- decay-v1 / boost-v1
def decay_value(s: float, elapsed_days: float, importance: float,
                params: dict) -> float:
    """anchor 起点の減衰値。grace 内は不変・超過は週単位複利で floor まで。

    現行 memory.py と同型: grace 経過後は anchor からの全 elapsed に
    weekly_factor を掛ける（grace 控除ではない —— parity 維持）。
    """
    if s is None:
        s = 0.5
    imp = importance if importance else 1
    grace = max(params["grace_min_days"],
                imp * params["grace_days_per_importance"])
    if elapsed_days <= grace:
        return s
    v = s * (params["weekly_factor"] ** (elapsed_days / 7.0))
    return max(params["floor"], v)


def apply_boost(s: float, rate: float) -> float:
    """connectome 式の減衰つき成長（現行 _boost_confidence と同型）。"""
    return min(1.0, s + rate * (1.0 - s))


def effective_strength(state: dict, boost_ts, at_ts: str,
                       decay_params: dict = None,
                       boost_params: dict = None) -> float:
    """読み時導出の正値（anchor + 以後の receipt boost + 経過減衰）。

    boost_ts: context_assembly receipt がその記憶を実注入した時刻の列
    （呼び出し側が eligibility フィルタ済みのものを渡す）。
    """
    dp = decay_params or params("decay")
    bp = boost_params or params("boost")
    at = _parse(at_ts)
    s = state.get("retrieval_strength")
    if s is None:
        s = 0.5
    anchor = _parse(state.get("last_recalled_at")) \
        or _parse(state.get("updated_at")) \
        or _parse(state.get("created_at"))
    if anchor is None:
        return s
    ts_sorted = sorted(t for t in (_parse(x) for x in boost_ts or [])
                       if t is not None and anchor < t <= at)
    cap = bp.get("cap_per_day")
    seen_days = {}
    for t in ts_sorted:
        if cap:
            day = t.date().isoformat()
            if seen_days.get(day, 0) >= cap:
                continue
            seen_days[day] = seen_days.get(day, 0) + 1
        s = decay_value(s, _days(anchor, t),
                        state.get("importance"), dp)
        s = apply_boost(s, bp["rate"])
        anchor = t
    return decay_value(s, _days(anchor, at), state.get("importance"), dp)


def boost_eligible(assembly_payload: dict, scope: str,
                   boost_params: dict = None) -> bool:
    """SD-10: 実注入・purpose 対象・scope 既定・receipt 必須。

    basis=search は injected の type=='memory' で担保（構造参照は
    identity/usermodel/state 型なので recall 由来 memory のみ残る）。
    """
    bp = boost_params or params("boost")
    if assembly_payload.get("purpose") not in bp["purposes"]:
        return False
    if scope not in bp["scopes"]:
        return False
    return True


def injected_memory_ids(assembly_payload: dict) -> list:
    return [i["id"] for i in assembly_payload.get("injected") or []
            if isinstance(i, dict) and i.get("type") == "memory"
            and i.get("id")]


def demote_check(strength: float, decay_params: dict = None) -> bool:
    dp = decay_params or params("decay")
    return strength <= dp["floor"]


def stamp_needed(stored_s: float, derived_s: float,
                 decay_params: dict = None) -> bool:
    """sweep が param_update を刻印すべき変化量（churn 抑制の min_delta）。"""
    dp = decay_params or params("decay")
    if stored_s is None:
        stored_s = 0.5
    return abs(stored_s - derived_s) >= dp["min_delta"]


# ------------------------------------------------- state 投影（D-D3）
def project_inner(baseline: dict, changes, at_ts: str,
                  proj_params: dict = None) -> dict:
    """inner（気分）の投影値。

    changes: [(ts, value_dict)] 時系列順の state_change payload。
    各 change は EMA+max_step 緩衝を通してから取り込み（Q-O157:
    機構導入・初版は無緩衝値）、最後に at 時点までの線形復帰を掛ける
    （偽のイベントは発行しない —— evolution §5.2）。
    """
    pp = proj_params or params("state_project")
    base = dict(pp["baseline"])
    base.update(baseline or {})
    v = dict(base)
    last_t = None
    alpha = pp.get("ema_alpha", 1.0)
    max_step = pp.get("max_step") or {}
    for ts, ch in changes or []:
        t = _parse(ts)
        for k in ("mood", "valence", "energy"):
            if k not in ch:
                continue
            target = ch[k]
            if isinstance(target, (int, float)) and \
                    isinstance(v.get(k), (int, float)):
                delta = (target - v[k]) * alpha
                step = max_step.get(k)
                if step is not None:
                    delta = max(-step, min(step, delta))
                v[k] = v[k] + delta
            else:
                v[k] = target      # mood ラベル等はそのまま
        last_t = t
    if last_t is not None:
        hours = (_parse(at_ts) - last_t).total_seconds() / 3600.0
        if hours > 0:
            k = min(hours / pp["return_hours"], 1.0)
            # 現行 inner.py と同型: energy→60・valence→0・mood は残る
            v["energy"] = round(
                v.get("energy", 60) + (60 - v.get("energy", 60)) * k)
            v["valence"] = round(v.get("valence", 0) * (1 - k), 1)
    return v


# ------------------------------------------------- reflection_trigger（SD-79）
def reflection_due(accumulated_importance: float,
                   refl_params: dict = None) -> bool:
    rp = refl_params or params("reflect")
    return accumulated_importance >= rp["importance_threshold"]


# ------------------------------------------------- suspect lane（Q-O66）
def suspect_in_scope(new_state: dict, candidate_state: dict,
                     suspect_params: dict = None) -> bool:
    """新規受理 memory の近傍限定（同 scope+subject+memory_type・active）。"""
    sp = suspect_params or params("suspect")
    if candidate_state.get("status") != "active":
        return False
    if candidate_state.get("record_family") != "memory":
        return False
    if candidate_state.get("record_id") == new_state.get("record_id"):
        return False
    if candidate_state.get("scope") != new_state.get("scope"):
        return False
    if sp.get("same_memory_type") and \
            candidate_state.get("memory_type") != \
            new_state.get("memory_type"):
        return False
    if sp.get("same_subject"):
        ns = new_state.get("subject")
        cs = candidate_state.get("subject")
        if ns or cs:
            return ns == cs
    return True


def cap_pairs(pairs, suspect_params: dict = None):
    sp = suspect_params or params("suspect")
    return list(pairs)[:sp["max_pairs"]]


# ------------------------------------------------- prediction（SD-78）
def prediction_due(state: dict, at_ts: str) -> bool:
    """horizon 到来の pending prediction（auto-resolve 対象）。"""
    if state.get("record_family") != "prediction":
        return False
    if state.get("outcome") != "pending":
        return False
    h = _parse(state.get("horizon"))
    return h is not None and _parse(at_ts) >= h


def prediction_scoring_ok(scoring: str) -> bool:
    """auto-resolve 可能な採点方式（deterministic check のみ機械採点。
    verdict 等の人間採点は sweep では解決しない）。"""
    return scoring in ("deterministic", "deterministic_check")


# ------------------------------------------------- simulate（規則版差分）
def simulate_strengths(states, boosts_map: dict, at_ts: str,
                       params_a: dict, params_b: dict) -> dict:
    """decay 規則 a/b の活性化前差分レポート（書き込まない試算）。

    states: state dict の iterable（memory のみが対象、pinned 除外は
    呼び出し側）。boosts_map: {record_id: [ts]}。
    戻り値: {changed, would_demote, max_delta, samples}
    """
    changed, would, max_d = 0, [], 0.0
    samples = []
    for st in states:
        rid = st.get("record_id")
        ts = (boosts_map or {}).get(rid) or []
        a = effective_strength(st, ts, at_ts, decay_params=params_a)
        b = effective_strength(st, ts, at_ts, decay_params=params_b)
        d = abs(a - b)
        if d > 0:
            changed += 1
            samples.append({"record_id": rid, "a": a, "b": b,
                            "delta": round(d, 6)})
        max_d = max(max_d, d)
        if demote_check(b, params_b) and not demote_check(a, params_a):
            would.append(rid)
    samples.sort(key=lambda x: -x["delta"])
    return {"changed": changed, "would_demote": would,
            "max_delta": max_d, "samples": samples[:20]}


def rules_manifest() -> dict:
    """kernel_table/policy_state pin 用の規則一覧＋digest。"""
    return {rid: {"version": RULES[rid]["version"],
                  "params": RULES[rid]["params"],
                  "digest": canon.hash_obj(
                      "pcore.rule.v1",
                      {"rule": rid, "version": RULES[rid]["version"],
                       "params": RULES[rid]["params"]})}
            for rid in sorted(RULES)}
