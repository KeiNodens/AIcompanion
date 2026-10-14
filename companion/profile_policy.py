"""Hermes profile 別ポリシー（案A: 多面・単一脳）。

呼出し元の Hermes profile ごとに (a) 形式橋で見せる身体ツールの glob と
(b) {skills} 索引・skill_* ツールで使える Core 技能を絞る。
正本は data/profile_policy.yaml。ファイル無し・壊れ・キー不足は
環境既定（BODY_BRIDGE_TOOLS / BODY_BRIDGE_DM_TOOLS / 全技能）に
フォールバックする = 従来どおりの挙動。

形式（全部のキーは省略可）:
  default:                        # profile 未登録・解決不能時の既定
    bridge_tools:    [glob, ...]  # 常時開放（省略時 BODY_BRIDGE_TOOLS）
    bridge_owner_tools: [glob, ...]  # owner判定の会話だけ追加（省略時 BODY_BRIDGE_OWNER_TOOLS）
    bridge_dm_tools: [glob, ...]  # DM 会話だけ追加開放（省略時 BODY_BRIDGE_DM_TOOLS）
    skills: "*" | [glob, ...]     # 使える Core 技能名。省略時 "*"=全て
  profiles:
    <profile_name>:               # Hermes sessions.profile_name の値
      bridge_tools: ...           # キーごとに default → 組込み既定へ倒れる
      bridge_dm_tools: ...        # （部分上書き。merge ではなく置き換え）
      skills: ...

skills の値は "*" or 省略 = 全 active 技能、明示リスト（空含む）=
glob 一致だけを使える。bridge 側は「Hermes が送ってきた tools ∩ 許可
glob」なので、bot profile の mcp_servers に無い tool はそもそも届かない
（許可は防御の二重化）。mcp__companion 系は bridge.py 側で常に除外。
"""
import fnmatch
import logging

import yaml

from . import config
from .config import PROFILE_POLICY_PATH

log = logging.getLogger("companion")

POLICY_PATH = PROFILE_POLICY_PATH

_cache: dict = {"sig": None, "value": {}}


def load() -> dict:
    """profile_policy.yaml を読む。(mtime, size) キャッシュ。
    無い・壊れ・トップが dict でない → {}"""
    try:
        st = POLICY_PATH.stat()
    except OSError:
        _cache.update(sig=None, value={})
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    if _cache["sig"] == sig:
        return _cache["value"]
    try:
        data = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.warning("profile_policy.yaml が読めません", exc_info=True)
        data = None
    if not isinstance(data, dict):
        data = {}
    _cache.update(sig=sig, value=data)
    return data


def _as_list(v) -> list[str]:
    """list でもカンマ区切り文字列でも受ける正規化"""
    if v is None:
        return []
    if isinstance(v, str):
        return [p.strip() for p in v.split(",") if p.strip()]
    if isinstance(v, (list, tuple)):
        return [str(p).strip() for p in v if str(p).strip()]
    return []


def _sections(profile: str | None) -> tuple[dict, dict]:
    """(profile 節, default 節)。profile 未解決・未登録は空 dict"""
    cfg = load()
    default = cfg.get("default")
    default = default if isinstance(default, dict) else {}
    profs = cfg.get("profiles")
    section = {}
    if profile and isinstance(profs, dict):
        entry = profs.get(profile)
        if isinstance(entry, dict):
            section = entry
    return section, default


def _pick(section: dict, default: dict, key: str, fallback):
    """profiles.<name>.<key> → default.<key> → fallback の順で値を取る"""
    if key in section:
        return section[key]
    if key in default:
        return default[key]
    return fallback


def _env_list(value: str) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def bridge_patterns(profile: str | None) -> tuple[list[str], list[str], list[str]]:
    """(常時開放 glob, owner限定 glob, DM 限定開放 glob)。
    owner/DM の判定は呼び出し側が行う"""
    section, default = _sections(profile)
    always = _pick(section, default, "bridge_tools",
                   _env_list(config.BODY_BRIDGE_TOOLS))
    owner = _pick(section, default, "bridge_owner_tools",
                  _env_list(config.BODY_BRIDGE_OWNER_TOOLS))
    dm = _pick(section, default, "bridge_dm_tools",
               _env_list(config.BODY_BRIDGE_DM_TOOLS))
    return _as_list(always), _as_list(owner), _as_list(dm)


def skill_globs(profile: str | None) -> list[str] | None:
    """その profile で使える技能名の glob 列。None = 全て（既定）。

    明示リスト（空含む）は一致だけを許可。"*" や未指定は全許可。"""
    section, default = _sections(profile)
    v = _pick(section, default, "skills", "*")
    if v is None or (isinstance(v, str) and v.strip() == "*"):
        return None
    return _as_list(v)


def skill_allowed(name: str, profile: str | None) -> bool:
    globs = skill_globs(profile)
    if globs is None:
        return True
    return any(fnmatch.fnmatchcase(name, g) for g in globs)
