import asyncio
import collections
import logging
from pathlib import Path

from ..config import OBSIDIAN_SYNC_DIR, OBSIDIAN_VAULT
from ..db import get_db
from ..memory import all_user

log = logging.getLogger("companion")

_AUTO_NOTE = (
    "<!-- companion が自動生成。手で編集しても次回同期で上書きされます -->\n\n"
)


def _w_vault(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_AUTO_NOTE + text, encoding="utf-8")


async def sync_obsidian() -> str:
    """mem0の記憶とSQLiteの状態をvault内 markdown に一方向出力する"""
    if not OBSIDIAN_VAULT:
        return "OBSIDIAN_VAULT が未設定です"
    base = Path(OBSIDIAN_VAULT) / OBSIDIAN_SYNC_DIR

    res = await asyncio.to_thread(
        all_user, top_k=10000
    )
    items = res.get("results", []) if isinstance(res, dict) else (res or [])
    by_month = collections.defaultdict(list)
    for it in items:
        month = str(it.get("created_at") or "")[:7] or "日付不明"
        by_month[month].append(it.get("memory", ""))
    for month, facts in sorted(by_month.items()):
        _w_vault(
            base / "記憶" / f"{month}.md",
            f"# 記憶 {month}（{len(facts)}件）\n\n"
            + "\n".join(f"- {m}" for m in facts),
        )

    with get_db() as conn:
        tasks = conn.execute(
            "SELECT id,title,due,note,done FROM tasks ORDER BY done, due IS NULL, due"
        ).fetchall()
        litems = conn.execute(
            "SELECT list_name,id,item,note FROM lists ORDER BY list_name, id"
        ).fetchall()
        rems = conn.execute(
            "SELECT id,text,due_at,fired FROM reminders ORDER BY due_at"
        ).fetchall()

    _w_vault(
        base / "タスク.md",
        "# タスク\n\n"
        + (
            "\n".join(
                f"- [{'x' if r[4] else ' '}] #{r[0]} {r[1]}"
                + (f"（期限 {r[2]}）" if r[2] else "")
                + (f" — {r[3]}" if r[3] else "")
                for r in tasks
            )
            or "なし"
        ),
    )

    buf, cur = ["# リスト\n"], None
    for ln, i, item, note in litems:
        if ln != cur:
            buf.append(f"\n## {ln}\n")
            cur = ln
        buf.append(f"- #{i} {item}" + (f" — {note}" if note else ""))
    _w_vault(base / "リスト.md", "\n".join(buf) + ("\n" if litems else "なし\n"))

    _w_vault(
        base / "リマインダー.md",
        "# リマインダー\n\n"
        + (
            "\n".join(
                f"- [{'x' if r[3] else ' '}] {r[2]} — {r[1]}" for r in rems
            )
            or "なし"
        ),
    )

    log.info("obsidian sync done: %d memories", len(items))
    return f"Obsidianに同期しました（記憶{len(items)}件）"
