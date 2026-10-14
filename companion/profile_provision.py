"""Hermes bot profile の自動 provisioning（案A）。

Desktop/CLI で作られた Hermes profile（HERMES_HOME/profiles/<name>/）は
model ブロックだけを継承し、companion 経由に必要な以下が欠ける:

- providers（companion 向けエントリ: session_affinity_header +
  x-companion-key） — 無いと入口認証 enforce で 401
- auxiliary — 無いと補助呼出し（圧縮・タイトル・vision）が主モデル
  （=companion）に fallback し、彼女の口調の要約で turns/記憶を汚す
- delegation — 無いと子エージェントも companion を呼ぶ

model ブロックが companion（default=="companion" または base_url に
:8788）を向く profile を走査し、欠けているキーだけを launch config
（HERMES_HOME/config.yaml）の値で補完する。既存値は上書きしない。

scheduler の定期 tick（jobs.py）と scripts/bot_profile_setup.py の
両方から呼ばれる。書き込みは tmp+replace。yaml.safe_dump で書くので
ファイル内のコメントは保存されない（bot profile の config は
機械生成が前提なので許容とする）。
"""
import logging
import time
from pathlib import Path

import yaml

from . import config, hermes_conf

log = logging.getLogger("companion")

_last_run = 0.0


def _profiles_root() -> Path:
    return config.HERMES_HOME / "profiles"


def _targets_companion(model: dict) -> bool:
    """model ブロックが companion を向いているか。
    継承されたブロックは default="companion" + base_url=:8788"""
    if not isinstance(model, dict):
        return False
    if str(model.get("default") or "").strip() == "companion":
        return True
    return "8788" in str(model.get("base_url") or "")


def _companion_providers(launch: dict) -> dict:
    """launch config の providers のうち :8788 を向くもの（名前非依存）"""
    out = {}
    for name, entry in (launch.get("providers") or {}).items():
        if isinstance(entry, dict) and "8788" in str(entry.get("base_url") or ""):
            out[name] = entry
    return out


def _luna_route() -> dict:
    """launch config の model_routes.body（裏方 luna）の値を取る"""
    routes = (
        ((hermes_conf.load_config().get("platforms") or {})
         .get("api_server") or {})
        .get("extra") or {}
    ).get("model_routes") or {}
    body = routes.get("body") or {}
    return {
        "model": str(body.get("model") or "").strip(),
        "base_url": str(body.get("base_url") or "").strip(),
        "api_key": str(body.get("api_key") or "").strip(),
    }


def _wanted() -> dict:
    """profile config に欲しいキーの dict。launch config から組み立てる"""
    launch = hermes_conf.load_config()
    want = {}
    prov = _companion_providers(launch)
    if prov:
        want["providers"] = prov
    aux = launch.get("auxiliary")
    if isinstance(aux, dict):
        want["auxiliary"] = aux
    luna = _luna_route()
    if luna["base_url"] and luna["model"]:
        want["delegation"] = {
            "model": luna["model"],
            "base_url": luna["base_url"],
            "api_key": luna["api_key"] or "local",
        }
    model = launch.get("model") or {}
    if model.get("default") and model.get("base_url"):
        want["model"] = model
    return want


def _merge(dst: dict, want: dict) -> list[str]:
    """欠けているキーだけを足す深いマージ。既存値は上書きしない。
    追加したキー名の一覧を返す"""
    added = []
    for key, wv in want.items():
        dv = dst.get(key)
        if not isinstance(dv, dict) or not isinstance(wv, dict):
            if key not in dst:
                dst[key] = wv
                added.append(key)
            continue
        for sk, sv in wv.items():
            if sk not in dv:
                dv[sk] = sv
                added.append(f"{key}.{sk}")
    return added


def provision_profile(pdir: Path, want: dict | None = None) -> list[str]:
    """1 profile を補完し、追加したキー名を返す。変更なし・対象外は []"""
    cfg_path = pdir / "config.yaml"
    if not cfg_path.is_file():
        return []
    try:
        existing = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        log.warning("profile config.yaml が読めません: %s", pdir, exc_info=True)
        return []
    if not isinstance(existing, dict):
        return []
    if not _targets_companion(existing.get("model")):
        return []
    added = _merge(existing, want if want is not None else _wanted())
    if not added:
        return []
    tmp = cfg_path.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(existing, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(cfg_path)
    return added


def ensure_companion_profiles(force: bool = False) -> dict[str, list[str]]:
    """companion 向きの全 profile を補完。{profile名: 追加キー} を返す。
    PROFILE_PROVISION_MIN 秒（既定 1 時間=3600s）より短い間隔では何もしない。
    force=True は間隔を無視する（スクリプトからの即時実行用）"""
    global _last_run
    if not config.PROFILE_PROVISION:
        return {}
    now = time.monotonic()
    if (not force and _last_run
            and now - _last_run < config.PROFILE_PROVISION_MIN):
        return {}
    _last_run = now
    root = _profiles_root()
    if not root.is_dir():
        return {}
    want = _wanted()
    if not want.get("providers"):
        log.warning("launch config に companion 向け providers がありません"
                    "（profile provisioning は継続します）")
    changed = {}
    for pdir in sorted(root.iterdir()):
        if not pdir.is_dir():
            continue
        try:
            added = provision_profile(pdir, want)
        except Exception:
            log.exception("profile provisioning に失敗: %s", pdir.name)
            continue
        if added:
            changed[pdir.name] = added
            log.info("profile %s を companion 向けに補完: %s",
                     pdir.name, ", ".join(added))
    return changed
