from ..db import get_db


def tasks_add(title: str, due: str | None = None, note: str | None = None) -> str:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, due, note) VALUES (?,?,?)", (title, due, note)
        )
        return f"タスク#{cur.lastrowid}「{title}」を追加" + (f"（期限: {due}）" if due else "")


def fmt_tasks(rows) -> str:
    if not rows:
        return "タスクはありません"
    lines = []
    for r in rows:
        due = f" [期限:{r[2]}]" if r[2] else ""
        note = f" ({r[3]})" if r[3] else ""
        lines.append(f"#{r[0]} {r[1]}{due}{note}")
    return "\n".join(lines)


def tasks_list(include_done: bool = False) -> str:
    with get_db() as conn:
        sql = "SELECT id,title,due,note FROM tasks"
        if not include_done:
            sql += " WHERE done=0"
        rows = conn.execute(sql + " ORDER BY done, due IS NULL, due").fetchall()
    return fmt_tasks(rows)


def _find_task(conn, task_id=None, query=None):
    if task_id is not None:
        return conn.execute(
            "SELECT id,title FROM tasks WHERE id=? AND done=0", (task_id,)
        ).fetchone()
    if query:
        return conn.execute(
            "SELECT id,title FROM tasks WHERE done=0 AND title LIKE ? ORDER BY id LIMIT 1",
            (f"%{query}%",),
        ).fetchone()
    return None


def tasks_complete(task_id: int | None = None, query: str | None = None) -> str:
    with get_db() as conn:
        row = _find_task(conn, task_id, query)
        if not row:
            return "該当するタスクが見つかりません"
        conn.execute("UPDATE tasks SET done=1 WHERE id=?", (row[0],))
    return f"タスク#{row[0]}「{row[1]}」を完了にしました"


def tasks_delete(task_id: int | None = None, query: str | None = None) -> str:
    with get_db() as conn:
        row = _find_task(conn, task_id, query)
        if not row:
            return "該当するタスクが見つかりません"
        conn.execute("DELETE FROM tasks WHERE id=?", (row[0],))
    return f"タスク#{row[0]}「{row[1]}」を削除しました"
