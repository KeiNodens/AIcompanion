"""kernel.approval —— 会話内承認の決定的照合器（write-path §5.3・core-api §4）

「approval 登録以後に ingress された最初の actor=master message」を
版付き語彙 registry で機械照合し、一致時のみ decided_by=master を刻む。
LLM は照合に介さない。曖昧応答は None（pending のまま残留 = 安全側）。

語彙集合は vocab_registry に `content_digest` pin 付きで登録し、
変更は語彙版 bump＋policy_change 刻印を伴う（SD-58・Q-O21）。
"""

from . import canon

VOCAB_ID = "approval_vocab"
VOCAB_VERSION = "approval-vocab-v1"

# 初期集合は狭く（Q-O21: 曖昧なものは常に pending 残留で安全側）。
# 照合は canon.norm_text 済み文字列への部分一致。
APPROVE_PATTERNS = (
    "承認", "承認する", "いいよ", "いいです", "ok", "おk", "おけ",
    "やって", "お願い", "お願いします", "それでいい", "それでok",
    "覚えておいて", "覚えて", "そうして", "ぜひ", "はい、お願い",
)
REJECT_PATTERNS = (
    "却下", "だめ", "ダメ", "駄目", "やめて", "やめ", "待って",
    "違う", "覚えないで", "忘れないで", "いや", "いいえ", "やめろ",
    "no", "なし", "ナシ", "やめる", "保留",
)
# 誤爆防止: 上記の短い語が別語の一部になるのを防ぐ否定・保留の優先パターン
# （「待って」が含まれれば approve 語があっても pending 側に倒す、等）
BLOCKING_PATTERNS = (
    "待って", "違う", "だけど", "でも", "ただし", "いや",
)


def vocab_snapshot() -> dict:
    """vocab_registry 登録用の語彙版 snapshot。"""
    return {
        "version": VOCAB_VERSION,
        "approve": sorted(APPROVE_PATTERNS),
        "reject": sorted(REJECT_PATTERNS),
        "blocking": sorted(BLOCKING_PATTERNS),
    }


def vocab_digest() -> str:
    return canon.hash_obj("pcore.approval_vocab.v1", vocab_snapshot())


def match_decision(text: str, vocab_row: dict = None):
    """master 発言テキストを承認語彙と機械照合する。

    戻り値: "approved" | "rejected" | None（不成立・曖昧 = pending 残留）
    vocab_row: vocab_registry に登録済みの語彙 dict（省略時は内蔵版）。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    v = vocab_row or vocab_snapshot()
    t = canon.norm_text(text)
    hits_a = [p for p in v["approve"] if canon.norm_text(p) in t]
    hits_r = [p for p in v["reject"] if canon.norm_text(p) in t]
    hits_b = [p for p in v["blocking"] if canon.norm_text(p) in t]
    # 両方ヒット・blocking 語含有は曖昧として不決裁
    if hits_b or (hits_a and hits_r):
        return None
    if hits_r:
        return "rejected"
    if hits_a:
        return "approved"
    return None
