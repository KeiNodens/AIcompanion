from openai import AsyncOpenAI, OpenAI

from .config import LLM_BASE_URL, LLM_MODEL, REASONING_EFFORT

llm = AsyncOpenAI(base_url=LLM_BASE_URL, api_key="unused")


def current_params() -> tuple[str, str]:
    """会話用の (model, reasoning_effort)。state.json の llm 上書き or env既定"""
    from .db import load_state

    ov = load_state().get("llm") or {}
    return ov.get("model") or LLM_MODEL, ov.get("effort") or REASONING_EFFORT


def available_models() -> list[str]:
    """LLMエンドポイントの /v1/models 一覧（同期版。失敗時は空）"""
    try:
        res = OpenAI(
            base_url=LLM_BASE_URL, api_key="unused", timeout=5
        ).models.list()
        return sorted(m.id for m in res.data)
    except Exception:
        return []
