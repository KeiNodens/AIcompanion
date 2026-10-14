import logging
import re
from datetime import datetime
from pathlib import Path

from ..config import OBSIDIAN_MEDIA_DIR, OBSIDIAN_VAULT
from ..memory import add_user_fact

log = logging.getLogger("companion")


def media_dir() -> Path | None:
    if not OBSIDIAN_VAULT:
        return None
    d = Path(OBSIDIAN_VAULT) / OBSIDIAN_MEDIA_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def media_log(title: str, kind: str = "作品", note: str = "", rating: int | None = None) -> str:
    d = media_dir()
    if d is None:
        return "OBSIDIAN_VAULT が未設定です"
    safe = re.sub(r'[\\/:*?"<>|]', "_", title).strip().rstrip(".")
    if not safe:
        return "タイトルが空です"
    p = d / f"{safe}.md"
    today = datetime.now().strftime("%Y-%m-%d")
    star = f"（評価{rating}/5）" if rating else ""
    line = f"{today} {kind}{star}: {note or '記録'}"
    body = p.read_text(encoding="utf-8") + "\n\n" + line if p.exists() else line + "\n"
    p.write_text(body, encoding="utf-8")
    try:
        add_user_fact(
            f"[{today}] ユーザーは{kind}「{title}」を記録した。{note or ''}")
    except Exception:
        log.exception("media_log fact write failed")
    return f"{kind}「{title}」をvaultに記録しました"


def media_list() -> str:
    d = media_dir()
    if d is None:
        return "OBSIDIAN_VAULT が未設定です"
    names = sorted(p.stem for p in d.glob("*.md") if p.stat().st_size > 0)
    if not names:
        return "記録された作品はまだありません"
    return f"## 記録済み作品 ({len(names)})\n" + "\n".join(f"- {n}" for n in names[:50])
