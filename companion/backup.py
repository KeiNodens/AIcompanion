"""data/ と人格ファイルの日次スナップショット。

DB（*.db）は sqlite の .backup() API で取るのでライブ中でも一貫した写しに
なる（-wal/-shm は別途不要・写し側に反映済み）。qdrant・skills 系・docs 等の
ディレクトリと state.json・人格ファイルはそのまま写す。qdrant は
crash-consistent な写しだが WAL があるので起き直りで回復できる。

世代は data/backups/daily-<ts>/ に BACKUP_KEEP 件だけ残す。`pre-lmig-*`
（migrate.py の手動バックアップ）は世代管理の対象外なので消えない。
`python -m companion.backup` で手動実行もできる。
"""
import json
import logging
import os
import shutil
import sqlite3
import stat
from datetime import datetime
from pathlib import Path

from .config import BACKUP_DIR, BACKUP_KEEP, DATA_DIR, ROOT

log = logging.getLogger("companion")


def fix_readonly(root: Path) -> list[str]:
    """root 配下の読み取り専用ファイルを書込み可に戻す。
    直したファイルの相対パス一覧を返す。

    Windows では copy2/copytree の copystat が R 属性まで写すため、
    ライブファイルに R が付くとバックアップ経由で再持ち込みされる。
    起動時（runtime.ensure_started）・写し直後・rotate 前に呼んで
    失敗を自己修復にする。"""
    if not root.exists():
        return []
    fixed = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            if p.stat().st_mode & stat.S_IWRITE:
                continue
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            continue
        fixed.append(str(p.relative_to(root)))
    return fixed

# ディレクトリ写しするもの（db・ログ・スクショ・backups 自身は除く）
_DIRS = (
    "qdrant", "skills", "skills-inbox", "skills-export",
    "skills-archive", "docs", "delegate",
)
# data/ 直下のファイル
_DATA_FILES = (
    "state.json", "persona_notes.md", "user_profile.md",
    "recent_context.md", "tool_log.jsonl", "profile_policy.yaml",
)
# repo ルート側の人格・設定ファイル（data/ と一緒に退避しておく）
_ROOT_FILES = ("persona.md", "prompt.txt", "SOUL.md", ".env")


def _copy_db(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    return dst.stat().st_size


def _copy_tree(src: Path, dst: Path) -> int:
    shutil.copytree(src, dst)
    return sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())


def _copy_file(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst.stat().st_size


def rotate(keep: int | None = None) -> list[Path]:
    """daily-* 世代を keep 件だけ残して古い順に消す。pre-lmig-* は触らない"""
    keep = BACKUP_KEEP if keep is None else keep
    if not BACKUP_DIR.exists():
        return []
    gens = sorted(p for p in BACKUP_DIR.glob("daily-*") if p.is_dir())
    stale = gens if keep <= 0 else gens[:-keep]
    for p in stale:
        fix_readonly(p)  # R属性ファイルがあると rmtree が詰まるので先に解除
        shutil.rmtree(p, ignore_errors=True)
    return stale


def run_backup() -> dict:
    """1世代分のスナップショットを取る。部分的な失敗は errors に集めて続行"""
    stamp = datetime.now().strftime("daily-%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)
    files, errors = [], []
    total = 0

    def _step(label: str, fn) -> None:
        nonlocal total
        try:
            n = fn()
        except FileNotFoundError:
            return  # 無いファイルは飛ばす（初回セットアップ前など）
        except Exception as e:
            errors.append(f"{label}: {e}")
            log.warning("backup %s failed: %s", label, e)
            return
        files.append(label)
        total += n

    # sqlite DB群（companion/core/history/derived 等、*.db を全て一貫写し）
    for src in sorted(DATA_DIR.glob("*.db")):
        _step(f"db/{src.name}",
              lambda s=src: _copy_db(s, dest / "db" / s.name))
    for name in _DIRS:
        src = DATA_DIR / name
        if src.is_dir():
            _step(f"{name}/", lambda s=src: _copy_tree(s, dest / name))
    for name in _DATA_FILES:
        _step(name, lambda n=name: _copy_file(DATA_DIR / n, dest / n))
    for name in _ROOT_FILES:
        _step(name, lambda n=name: _copy_file(ROOT / n, dest / n))

    (dest / "manifest.json").write_text(
        json.dumps(
            {"created_at": datetime.now().isoformat(timespec="seconds"),
             "files": files, "errors": errors, "bytes": total},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    fixed = fix_readonly(dest)  # copystat が R 属性まで写すので写し側は必ず解除
    if fixed:
        log.warning("backup: cleared read-only attr on %d copies: %s",
                    len(fixed), fixed)
    removed = rotate()
    if removed:
        log.info("backup rotated out: %s", [p.name for p in removed])
    log.info("backup done: %s (%d items, %d bytes, %d errors)",
             dest.name, len(files), total, len(errors))
    return {"dir": str(dest), "files": files, "bytes": total, "errors": errors}


def main() -> None:
    res = run_backup()
    print(f"backup -> {res['dir']}")
    print(f"  {len(res['files'])} items, {res['bytes']} bytes")
    for e in res["errors"]:
        print(f"  ERROR: {e}")


if __name__ == "__main__":
    main()
