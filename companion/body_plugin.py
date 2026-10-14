"""Hermes 側 approval-audit plugin の配布口（runtime-hermes-tooling-impl.md R5b）。

companion が出荷する `companion-approval-audit` plugin は
`companion/hermes_plugin/companion-approval-audit/` が正本で、Hermes は
`HERMES_HOME/plugins/<name>/` から user plugin として読む。ここはその
写しを置く口と導入状態の点検だけ（Hermes プロセスには触らない）。

有効化は Hermes 側の `plugins.enabled`（`hermes plugins enable
companion-approval-audit` 相当）で、companion は Hermes の config.yaml を
書き換えない — 未導入/未有効化は hermes_check が warn で報告する。
"""
import filecmp
import logging
import shutil
import sys
from pathlib import Path

from . import config, hermes_conf

log = logging.getLogger("companion")

PLUGIN_NAME = "companion-approval-audit"
PLUGIN_FILES = ("plugin.yaml", "__init__.py")


def shipped_dir() -> Path:
    return Path(__file__).resolve().parent / "hermes_plugin" / PLUGIN_NAME


def installed_dir() -> Path:
    return config.HERMES_HOME / "plugins" / PLUGIN_NAME


def status(cfg: dict | None = None) -> dict:
    """導入状態。installed/enabled/drift を bool で返す（書き込みはしない）。
    cfg を渡すと plugins.enabled/disabled はその config を見る
    （省略時は実 HERMES_HOME/config.yaml）"""
    shipped = shipped_dir()
    dest = installed_dir()
    installed = all((dest / f).is_file() for f in PLUGIN_FILES)
    drift = installed and any(
        not (dest / f).is_file()
        or not filecmp.cmp(shipped / f, dest / f, shallow=False)
        for f in PLUGIN_FILES)
    cfg = hermes_conf.load_config() if cfg is None else (cfg or {})
    plugins = cfg.get("plugins") or {}
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled") or []
    return {
        "installed": installed,
        "drift": drift,
        # enabled は allow-list。key 自体が無い(None)なら未指定=要 enable
        "enabled": isinstance(enabled, list) and PLUGIN_NAME in enabled,
        "disabled": isinstance(disabled, list) and PLUGIN_NAME in disabled,
        "dest": str(dest),
    }


def install() -> dict:
    """出荷 plugin を HERMES_HOME/plugins/ に写す。上書きは最新化のみ。"""
    shipped = shipped_dir()
    dest = installed_dir()
    if not all((shipped / f).is_file() for f in PLUGIN_FILES):
        raise FileNotFoundError(f"出荷 plugin が見つかりません: {shipped}")
    dest.mkdir(parents=True, exist_ok=True)
    for f in PLUGIN_FILES:
        shutil.copy2(shipped / f, dest / f)
    return status()


def main(argv=None) -> int:
    st = install()
    print(f"{PLUGIN_NAME}: installed -> {st['dest']}")
    if st["enabled"] and not st["disabled"]:
        print("plugins.enabled に登録済みです")
    else:
        print("有効化は Hermes 側で: hermes plugins enable "
              f"{PLUGIN_NAME}（config.yaml の plugins.enabled）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
