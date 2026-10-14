"""kernel.tier —— L-TIER の純粋規則（FA §6.2）

4 tier（hot/warm/cold/archive）の判定・遷移規則。I/O・時計は持たない。
adapter（adapters.tiering）が records の現値と tier_eval（運用表）の
滞留値を渡し、ここで「この cycle に提案すべき遷移」を決定的に出す。

- desired_tier: retrieval_strength の閾値写像（閾値は scope 別 policy）
- hysteresis: desired≠current が hysteresis cycle 連続したときだけ遷移
- dwell: 直前遷移から dwell_min_cycles 未満の cycle では遷移しない
- cycle_id: 日付文字列から決定的導出（冪等錨）
- supersede_carry: supersede 時に tier/強度/アクセス履歴を継承（SD-17）

archive への遷移判定自体はここで出すが、apply はしない —— proposal は
強制 pending（admit の tier_archive_pending check）で翌朝承認面に載る。
"""

from . import canon

TIER_VERSION = "tier-v1"

TIERS = ("hot", "warm", "cold", "archive")
_TIER_IDX = {t: i for i, t in enumerate(TIERS)}

# tier 遷移イベントの cause 語彙（層語彙・open-world）
CAUSES = frozenset({
    "decay_streak",        # hysteresis を超えた低下系遷移
    "context_reheat",      # 文脈再加熱による上向き遷移
    "manual",              # 手動 demote/昇格
    "restore",             # status 復帰との連動（limbo 禁止・SD-42）
    "supersede_carry",     # supersede 継承（SD-17）
    "migration",           # 移行 seed
    "archive_approved",    # 承認面での archive 確定
    "archive_rejected",    # 承認面での archive 見送り
})

# scope 別 policy 初期値（Q-O46）。
# agent は旧 demote_stale の写像値: 旧 confidence floor=0.05・逓減 0.9/週
# を tier 閾値に写す。importance 猶予（imp*7日）は decay 規則側が担う
# ため、ここでは純粋に retrieval_strength 閾値のみ持つ。
DEFAULT_TIER_POLICIES = {
    "user": {
        "enabled": True,
        "warm_below": 0.40,     # s < 0.40 → warm 相当
        "cold_below": 0.15,     # s < 0.15 → cold 相当
        "archive_below": 0.05,  # s < 0.05 → archive 候補
        "hysteresis": 3,        # 連続3cycleで遷移確定
        "dwell_min_cycles": 1,  # 最小滞留1cycle
        "archive_mode": "approval",  # auto_apply 不採用（Q-O47）
    },
    "agent": {
        "enabled": True,
        "warm_below": 0.40,
        "cold_below": 0.15,
        "archive_below": 0.05,
        "hysteresis": 3,
        "dwell_min_cycles": 1,
        "archive_mode": "approval",
    },
}


def valid_tier(t) -> bool:
    return t in _TIER_IDX


def tier_index(t) -> int:
    return _TIER_IDX.get(t, -1)


def valid_cause(c) -> bool:
    return c in CAUSES


def cycle_id_for(day: str) -> str:
    """日付文字列（YYYY-MM-DD）から cycle_id を決定的導出。"""
    return f"tier-{str(day)[:10]}"


def desired_tier(strength, policy: dict) -> str:
    """retrieval_strength → desired tier の閾値写像。

    archive は「候補」として返す（apply は承認面経由のみ）。
    """
    s = float(strength or 0.0)
    if s < policy["archive_below"]:
        return "archive"
    if s < policy["cold_below"]:
        return "cold"
    if s < policy["warm_below"]:
        return "warm"
    return "hot"


def dwell_ok(state: dict, cycle_no: int, policy: dict) -> bool:
    """最小滞留の判定。state.tier_cycle_no は直前遷移の cycle 番号。
    未刻印（遷移歴なし）は dwell 充足扱い。"""
    last = state.get("tier_cycle_no")
    if last is None:
        return True
    try:
        last = int(last)
    except (TypeError, ValueError):
        return True
    return (cycle_no - last) >= int(policy.get("dwell_min_cycles") or 1)


def evaluate(state: dict, policy: dict, *, cycle_no: int,
             eval_row: dict | None) -> dict:
    """1 record の tier 評価（決定的）。

    eval_row: tier_eval 表の滞留行 {desired_tier, streak} または None。
    戻り値:
      {desired, current, streak, transition(bool), direction,
       dwell_ok, archive_review(bool)}
    """
    cur = state.get("tier") or "hot"
    des = desired_tier(state.get("retrieval_strength"), policy)
    prev_des = (eval_row or {}).get("desired_tier")
    streak = int((eval_row or {}).get("streak") or 0)
    if des == cur:
        streak = 0
    elif prev_des == des:
        streak += 1
    else:
        streak = 1
    hy = int(policy.get("hysteresis") or 1)
    dw = dwell_ok(state, cycle_no, policy)
    direction = ("down" if tier_index(des) > tier_index(cur)
                 else "up" if tier_index(des) < tier_index(cur)
                 else "same")
    trans = (des != cur and streak >= hy and dw)
    return {
        "desired": des, "current": cur, "streak": streak,
        "transition": trans, "direction": direction,
        "dwell_ok": dw, "hysteresis": hy,
        "archive_review": trans and des == "archive",
    }


def supersede_carry(prev_state: dict, new_state: dict) -> dict:
    """supersede の生涯継承（SD-17）: tier・強度・想起履歴を引き継ぐ。

    戻り値は new_state のコピーに継承フィールドを上書きしたもの。
    """
    out = dict(new_state)
    for f in ("tier", "retrieval_strength", "last_recalled_at",
              "epistemic_confidence", "tier_cycle_no"):
        if f in prev_state and prev_state[f] is not None:
            out[f] = prev_state[f]
    carry = dict(out.get("carried") or {})
    carry["supersede_carry"] = {
        "from": prev_state.get("record_id"),
        "fields": ["tier", "retrieval_strength", "last_recalled_at",
                   "epistemic_confidence", "tier_cycle_no"],
    }
    out["carried"] = carry
    return out


def policy_digest(policy: dict) -> str:
    """tier_policies 行・proposal 刻印用の policy pin digest。"""
    return canon.hash_obj("pcore.tier_policy.v1",
                          {"version": TIER_VERSION, "policy": policy})
