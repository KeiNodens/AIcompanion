from ..db import get_db


def list_add(list_name: str, item: str, note: str | None = None) -> str:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO lists (list_name, item, note) VALUES (?,?,?)",
            (list_name, item, note),
        )
    return f"「{list_name}」リストに「{item}」を追加"


def list_show(list_name: str) -> str:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,item,note FROM lists WHERE list_name LIKE ? ORDER BY id",
            (f"%{list_name}%",),
        ).fetchall()
    if not rows:
        return f"「{list_name}」リストは空です"
    return f"## {list_name}\n" + "\n".join(
        f"#{r[0]} {r[1]}" + (f" ({r[2]})" if r[2] else "") for r in rows
    )


def list_remove(query: str | None = None, item_id: int | None = None) -> str:
    with get_db() as conn:
        if item_id is not None:
            row = conn.execute("SELECT id,item FROM lists WHERE id=?", (item_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT id,item FROM lists WHERE item LIKE ? ORDER BY id LIMIT 1",
                (f"%{query}%",),
            ).fetchone()
        if not row:
            return "該当する項目が見つかりません"
        conn.execute("DELETE FROM lists WHERE id=?", (row[0],))
    return f"「{row[1]}」をリストから削除しました"


def list_names() -> str:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT list_name, COUNT(*) FROM lists GROUP BY list_name"
        ).fetchall()
    if not rows:
        return "リストはまだありません"
    return "\n".join(f"- {r[0]} ({r[1]}件)" for r in rows)
