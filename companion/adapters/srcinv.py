"""adapters.srcinv —— L-MIG Phase 0/2 の移行元目録収集（不純 I/O 端）。

本番 source（mem0・history.db・companion.db・persona 等・state.json・
blob 群）を読み、`transcribe.build_inventory` が期待する形で返す。
完全停止中に呼ぶ前提。DB は読み取りのみ開く。
"""

from __future__ import annotations

import glob
import hashlib
import os
import sqlite3
from pathlib import Path

_DB_TABLES = ["tasks", "lists", "reminders", "approvals", "delegate_jobs"]
_STATE_KEYS = ["inner", "llm", "music_route", "mood", "energy"]


def _sha_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_mem0_items(mem0, *, user_id: str, agent_id: str,
                       limit: int = 50000) -> list:
    """mem0 seam 経由で user/agent 両 scope の項目を全件取得する。"""
    out = []
    for flt in ({"user_id": user_id}, {"agent_id": agent_id}):
        try:
            res = mem0.get_all(filters=flt, top_k=limit)
        except Exception:
            res = None
        items = res.get("results") if isinstance(res, dict) else res
        for it in items or []:
            if isinstance(it, dict) and it not in out:
                it = dict(it)
                it.setdefault("metadata", {})
                if "agent_id" in flt:
                    it["metadata"].setdefault("agent_id", agent_id)
                out.append(it)
    return out


def collect_history(path) -> list:
    """history.db（mem0 update/delete 履歴）。"""
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(history)")]
        pick = [c for c in ("id", "memory_id", "old_memory", "new_memory",
                            "event", "created_at") if c in cols]
        rows = []
        for r in conn.execute(f"SELECT {', '.join(pick)} FROM history"):
            rows.append(dict(zip(pick, r)))
        return rows
    except Exception:
        return []
    finally:
        conn.close()


def collect_messages(path) -> list:
    """history.db の messages 表（mem0 が保持していた会話写し）。

    turns 表の正本と内容が重複するため、会話イベントにはせず
    migration event（監査写し）として移行する（Q-O17 決着）。
    """
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(messages)")]
        except sqlite3.Error:
            return []
        if not cols:
            return []
        return [dict(zip(cols, r))
                for r in conn.execute(
                    f"SELECT {', '.join(cols)} FROM messages ORDER BY id")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def collect_turns(db_path) -> list:
    """turns 表（会話往復）。inner_text も写像対象。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(turns)")]
        pick = [c for c in ("id", "user_text", "assistant_text",
                            "inner_text", "ts", "source", "session_key",
                            "created_at") if c in cols]
        return [dict(zip(pick, r))
                for r in conn.execute(
                    f"SELECT {', '.join(pick)} FROM turns ORDER BY id")]
    finally:
        conn.close()


def collect_db_rows(db_path) -> dict:
    """Body 側表を列名つきで読む。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = {}
        for table in _DB_TABLES:
            try:
                cols = [r[1] for r in conn.execute(
                    f"PRAGMA table_info({table})")]
                if not cols:
                    out[table] = []
                    continue
                # 全列を拾う（id が無いと import_key が衝突するため）
                out[table] = [dict(zip(cols, r)) for r in conn.execute(
                    f"SELECT {', '.join(cols)} FROM {table}")]
            except sqlite3.Error:
                out[table] = []
        return out
    finally:
        conn.close()


def collect_files(persona_md, persona_notes) -> list:
    """persona.md（user 系）・persona_notes.md（agent 系）のテキスト。

    欠損ファイルは目録から外す（移行対象が存在しない＝失敗ではない）。
    """
    out = []
    for name, path in (("persona.md", persona_md),
                       ("persona_notes.md", persona_notes)):
        try:
            out.append({"name": name,
                        "content": Path(path).read_text(encoding="utf-8")})
        except Exception:
            continue
    return out


def collect_state(state_path) -> dict:
    """state.json の写像対象キーのみ抽出（資産ではない運用キーは除く）。"""
    from .. import db
    try:
        state = db.load_state() if state_path is None else _load(state_path)
    except Exception:
        return {}
    return {k: v for k, v in (state or {}).items() if k in _STATE_KEYS}


def _load(path) -> dict:
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def collect_blobs(data_dir) -> list:
    """data/docs/*.pdf（doc_ref 用）と data/delegate/*.log（delegate_log 用）。"""
    out = []
    dd = str(data_dir)
    for pattern, kind, table in (
            (os.path.join(dd, "docs", "*.pdf"), "doc_ref", "docs"),
            (os.path.join(dd, "delegate", "*.log"),
             "delegate_log", "delegate_logs")):
        for p in sorted(glob.glob(pattern)):
            try:
                out.append({"path": p, "kind": kind,
                            "source_table": table,
                            "hash": _sha_file(p),
                            "size": os.path.getsize(p)})
            except OSError:
                continue
    return out


def collect_inventory(*, mem0=None, user_id: str = "me",
                      agent_id: str = "companion", data_dir,
                      db_path, persona_md=None, persona_notes=None,
                      state_path=None) -> dict:
    """移行元目録一式。mem0=None で mem0 系を除外（unit test 用）。"""
    data_dir = str(data_dir)
    return {
        "mem0_items": collect_mem0_items(mem0, user_id=user_id,
                                         agent_id=agent_id)
        if mem0 is not None else [],
        "history_rows": collect_history(os.path.join(data_dir,
                                                     "history.db")),
        "message_rows": collect_messages(os.path.join(data_dir,
                                                      "history.db")),
        "turn_rows": collect_turns(str(db_path)),
        "files": collect_files(persona_md, persona_notes),
        "db_rows": collect_db_rows(str(db_path)),
        "state_values": collect_state(state_path),
        "blobs": collect_blobs(data_dir),
    }
