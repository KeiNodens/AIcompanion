from datetime import datetime

from ..db import get_db, resolve_notify_channel
from .actor import CURRENT_CHANNEL


def _parse_due(s: str) -> str | None:
    s = s.strip().replace("T", " ").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            if fmt == "%Y-%m-%d":
                d = d.replace(hour=9, minute=0)
            return d.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return None


def remind_at(when: str, text: str, channel_id: int = 0) -> str:
    due = _parse_due(when)
    if not due:
        return f"時刻「{when}」を解釈できませんでした（YYYY-MM-DD HH:MM 形式で）"
    if due < datetime.now().strftime("%Y-%m-%d %H:%M"):
        return f"{due} は過去の時刻です。未来の時刻を指定してください"
    channel_id = channel_id or resolve_notify_channel()
    if not channel_id:
        return (
            "リマインドの送り先チャンネルが分かりませんでした。"
            "Discordのチャンネルで依頼するか、NOTIFY_CHANNEL_ID を設定してください"
        )
    with get_db() as conn:
        conn.execute(
            "INSERT INTO reminders (channel_id, text, due_at) VALUES (?,?,?)",
            (channel_id, text, due),
        )
    return f"{due} に「{text}」をリマインド"


def reminders_list() -> str:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,text,due_at FROM reminders WHERE fired=0 ORDER BY due_at"
        ).fetchall()
    if not rows:
        return "リマインダーはありません"
    return "\n".join(f"#{r[0]} {r[2]} — {r[1]}" for r in rows)


def reminder_cancel(reminder_id: int | None = None, query: str | None = None) -> str:
    with get_db() as conn:
        if reminder_id is not None:
            row = conn.execute(
                "SELECT id,text FROM reminders WHERE id=? AND fired=0", (reminder_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id,text FROM reminders WHERE fired=0 AND text LIKE ? LIMIT 1",
                (f"%{query}%",),
            ).fetchone()
        if not row:
            return "該当するリマインダーが見つかりません"
        # 未送信の通知待ちも同じトランザクションで取り消す
        conn.execute(
            "DELETE FROM notify_outbox WHERE dedupe_key=? AND status='pending'",
            (f"reminder:{row[0]}",),
        )
        conn.execute("DELETE FROM reminders WHERE id=?", (row[0],))
    return f"リマインダー「{row[1]}」を削除しました"


def remind_handler(a):
    # 依頼されたチャンネル宛に固定する。別の発言で last_channel が変わっても
    # この依頼の通知先は変えない。デスクトップ等チャンネル不明の依頼は
    # resolve_notify_channel() の既定経路（env > 保存済み > 最終チャンネル）へ。
    return remind_at(
        a.get("when", ""), a.get("text", ""),
        channel_id=CURRENT_CHANNEL.get() or 0,
    )
