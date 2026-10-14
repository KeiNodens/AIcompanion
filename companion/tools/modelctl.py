"""会話モデル・reasoning effort の実行時切替（state.json保存・再起動後も維持）。

適用範囲は engine の会話ループのみ。記憶抽出・reflection・PageIndex 等の
裏方処理は env の LLM_MODEL 固定のまま。
"""
from ..config import EFFORT_CHOICES, LLM_MODEL, REASONING_EFFORT
from ..db import load_state, update_state
from ..llm import available_models, current_params
from .actor import is_owner

_DENY = "その設定はマスターしか変えられないの"


def model_status() -> str:
    model, effort = current_params()
    lines = [
        f"会話モデル: {model}（effort: {effort}）",
        f"env既定: {LLM_MODEL} / {REASONING_EFFORT}",
    ]
    models = available_models()
    if models:
        lines.append("利用可能: " + ", ".join(models))
    ov = load_state().get("llm")
    if not ov:
        lines.append("（上書きなし・env既定で動作中）")
    return "\n".join(lines)


def model_set(model: str, effort: str | None = None) -> str:
    if not is_owner():
        return _DENY
    model = (model or "").strip()
    if not model:
        return "モデル名を指定してください"
    if effort is not None:
        effort = effort.strip()
        if effort not in EFFORT_CHOICES:
            return f"effort は {'/'.join(EFFORT_CHOICES)} のいずれかです"

    def apply(state: dict) -> None:
        ov = dict(state.get("llm") or {})
        ov["model"] = model
        if effort:
            ov["effort"] = effort
        state["llm"] = ov

    update_state(apply)

    warn = ""
    avail = available_models()
    if avail and model not in avail:
        warn = f"\n（注意: モデル一覧に {model} はありません。存在確認済み: {', '.join(avail)}）"
    m, e = current_params()
    return f"会話モデルを {m} / effort {e} に切り替えました{warn}"


def model_reset() -> str:
    if not is_owner():
        return _DENY
    update_state(remove=["llm"])
    return f"env既定に戻しました（{LLM_MODEL} / {REASONING_EFFORT}）"
