"""秘密らしき断片の伏字化（共有ヘルパ）。

使い所:
- /v1/bridge/approval_event で受けた抜粋の再スクラブ（ingest 側の保険。
  本来の redaction は Hermes 側 plugin がやる — hermes_plugin/）
- skill_gen が LLM に渡す tool trace 材料の伏字化

Hermes 側 plugin（companion/hermes_plugin/companion-approval-audit）は
companion を import できないので独自に同等の実装を持つ。両者を揃えて
保守すること。
"""
import re

_SECRET_KV_RE = re.compile(
    r"(?i)\b([A-Za-z_][A-Za-z0-9_]*"
    r"(?:KEY|TOKEN|SECRET|PASS|PASSWORD|PWD|CREDENTIAL|AUTH)[A-Za-z0-9_]*)"
    r"\s*[=:]\s*(\"[^\"\n]*\"|'[^'\n]*'|[^\s&|;]+)")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
_USERINFO_RE = re.compile(r"(://)[^/@\s:]+:[^/@\s]+@")
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-][A-Za-z0-9._\-+/=]{23,}")
_WS_RE = re.compile(r"\s+")


def scrub_text(text, cap: int = 500) -> str:
    """KEY=val / Bearer / URL userinfo / 長いトークン状文字列を潰し、
    空白を畳んで cap 字に切る。"""
    text = _WS_RE.sub(" ", str(text or "")).strip()
    if not text:
        return ""
    text = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}=***", text)
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _USERINFO_RE.sub(r"\1***:***@", text)
    text = _LONG_TOKEN_RE.sub("***", text)
    return text[:cap]
