"""Hermes 側設定（config.yaml / .env）の読取口。

body 連携が必要とする Hermes の設定値をここに一元化する。鍵の単一ソース化
（runtime-hermes-tooling.md §4.4）と config lint（hermes_check.py）の両方が
このモジュール経由で読む。書き込みはしない。

- load_config(): HERMES_HOME/config.yaml を yaml で読む。(mtime, size) で
  キャッシュ。無い・壊れている場合は {}
- load_env():    HERMES_HOME/.env を読む（Hermes 側の秘密値。companion の
  .env ではない）。同じく (mtime, size) キャッシュ
- resolve():     "${VAR}" 形式の値を Hermes .env → os.environ の順で展開
- api_server_key() / companion_key(): (鍵, 出所) を返す。鍵の値そのものは
  ログやツール応答に出してはいけない
"""
import logging
import os
import re
import urllib.parse

import yaml
from dotenv import dotenv_values

from . import config

log = logging.getLogger("companion")

_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

_yaml_cache: dict = {"sig": None, "value": {}}
_env_cache: dict = {"sig": None, "value": {}}


def _sig(path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load_config() -> dict:
    """HERMES_HOME/config.yaml を読む。無い/壊れ/トップがdictでない→{}"""
    path = config.HERMES_HOME / "config.yaml"
    sig = _sig(path)
    if sig is None:
        _yaml_cache.update(sig=None, value={})
        return {}
    if _yaml_cache["sig"] == sig:
        return _yaml_cache["value"]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("Hermes config.yaml が読めません", exc_info=True)
        data = None
    if not isinstance(data, dict):
        data = {}
    _yaml_cache.update(sig=sig, value=data)
    return data


def load_env() -> dict:
    """HERMES_HOME/.env を読む。無い→{}"""
    path = config.HERMES_HOME / ".env"
    sig = _sig(path)
    if sig is None:
        _env_cache.update(sig=None, value={})
        return {}
    if _env_cache["sig"] == sig:
        return _env_cache["value"]
    try:
        data = {k: v for k, v in dotenv_values(path).items() if v is not None}
    except Exception:
        log.warning("Hermes .env が読めません", exc_info=True)
        data = {}
    _env_cache.update(sig=sig, value=data)
    return data


def resolve(value) -> str:
    """"${VAR}" 形式の値だけを展開する。それ以外は str() そのまま。
    未定義の変数は空文字にする"""
    if not isinstance(value, str):
        return str(value)
    m = _VAR_RE.match(value.strip())
    if not m:
        return value
    name = m.group(1)
    return load_env().get(name) or os.environ.get(name, "")


_EMBED_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: str) -> str:
    """"Bearer ${VAR}" のような埋め込み ${VAR} を全て展開する。
    Hermes は headers/url の値の一部として ${VAR} を許すため、
    値全体が ${VAR} の resolve() だけでは拾えない"""
    if not isinstance(value, str):
        return str(value)
    return _EMBED_VAR_RE.sub(
        lambda m: load_env().get(m.group(1)) or os.environ.get(m.group(1), ""),
        value,
    )


def targets_companion(url) -> bool:
    """base_url が companion の :API_PORT を向いているか"""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    return host in _LOCAL_HOSTS and port == config.API_PORT


def _api_server_extra() -> dict:
    platforms = load_config().get("platforms") or {}
    api_server = platforms.get("api_server") or {}
    extra = api_server.get("extra") or {}
    return extra if isinstance(extra, dict) else {}


def api_server_key() -> tuple[str, str]:
    """body 側 api_server(:8642) の Bearer 鍵。(key, source) を返す。

    出所の優先順: env の HERMES_API_KEY → Hermes config の
    platforms.api_server.extra.key → Hermes .env の API_SERVER_KEY
    """
    if config.HERMES_API_KEY:
        return str(config.HERMES_API_KEY), "env"
    key = resolve(_api_server_extra().get("key") or "")
    if key:
        return key, "hermes-config"
    key = load_env().get("API_SERVER_KEY") or ""
    if key:
        return key, "hermes-env"
    return "", ""


def companion_key() -> tuple[str, str]:
    """:8788 の入口鍵。(key, source) を返す。

    出所の優先順: env の COMPANION_API_KEY → Hermes config で companion を
    向く providers.<name>.extra_headers の "x-companion-key"（ヘッダ名は
    大小無視）
    """
    if config.COMPANION_API_KEY:
        return str(config.COMPANION_API_KEY), "env"
    providers = load_config().get("providers") or {}
    if not isinstance(providers, dict):
        return "", ""
    for entry in providers.values():
        if not isinstance(entry, dict):
            continue
        if not targets_companion(entry.get("base_url")):
            continue
        headers = entry.get("extra_headers") or {}
        if not isinstance(headers, dict):
            continue
        for hk, hv in headers.items():
            if str(hk).lower() == "x-companion-key":
                key = resolve(hv)
                if key:
                    return key, "hermes-config"
    return "", ""


def mcp_key() -> tuple[str, str]:
    """:8788 /mcp の入口鍵。(key, source) を返す。

    出所の優先順: env の COMPANION_MCP_KEY → Hermes config で companion
    を向く mcp_servers.<name>.headers（Authorization: Bearer /
    x-companion-key、${VAR} 展開あり）→ Hermes .env の COMPANION_MCP_KEY
    """
    if config.COMPANION_MCP_KEY:
        return str(config.COMPANION_MCP_KEY), "env"
    servers = load_config().get("mcp_servers") or {}
    if isinstance(servers, dict):
        for entry in servers.values():
            if not isinstance(entry, dict):
                continue
            if not targets_companion(entry.get("url")):
                continue
            headers = entry.get("headers") or {}
            if not isinstance(headers, dict):
                continue
            for hk, hv in headers.items():
                value = _expand(hv).strip()
                if not value or "${" in value:
                    continue
                if str(hk).lower() == "authorization":
                    if value.lower().startswith("bearer "):
                        return value[7:].strip(), "hermes-config"
                elif str(hk).lower() == "x-companion-key":
                    return value, "hermes-config"
    key = load_env().get("COMPANION_MCP_KEY") or ""
    if key:
        return key, "hermes-env"
    return "", ""
