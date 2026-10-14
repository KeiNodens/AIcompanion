"""persona.md(+persona_notes) を body 側 SOUL.md に写す配布口（Step 3 項目D）。

Hermes 側の SOUL.md は Core-as-model 形では応答生成に使われない「死に荷物」だが、
人格が出る裏方（自動返信・タイトル生成等の auxiliary）の口調を彼女に寄せる
ために、Core が持つ人格の写しを配置する。Hermes は読み専・companion が書き手。

運用:
- 同期は jobs.scheduler の定期 tick で呼ばれる sync_soul() が担う
- 生成物であることを示すマーカー行を先頭に付ける。マーカーがない既存
  SOUL.md（Hermes既定・手編集物）は上書き前に .bak-<ts> へ一度だけ退避する
- 書き込みは一時ファイル経由の原子置換
"""
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .config import PERSONA_NOTES_PATH, PERSONA_PATH, SOUL_PATH, SOUL_SYNC

log = logging.getLogger("companion")

# この行を含む SOUL.md は companion が管理する（外部生成物との区別）
MARKER = "<!-- generated-by: companion soul.py -->"


def render_soul() -> str:
    """persona.md + persona_notes.md から SOUL.md の本文を組み立てる"""
    persona = (
        PERSONA_PATH.read_text(encoding="utf-8").strip()
        if PERSONA_PATH.exists()
        else ""
    )
    notes = (
        PERSONA_NOTES_PATH.read_text(encoding="utf-8").strip()
        if PERSONA_NOTES_PATH.exists()
        else ""
    )
    parts = [
        MARKER,
        "<!-- source: companion persona.md + data/persona_notes.md."
        " 編集は companion 側で。手編集は次回同期で上書きされる -->",
        "",
    ]
    if persona:
        parts.append(persona)
    if notes:
        parts += [
            "",
            "## 自分についてのメモ（本人が書き足したもの）",
            notes,
        ]
    return "\n".join(parts).rstrip() + "\n"


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sync_soul() -> str:
    """SOUL.md を render 結果に同期する。状態語を返す（skipped/noop/updated/backed-up）"""
    if not SOUL_SYNC:
        return "skipped"
    if not SOUL_PATH.parent.is_dir():
        # Hermes 未導入環境では何もしない
        return "skipped"
    text = render_soul()
    existing = ""
    try:
        if SOUL_PATH.exists():
            existing = SOUL_PATH.read_text(encoding="utf-8")
    except OSError:
        log.exception("SOUL.md read failed")
        return "skipped"
    if existing == text:
        return "noop"
    if existing and MARKER not in existing:
        # companion 管理外の既存ファイルは一度だけ退避してから置き換える
        bak = SOUL_PATH.with_name(
            f"SOUL.md.bak-{datetime.now():%Y%m%d-%H%M%S}"
        )
        try:
            os.replace(SOUL_PATH, bak)
            log.warning("existing SOUL.md was not companion-managed; backed up to %s", bak)
        except OSError:
            log.exception("SOUL.md backup failed; leaving it untouched")
            return "skipped"
    try:
        _write_atomic(SOUL_PATH, text)
    except OSError:
        log.exception("SOUL.md write failed")
        return "skipped"
    log.info("SOUL.md synced (%d chars)", len(text))
    return "updated"
