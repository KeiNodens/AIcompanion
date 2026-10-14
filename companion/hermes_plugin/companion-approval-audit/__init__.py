"""companion-approval-audit — 承認 hook イベントを Core に転送する observer。

runtime-hermes-tooling-impl.md R5b/R5c。承認可否の正本は Hermes 側の
会話内承認で、この plugin は記録を companion の
POST /v1/bridge/approval_event に写すだけ。転送は失敗してよい
（Core 停止中でも承認を阻害しないよう常に fail-open）。

command は送信前に redact して 200 字に切る（承認文面に秘密情報が
混ざることがある — Hermes 側の承認 hook 仕様の注意書きどおり）。

Hermes API への参照は実行時の lazy import に限定し、import 自体は
stdlib のみで完結する（companion 側テストから直接読み込める形）。
"""

import json
import logging
import os
import re
import urllib.request

log = logging.getLogger("hermes.plugins.companion-approval-audit")

DEFAULT_BASE = "http://localhost:8788/v1"
EXCERPT_MAX = 200
_TIMEOUT = 3.0

# --- redaction (R5c) --------------------------------------------------------

_SECRET_KV_RE = re.compile(
    r"(?i)\b([A-Za-z_][A-Za-z0-9_]*"
    r"(?:KEY|TOKEN|SECRET|PASS|PASSWORD|PWD|CREDENTIAL|AUTH)[A-Za-z0-9_]*)"
    r"\s*[=:]\s*(\"[^\"\n]*\"|'[^'\n]*'|[^\s&|;]+)")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
_USERINFO_RE = re.compile(r"(://)[^/@\s:]+:[^/@\s]+@")
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-][A-Za-z0-9._\-+/=]{23,}")
_WS_RE = re.compile(r"\s+")


def redact(text: str, extra_secrets: tuple = ()) -> str:
    """承認 command/説明文から秘密らしき断片を潰し、抜粋として返す。

    - KEY=val / KEY: val（KEY|TOKEN|SECRET|PASS|AUTH… を含む変数名）
    - Bearer <token>
    - URL userinfo（://user:pass@）
    - 24字超のトークン状文字列
    - 既知の秘密値（解決済みの companion 鍵など）のリテラル出現
    """
    text = _WS_RE.sub(" ", str(text or "")).strip()
    if not text:
        return ""
    for secret in extra_secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    text = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}=***", text)
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _USERINFO_RE.sub(r"\1***:***@", text)
    text = _LONG_TOKEN_RE.sub("***", text)
    return text[:EXCERPT_MAX]


# --- endpoint / key resolution ----------------------------------------------

def _hermes_home():
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        return None


def _dotenv_value(name: str):
    """Hermes .env から name= の値を読む（env 変数未展開時の fallback）。"""
    home = _hermes_home()
    if home is None:
        return None
    try:
        for line in (home / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def _resolve_secret(value: str):
    """${VAR} / $VAR 形式なら secret_scope → env → .env の順で解決。
    リテラル値はそのまま返す。"""
    value = str(value or "").strip()
    m = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", value)
    if not m:
        return value or None
    name = m.group(1)
    try:
        from agent.secret_scope import get_secret
        got = get_secret(name)
        if got:
            return got
    except Exception:
        pass
    return os.environ.get(name) or _dotenv_value(name)


def _config() -> dict:
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _endpoint_and_key():
    """(url, key)。config.yaml の providers.* で x-companion-key を
    送るエンドポイント（= companion Core）から解決する。見つからなければ
    COMPANION_APPROVAL_URL / COMPANION_API_URL env、最後に既定値。"""
    override = os.environ.get("COMPANION_APPROVAL_URL", "").strip()
    if override:
        return override, _api_key()
    providers = _config().get("providers") or {}
    for entry in providers.values():
        if not isinstance(entry, dict):
            continue
        headers = {}
        for hk in ("extra_headers", "default_headers"):
            h = entry.get(hk)
            if isinstance(h, dict):
                headers.update(h)
        for hk, hv in headers.items():
            if str(hk).lower() == "x-companion-key":
                base = str(entry.get("base_url") or "").rstrip("/")
                if base:
                    return (f"{base}/bridge/approval_event",
                            _api_key(default=_resolve_secret(hv)))
    base = os.environ.get("COMPANION_API_URL", "").strip().rstrip("/")
    if not base:
        base = DEFAULT_BASE
    if not base.endswith("/v1") and "/bridge/" not in base:
        base = base + "/v1"
    return base + "/bridge/approval_event", _api_key()


def _api_key(default=None):
    return (os.environ.get("COMPANION_API_KEY", "").strip()
            or _resolve_secret("${COMPANION_API_KEY}")
            or default)


# --- forwarding --------------------------------------------------------------

def _post(payload: dict) -> bool:
    url, key = _endpoint_and_key()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST")
    if key:
        req.add_header("x-companion-key", key)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        log.debug("companion approval event forward failed: %s", e)
        return False


def _forward(event: str, kwargs: dict) -> None:
    try:
        key = _api_key()
        excerpt = redact(
            kwargs.get("command") or kwargs.get("description") or "",
            extra_secrets=(key,) if key else ())
        payload = {
            "event": event,
            "session_key": kwargs.get("session_key") or "",
            "session_id": kwargs.get("session_id") or "",
            "turn_id": kwargs.get("turn_id") or "",
            "tool_call_id": kwargs.get("tool_call_id") or "",
            "pattern_key": kwargs.get("pattern_key") or "",
            "pattern_keys": kwargs.get("pattern_keys") or [],
            "surface": kwargs.get("surface") or "",
            "description": redact(kwargs.get("description") or "",
                                  extra_secrets=(key,) if key else ()),
            "command_excerpt": excerpt,
        }
        for k in ("choice", "decided_by", "coalesced"):
            if kwargs.get(k) is not None:
                payload[k] = kwargs[k]
        if not payload["pattern_key"]:
            keys = payload["pattern_keys"]
            payload["pattern_key"] = str(keys[0]) if keys else "unknown"
        _post(payload)
    except Exception:
        # observer は決して承認経路を壊さない
        log.debug("companion approval event forward aborted", exc_info=True)


def _on_pre(**kwargs):
    _forward("pre_approval_request", kwargs)


def _on_post(**kwargs):
    _forward("post_approval_response", kwargs)


def register(ctx) -> None:
    ctx.register_hook("pre_approval_request", _on_pre)
    ctx.register_hook("post_approval_response", _on_post)
