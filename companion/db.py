import json
import logging
import os
import secrets
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta

from .config import DB_PATH, STATE_PATH

log = logging.getLogger("companion")


_SCHEMA_VERSION = 7

_MIGRATIONS = {
    # v1: 処理の途中状態を永続化するテーブル群
    1: [
        """CREATE TABLE IF NOT EXISTS turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_key TEXT NOT NULL,
        user_text TEXT NOT NULL,
        assistant_text TEXT NOT NULL,
        ts TEXT,
        source TEXT,
        mem0_status TEXT NOT NULL DEFAULT 'pending',
        mem0_attempts INTEGER NOT NULL DEFAULT 0,
        mem0_error TEXT,
        compressed INTEGER NOT NULL DEFAULT 0,
        episode_done INTEGER NOT NULL DEFAULT 0,
        inner_text TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""",
        """CREATE TABLE IF NOT EXISTS recent_context_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        text TEXT NOT NULL,
        updated_at TEXT
    )""",
        """CREATE TABLE IF NOT EXISTS notify_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedupe_key TEXT UNIQUE NOT NULL,
        channel_id INTEGER NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_at TEXT,
        last_error TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        sent_at TEXT
    )""",
        """CREATE TABLE IF NOT EXISTS job_runs (
        job TEXT NOT NULL,
        run_date TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        detail TEXT,
        updated_at TEXT,
        PRIMARY KEY (job, run_date)
    )""",
        """CREATE TABLE IF NOT EXISTS reflection_state (
        run_date TEXT PRIMARY KEY,
        insights TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""",
    ],
    # v2: 委譲ジョブの本人承認レコード（LLMの判断と分離）
    2: [
        """CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        requester TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        expires_at TEXT,
        decided_by TEXT,
        decided_at TEXT
    )""",
    ],
    # v3: Core skill registry（runtime-hermes.md 項目E）。
    # 技能の中身は data/skills/<name>/ のファイル群、DBは状態と出自の正本。
    # status: staged（承認待ち・プロンプトに出ない）→ active → disabled
    #         / rejected（取込時の検査不合格）
    3: [
        """CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        scopes TEXT,
        origin TEXT NOT NULL DEFAULT 'imported',
        status TEXT NOT NULL DEFAULT 'staged',
        content_hash TEXT NOT NULL,
        source_path TEXT,
        rel_path TEXT,
        note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        decided_by TEXT,
        decided_at TEXT
    )""",
    ],
    # v4: 身体側 run の記録とツール呼出しトレース（runtime-hermes-tooling.md B段）。
    # body_runs は Hermes 側 run の Core 側正本、turn_tool_calls は会話 turn と
    # body run のツール呼出し履歴。approvals は kind で委譲（delegate）と
    # 身体側承認（body_run: job_id=0, requester='body'）を分ける
    4: [
        """CREATE TABLE IF NOT EXISTS body_runs (
        run_id TEXT PRIMARY KEY,
        label TEXT,
        body_session TEXT,
        origin_session TEXT,
        task TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        last_seq INTEGER NOT NULL DEFAULT -1,
        output TEXT,
        error TEXT,
        report_status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT,
        finished_at TEXT
    )""",
        """CREATE TABLE IF NOT EXISTS turn_tool_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        turn_id INTEGER,
        session_key TEXT,
        origin TEXT NOT NULL,
        ref TEXT,
        seq INTEGER,
        tool TEXT NOT NULL,
        args_json TEXT,
        status TEXT,
        result_excerpt TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        finished_at TEXT
    )""",
        "CREATE INDEX IF NOT EXISTS idx_ttc_turn ON turn_tool_calls(turn_id)",
        "CREATE INDEX IF NOT EXISTS idx_ttc_ref ON turn_tool_calls(origin, ref)",
        "ALTER TABLE approvals ADD COLUMN kind TEXT NOT NULL DEFAULT 'delegate'",
        "ALTER TABLE approvals ADD COLUMN ref TEXT",
        "ALTER TABLE approvals ADD COLUMN code TEXT",
        "ALTER TABLE approvals ADD COLUMN summary TEXT",
    ],
    # v5: R4 スキル面（runtime-hermes-tooling.md §7.4/§7.3）。
    # requires_tools は frontmatter の宣言（索引・承認時に所持ツールと照合して
    # 「⚠使えないtoolあり」を出す）。upstream_gone_at は §7.3 照合で
    # Hermes 側の技能一覧から消えたことを検知した時刻（自動 disable はしない）
    5: [
        "ALTER TABLE skills ADD COLUMN requires_tools TEXT",
        "ALTER TABLE skills ADD COLUMN upstream_gone_at TEXT",
    ],
    # v6: 案A。往復を起こした呼出し元 Hermes profile（profile_policy 判定で
    # 解決した値。None=未解決/ローカル入口）を turns に刻み、後から
    # 「どの bot との会話か」を追跡できるようにする
    6: [
        "ALTER TABLE turns ADD COLUMN profile TEXT",
    ],
    # v7: body run の実行先 backend（research/design/codex-body.md）。
    # 既存行は全て Hermes 経由なので既定値 'hermes' で埋まる
    7: [
        "ALTER TABLE body_runs ADD COLUMN backend TEXT NOT NULL"
        " DEFAULT 'hermes'",
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    """バージョン付きスキーマ移行。各部分移行はトランザクションでまとめる"""
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    for ver in sorted(_MIGRATIONS):
        if v >= ver:
            continue
        try:
            conn.execute("BEGIN")
            for stmt in _MIGRATIONS[ver]:
                conn.execute(stmt)
            if ver == 1:
                cols = [
                    r[1] for r in conn.execute(
                        "PRAGMA table_info(delegate_jobs)"
                    ).fetchall()
                ]
                if "notify_status" not in cols:
                    conn.execute(
                        "ALTER TABLE delegate_jobs ADD COLUMN notify_status TEXT"
                    )
                    # 既存の終了済みジョブは通知済み扱い（未通知分を推測再送しない）
                    conn.execute(
                        "UPDATE delegate_jobs SET notify_status='sent' "
                        "WHERE status IN "
                        "('done','failed','timeout','cancelled','interrupted')"
                    )
            conn.execute(f"PRAGMA user_version={ver}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tasks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "title TEXT NOT NULL, due TEXT, note TEXT, done INTEGER DEFAULT 0, "
        "created_at TEXT DEFAULT (datetime('now','localtime')))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS lists ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "list_name TEXT NOT NULL, item TEXT NOT NULL, note TEXT, "
        "created_at TEXT DEFAULT (datetime('now','localtime')))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reminders ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "channel_id INTEGER, text TEXT NOT NULL, due_at TEXT NOT NULL, "
        "fired INTEGER DEFAULT 0, "
        "created_at TEXT DEFAULT (datetime('now','localtime')))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS delegate_jobs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "prompt TEXT, request TEXT NOT NULL, workdir TEXT NOT NULL, "
        "model TEXT, effort TEXT, "
        "status TEXT NOT NULL DEFAULT 'pending', "
        "exit_code INTEGER, "
        "created_at TEXT DEFAULT (datetime('now','localtime')), "
        "started_at TEXT, finished_at TEXT)"
    )
    _migrate(conn)
    return conn


_state_lock = threading.RLock()


def load_state() -> dict:
    """state.json を読む。

    壊れたJSONやdict以外の内容は原本を .broken-* に退避して空扱いにする。
    空を返しても原本は残るので、後続の保存で証拠が消えない。
    """
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        data = None
    if isinstance(data, dict):
        return data
    broken = STATE_PATH.with_name(
        f"{STATE_PATH.name}.broken-{datetime.now():%Y%m%d%H%M%S}"
    )
    try:
        os.replace(STATE_PATH, broken)
        log.error("state.json が壊れていたため %s に退避しました", broken.name)
    except OSError:
        log.error("state.json が壊れています（退避に失敗）")
    return {}


def _write_state(state: dict) -> None:
    """同一ディレクトリの一時ファイルに書いてから原子置換する。
    失敗時は一時ファイルを消し、既存の state.json をそのまま残す。"""
    fd, tmp = tempfile.mkstemp(
        dir=str(STATE_PATH.parent), prefix=STATE_PATH.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False))
        os.replace(tmp, STATE_PATH)
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


def update_state(mutator=None, *, remove=(), **kw) -> dict:
    """state.json をロック下で read-modify-write し、原子置換する。

    mutator(state): ロック内で最新の state dict を受け取って直接変更する
    （ネストした値の読み改め書き・複数キーの整合更新は必ずこちら経由）。
    remove: 削除するキー。**kw: 単純な上書き。
    """
    with _state_lock:
        state = load_state()
        if mutator is not None:
            mutator(state)
        for k in remove:
            state.pop(k, None)
        state.update(kw)
        _write_state(state)
        return state


def save_state(**kw) -> None:
    update_state(**kw)


def delete_state(*keys) -> None:
    update_state(remove=keys)


_last_channel_id = [0]


def set_last_channel(channel_id: int):
    _last_channel_id[0] = channel_id


def get_last_channel() -> int:
    return _last_channel_id[0]


def resolve_notify_channel() -> int:
    """定期通知・リマインドの既定宛先: env > 保存済み > メモリ上の最終チャンネル"""
    from . import config

    return (
        config.NOTIFY_CHANNEL_ID
        or load_state().get("last_channel_id", 0)
        or get_last_channel()
    )


# ---------------------------------------------------------------------------
# approvals 共通: 4桁決裁コードの採番（body_run / delegate 共用）
# ---------------------------------------------------------------------------

def new_approval_code(conn) -> str:
    """pending 中で重複しない4桁コードを引く"""
    for _ in range(100):
        code = f"{secrets.randbelow(10000):04d}"
        row = conn.execute(
            "SELECT 1 FROM approvals WHERE code=? AND status='pending'",
            (code,),
        ).fetchone()
        if row is None:
            return code
    raise RuntimeError("approval code space exhausted")


# ---------------------------------------------------------------------------
# turns: 会話の往復の永続化（mem0保存・圧縮の処理状態を追跡）
# ---------------------------------------------------------------------------

def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def record_turn(session_key, user_text: str, assistant_text: str,
                ts: str | None = None, source: str | None = None,
                inner_text: str | None = None,
                profile: str | None = None) -> int:
    """完了した往復を永続化。以後の処理（mem0/圧縮/経験化）はこの行を起点に追跡する"""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO turns"
            " (session_key, user_text, assistant_text, ts, source, inner_text,"
            "  profile)"
            " VALUES (?,?,?,?,?,?,?)",
            (str(session_key), user_text, assistant_text,
             ts or _now_str(), source, inner_text, profile),
        )
        return cur.lastrowid


def count_foldable_turns(keep_turns: int) -> int:
    """直近 keep_turns 往復より古い未圧縮ターン数（圧縮トリガー判定用）"""
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM turns WHERE compressed=0 AND id NOT IN"
            " (SELECT id FROM turns ORDER BY id DESC LIMIT ?)",
            (keep_turns,),
        ).fetchone()[0]


def foldable_turns(keep_turns: int, limit: int = 40):
    """圧縮対象: 直近 keep_turns 往復を除く未圧縮ターンを古い順で"""
    with get_db() as conn:
        return conn.execute(
            "SELECT id, user_text, assistant_text FROM turns"
            " WHERE compressed=0 AND id NOT IN"
            " (SELECT id FROM turns ORDER BY id DESC LIMIT ?)"
            " ORDER BY id LIMIT ?",
            (keep_turns, limit),
        ).fetchall()


def commit_compressed_turns(ids, text: str) -> None:
    """圧縮結果と消費済み範囲を同じトランザクションで確定する"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO recent_context_state (id, text, updated_at)"
            " VALUES (1,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " text=excluded.text, updated_at=excluded.updated_at",
            (text, _now_str()),
        )
        conn.executemany(
            "UPDATE turns SET compressed=1 WHERE id=?", [(i,) for i in ids]
        )


def get_recent_context_text() -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT text FROM recent_context_state WHERE id=1"
        ).fetchone()
    return row[0] if row else ""


def seed_recent_context(file_text: str) -> None:
    """初回導入時だけ recent_context.md の本文をDB側の初期値として引き継ぐ"""
    if get_recent_context_text() or not (file_text or "").strip():
        return
    with get_db() as conn:
        conn.execute(
            "INSERT INTO recent_context_state (id, text, updated_at)"
            " VALUES (1,?,?) ON CONFLICT(id) DO NOTHING",
            (file_text, _now_str()),
        )


def mark_turn_mem0_done(turn_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE turns SET mem0_status='done',"
            " mem0_attempts=mem0_attempts+1, mem0_error=NULL WHERE id=?",
            (turn_id,),
        )


def mark_turn_mem0_failed(turn_id: int, error: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE turns SET mem0_status='failed',"
            " mem0_attempts=mem0_attempts+1, mem0_error=? WHERE id=?",
            (error, turn_id),
        )


def turns_pending_mem0(limit: int = 10, max_attempts: int = 5):
    """mem0保存の再試行対象。'unknown'（起き上がり時の半書込み）は自動再実行しない"""
    with get_db() as conn:
        return conn.execute(
            "SELECT id, user_text, assistant_text, ts FROM turns"
            " WHERE mem0_status='failed' AND mem0_attempts<?"
            " ORDER BY id LIMIT ?",
            (max_attempts, limit),
        ).fetchall()


def sweep_stale_pending_turns() -> int:
    """起動時: pending のまま残ったターンは半書込みの可能性があるので unknown に"""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE turns SET mem0_status='unknown' WHERE mem0_status='pending'"
        )
        return cur.rowcount


def turns_pending_episode(limit: int = 30):
    """自己経験メモの未処理材料（古い順）。LLM/save失敗時も行は残る"""
    with get_db() as conn:
        return conn.execute(
            "SELECT id, session_key, user_text, assistant_text, inner_text, ts"
            " FROM turns WHERE episode_done=0 ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()


def mark_turns_episode_done(ids) -> None:
    """経験メモとして処理しきった往復だけを確定する（部分失敗時は呼ばない）"""
    with get_db() as conn:
        conn.executemany(
            "UPDATE turns SET episode_done=1 WHERE id=?", [(i,) for i in ids]
        )


def search_turns(query: str, profile: str | None = None, limit: int = 20):
    """turns 原文の部分一致検索（新しい順）。profile で特定 bot の往復に絞れる。

    turns は保持期間で掃除される前提なので LIKE 全走査で十分軽い。
    固有名詞・正確な文言の掘り起こし用（意味検索は mem0 側の役目）"""
    like = f"%{query}%"
    sql = (
        "SELECT id, session_key, user_text, assistant_text, ts, source, profile"
        " FROM turns WHERE (user_text LIKE ? OR assistant_text LIKE ?)"
    )
    args: list = [like, like]
    if profile:
        sql += " AND profile=?"
        args.append(profile)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with get_db() as conn:
        return conn.execute(sql, args).fetchall()


def purge_old_turns(days: int = 30) -> int:
    """圧縮・mem0保存・経験化がすべて完了した古い原文だけを掃除する。
    未処理分は材料として残す。turns 削除後に turn_tool_calls の孤児と、
    報告済みで保持期間を過ぎた body_runs（とその tool 記録）も消す"""
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM turns WHERE compressed=1 AND mem0_status='done'"
            " AND episode_done=1"
            " AND created_at < datetime('now','localtime', ?)",
            (f"-{days} days",),
        )
        conn.execute(
            "DELETE FROM turn_tool_calls WHERE turn_id IS NOT NULL"
            " AND turn_id NOT IN (SELECT id FROM turns)"
        )
        marks = ",".join("?" for _ in BODY_RUN_TERMINAL)
        old_runs = conn.execute(
            f"SELECT run_id FROM body_runs WHERE status IN ({marks})"
            " AND report_status IN ('inline','done','failed')"
            " AND finished_at IS NOT NULL"
            " AND finished_at < datetime('now','localtime', ?)",
            (*BODY_RUN_TERMINAL, f"-{days} days"),
        ).fetchall()
        run_ids = [r[0] for r in old_runs]
        if run_ids:
            rmarks = ",".join("?" for _ in run_ids)
            conn.execute(
                f"DELETE FROM turn_tool_calls WHERE origin='body_run'"
                f" AND ref IN ({rmarks})",
                run_ids,
            )
            conn.execute(
                f"DELETE FROM body_runs WHERE run_id IN ({rmarks})", run_ids
            )
        return cur.rowcount


def pending_outbox_summary() -> dict:
    """未処理の滞留状況（将来のdoctorコマンドや通知用の集計）"""
    with get_db() as conn:
        turns_failed = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE mem0_status='failed'"
        ).fetchone()[0]
        turns_unknown = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE mem0_status='unknown'"
        ).fetchone()[0]
        notify_pending = conn.execute(
            "SELECT COUNT(*) FROM notify_outbox WHERE status='pending'"
        ).fetchone()[0]
        notify_attention = conn.execute(
            "SELECT COUNT(*) FROM notify_outbox WHERE status='needs_attention'"
        ).fetchone()[0]
    return {
        "turns_mem0_failed": turns_failed,
        "turns_mem0_unknown": turns_unknown,
        "notify_pending": notify_pending,
        "notify_needs_attention": notify_attention,
    }


# ---------------------------------------------------------------------------
# notify_outbox: 通知の送信待ち（送信成功まで fired/完了扱いにしない）
# ---------------------------------------------------------------------------

def enqueue_notification(dedupe_key: str, channel_id: int, body: str) -> bool:
    """送信待ちを記録。dedupe_key 重複は追加しない（冪等）。追加したらTrue"""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notify_outbox (dedupe_key, channel_id, body)"
            " VALUES (?,?,?)",
            (dedupe_key, int(channel_id), body),
        )
        return cur.rowcount > 0


def due_notifications(limit: int = 20):
    with get_db() as conn:
        return conn.execute(
            "SELECT id, dedupe_key, channel_id, body, attempts"
            " FROM notify_outbox"
            " WHERE status='pending'"
            " AND (next_retry_at IS NULL OR next_retry_at<=?)"
            " ORDER BY id LIMIT ?",
            (_now_str(), limit),
        ).fetchall()


def mark_notification_sent(nid: int) -> None:
    """送信確定。起点（リマインダー/委譲ジョブ）の完了も同じトランザクションで確定"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT dedupe_key FROM notify_outbox WHERE id=?", (nid,)
        ).fetchone()
        conn.execute(
            "UPDATE notify_outbox SET status='sent', sent_at=? WHERE id=?",
            (_now_str(), nid),
        )
        if row and row[0].startswith("reminder:"):
            try:
                rid = int(row[0].split(":", 1)[1])
            except ValueError:
                rid = 0
            if rid:
                conn.execute("UPDATE reminders SET fired=1 WHERE id=?", (rid,))
        elif row and row[0].startswith("delegate_done:"):
            try:
                jid = int(row[0].split(":", 1)[1])
            except ValueError:
                jid = 0
            if jid:
                conn.execute(
                    "UPDATE delegate_jobs SET notify_status='sent' WHERE id=?",
                    (jid,),
                )


def mark_notification_failed(nid: int, error: str, *, give_up: bool) -> None:
    """失敗を記録。give_up でも 'needs_attention' に留め、成功扱いにはしない"""
    with get_db() as conn:
        if give_up:
            conn.execute(
                "UPDATE notify_outbox SET status='needs_attention',"
                " attempts=attempts+1, last_error=? WHERE id=?",
                (error, nid),
            )
        else:
            attempts = conn.execute(
                "SELECT attempts FROM notify_outbox WHERE id=?", (nid,)
            ).fetchone()[0]
            wait_min = min(2 ** attempts, 60)
            next_at = (datetime.now() + timedelta(minutes=wait_min)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conn.execute(
                "UPDATE notify_outbox SET attempts=attempts+1, last_error=?,"
                " next_retry_at=? WHERE id=?",
                (error, next_at, nid),
            )


# ---------------------------------------------------------------------------
# job_runs: 日次/週次処理の実行単位（失敗は当日中の限定再試行に留める）
# ---------------------------------------------------------------------------

def job_run_begin(job: str, run_date: str, *,
                  legacy_done: bool = False, max_attempts: int = 3) -> bool:
    """実行を開始してよければ True（'running' に遷移）。

    legacy_done: 移行前の state.json マーカーが同日完了を示すとき True で呼ぶ。
    失敗('failed')は max_attempts まで当日再試行できる。
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, attempts FROM job_runs WHERE job=? AND run_date=?",
            (job, run_date),
        ).fetchone()
        if row is None:
            status = "done" if legacy_done else "running"
            conn.execute(
                "INSERT INTO job_runs (job, run_date, status, attempts, updated_at)"
                " VALUES (?,?,?,?,?)",
                (job, run_date, status, 0 if legacy_done else 1, _now_str()),
            )
            return not legacy_done
        status, attempts = row
        if status == "failed" and attempts < max_attempts:
            conn.execute(
                "UPDATE job_runs SET status='running',"
                " attempts=attempts+1, updated_at=? WHERE job=? AND run_date=?",
                (_now_str(), job, run_date),
            )
            return True
        return False


def job_run_finish(job: str, run_date: str, status: str,
                   detail: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE job_runs SET status=?, detail=?, updated_at=?"
            " WHERE job=? AND run_date=?",
            (status, detail, _now_str(), job, run_date),
        )


def sweep_running_jobs() -> int:
    """起動時: running のまま残った実行は中断済みなので failed に掃き替える"""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE job_runs SET status='failed', updated_at=?"
            " WHERE status='running'",
            (_now_str(),),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# reflection_state: 夜間振り返りの部分完了（洞察候補→個別保存の進捗）
# ---------------------------------------------------------------------------

def get_reflection_insights(run_date: str):
    """確定済みの洞察候補をJSONで返す。未生成ならNone"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT insights FROM reflection_state WHERE run_date=?",
            (run_date,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def save_reflection_insights(run_date: str, insights: list) -> None:
    """生成した洞察候補を先に確定する（この後の保存途中での中断でも再生成しない）"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO reflection_state (run_date, insights)"
            " VALUES (?,?) ON CONFLICT(run_date) DO UPDATE SET"
            " insights=excluded.insights",
            (run_date, json.dumps(insights, ensure_ascii=False)),
        )


# ---------------------------------------------------------------------------
# skills: Core skill registry。技能ファイルは data/skills/ 配下、
# DB は状態（staged/active/disabled/rejected）と provenance の正本
# ---------------------------------------------------------------------------

_SKILL_COLS = (
    "id, name, description, scopes, origin, status, content_hash,"
    " source_path, rel_path, note, created_at, decided_by, decided_at,"
    " requires_tools, upstream_gone_at"
)


def _skill_row(r) -> dict:
    return dict(zip(_SKILL_COLS.split(", "), r))


def skill_stage(name: str, description: str, scopes: str, origin: str,
                content_hash: str, source_path: str, rel_path: str,
                requires_tools: str = "", note: str | None = None) -> str:
    """取り込み候補を staged で登録する。

    - 未登録: INSERT → "new"
    - 同名が既にあって content_hash が同じ: 何もしない → "unchanged"
    - 同名で内容が変わった: 上書きして status を staged に戻す（再承認が要る）
      → "restaged"。disabled/rejected からも staged に戻る
      （新しい版なので upstream_gone の旗も降ろす）
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT content_hash FROM skills WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO skills"
                " (name, description, scopes, origin, status, content_hash,"
                "  source_path, rel_path, note, requires_tools)"
                " VALUES (?,?,?,?,'staged',?,?,?,?,?)",
                (name, description, scopes, origin, content_hash,
                 source_path, rel_path, note, requires_tools),
            )
            return "new"
        if row[0] == content_hash:
            return "unchanged"
        conn.execute(
            "UPDATE skills SET description=?, scopes=?, origin=?,"
            " status='staged', content_hash=?, source_path=?, rel_path=?,"
            " note=?, requires_tools=?, decided_by=NULL, decided_at=NULL,"
            " upstream_gone_at=NULL WHERE name=?",
            (description, scopes, origin, content_hash, source_path,
             rel_path, note, requires_tools, name),
        )
        return "restaged"


def skill_mark_rejected(name: str, reason: str, content_hash: str = "",
                        source_path: str = "") -> None:
    """検査不合格の記録（staged にはしない。同名 staged/active は触らない）"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM skills WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO skills"
                " (name, origin, status, content_hash, source_path, note)"
                " VALUES (?,'imported','rejected',?,?,?)",
                (name, content_hash, source_path, reason),
            )
        elif row[0] == "rejected":
            conn.execute(
                "UPDATE skills SET note=?, content_hash=?, source_path=?"
                " WHERE name=?",
                (reason, content_hash, source_path, name),
            )
        # staged/active/disabled の同名スキルを不合格ファイルで上書きしない


def skill_get(name: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {_SKILL_COLS} FROM skills WHERE name=?", (name,)
        ).fetchone()
    return _skill_row(row) if row else None


def skill_list(status: str | None = None) -> list[dict]:
    q = f"SELECT {_SKILL_COLS} FROM skills"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    q += " ORDER BY name"
    with get_db() as conn:
        return [_skill_row(r) for r in conn.execute(q, args).fetchall()]


def skill_set_status(name: str, status: str, decided_by: str | None = None,
                     note: str | None = None) -> bool:
    """status を遷移させる（approve/disable 用）。対象が無ければ False"""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE skills SET status=?, decided_by=?, decided_at=?,"
            " note=COALESCE(?, note) WHERE name=?",
            (status, decided_by, _now_str(), note, name),
        )
        return cur.rowcount > 0


def skills_staged_pending() -> list[dict]:
    """承認待ちの一覧（通知・確認用）"""
    return skill_list(status="staged")


def skill_set_upstream_gone(name: str, ts: str | None) -> None:
    """§7.3 upstream 照合の旗。ts=検知時刻 / None で解除（一覧に戻った）"""
    with get_db() as conn:
        conn.execute(
            "UPDATE skills SET upstream_gone_at=? WHERE name=?", (ts, name)
        )


# ---------------------------------------------------------------------------
# body_runs: 身体（Hermes）側 run の Core 側正本
# status: running / waiting_for_approval / completed / failed / cancelled /
#         interrupted / lost
# report_status: pending / inline（ツール応答で済ませた）/ done / failed
# ---------------------------------------------------------------------------

BODY_RUN_ACTIVE = ("running", "waiting_for_approval")
BODY_RUN_TERMINAL = (
    "completed", "failed", "cancelled", "interrupted", "lost",
)

_BODY_RUN_COLS = (
    "run_id, label, body_session, origin_session, task, status, last_seq,"
    " output, error, report_status, created_at, updated_at, finished_at,"
    " backend"
)


def _body_run_row(r) -> dict:
    return dict(zip(_BODY_RUN_COLS.split(", "), r))


def body_run_insert(run_id: str, task: str, label: str | None = None,
                    body_session: str | None = None,
                    origin_session: str | None = None,
                    backend: str = "hermes") -> None:
    """依頼した run を running で記録する（作成直後に呼ぶ）"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO body_runs"
            " (run_id, label, body_session, origin_session, task,"
            "  status, updated_at, backend)"
            " VALUES (?,?,?,?,?,'running',?,?)",
            (run_id, label, body_session, origin_session, task, _now_str(),
             backend or "hermes"),
        )


def body_run_get(run_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {_BODY_RUN_COLS} FROM body_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    return _body_run_row(row) if row else None


def body_run_update(run_id: str, **fields) -> None:
    """status/label/last_seq など任意の列を更新する"""
    cols = {c.strip() for c in _BODY_RUN_COLS.split(",")} - {
        "run_id", "created_at", "task",
    }
    sets = {k: v for k, v in fields.items() if k in cols}
    if not sets:
        return
    sets["updated_at"] = _now_str()
    q = ", ".join(f"{k}=?" for k in sets)
    with get_db() as conn:
        conn.execute(
            f"UPDATE body_runs SET {q} WHERE run_id=?",
            (*sets.values(), run_id),
        )


def body_run_set_seq(run_id: str, seq: int) -> None:
    """イベントの再生位置。後戻りしないよう単調増加だけ受ける"""
    with get_db() as conn:
        conn.execute(
            "UPDATE body_runs SET last_seq=? WHERE run_id=? AND last_seq<?",
            (seq, run_id, seq),
        )


def body_run_finish(run_id: str, status: str, output: str | None = None,
                    error: str | None = None, inline: bool = False) -> None:
    """終了状態を確定する。report_status は inline 指定時だけ 'inline'、
    それ以外は 'pending'（'inline' 済みの行は維持＝報告ターンを起こさない）"""
    now = _now_str()
    with get_db() as conn:
        conn.execute(
            "UPDATE body_runs SET status=?, output=?, error=?,"
            " finished_at=?, updated_at=?,"
            " report_status=CASE WHEN report_status='inline'"
            "   THEN 'inline' ELSE ? END"
            " WHERE run_id=?",
            (status, output, error, now, now,
             "inline" if inline else "pending", run_id),
        )


def body_run_report_status(run_id: str, status: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE body_runs SET report_status=?, updated_at=? WHERE run_id=?",
            (status, _now_str(), run_id),
        )


def body_runs_active() -> list[dict]:
    """watcher が追うべき進行中の run"""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {_BODY_RUN_COLS} FROM body_runs"
            " WHERE status IN ('running','waiting_for_approval')"
            " ORDER BY created_at",
        ).fetchall()
    return [_body_run_row(r) for r in rows]


def body_runs_pending_report() -> list[dict]:
    """終了済みでまだ報告していない run"""
    marks = ",".join("?" for _ in BODY_RUN_TERMINAL)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {_BODY_RUN_COLS} FROM body_runs"
            f" WHERE status IN ({marks}) AND report_status='pending'"
            " ORDER BY finished_at",
            BODY_RUN_TERMINAL,
        ).fetchall()
    return [_body_run_row(r) for r in rows]


def body_runs_recent(limit: int = 5) -> list[dict]:
    """直近の run（list 表示用。進行中も含む新しい順）"""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {_BODY_RUN_COLS} FROM body_runs"
            " ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_body_run_row(r) for r in rows]


def body_run_active_by_label(label: str, backend: str | None = None
                             ) -> dict | None:
    """同じ作業ラベルで進行中の run（重複依頼の steer 判定用）。
    backend 指定時はその backend の run だけを見る"""
    q = (f"SELECT {_BODY_RUN_COLS} FROM body_runs"
         " WHERE label=? AND status IN ('running','waiting_for_approval')")
    args: tuple = (label,)
    if backend:
        q += " AND backend=?"
        args = (label, backend)
    q += " ORDER BY created_at DESC LIMIT 1"
    with get_db() as conn:
        row = conn.execute(q, args).fetchone()
    return _body_run_row(row) if row else None


def body_run_last_session(label: str, backend: str) -> str:
    """同じラベル+backend の直近 run の身体側セッションID。
    codex の thread 継続（label→thread_id の写像）に使う。無ければ "" """
    with get_db() as conn:
        row = conn.execute(
            "SELECT body_session FROM body_runs"
            " WHERE label=? AND backend=? AND body_session IS NOT NULL"
            " AND body_session<>'' ORDER BY created_at DESC LIMIT 1",
            (label, backend),
        ).fetchone()
    return str(row[0]) if row else ""


# ---------------------------------------------------------------------------
# turn_tool_calls: ツール呼出しの永続化。
# origin='core' は会話 turn 内の呼出し（turn_id で引く）、
# origin='body_run' は身体側 run の tool イベント（ref=run_id, turn_id=NULL）
# ---------------------------------------------------------------------------

def record_tool_calls(turn_id: int, session_key, calls: list) -> None:
    """engine のツールループ結果を正本テーブルに書く。

    calls の要素は engine が calls_log に積む dict
    （tool/call_id/args/status/origin/result）を想定する。
    """
    if not calls:
        return
    now = _now_str()
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO turn_tool_calls"
            " (turn_id, session_key, origin, ref, seq, tool, args_json,"
            "  status, result_excerpt, finished_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    turn_id, str(session_key), c.get("origin") or "core",
                    c.get("call_id"), i, c.get("tool") or "?",
                    json.dumps(c.get("args") or {}, ensure_ascii=False)[:1000],
                    c.get("status"), (c.get("result") or "")[:500], now,
                )
                for i, c in enumerate(calls)
            ],
        )


def body_tool_started(run_id: str, tool: str, seq: int | None = None,
                      preview: str = "", session_key: str | None = None) -> None:
    """身体側 run の tool.started を記録（status='started' の行を足す）"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO turn_tool_calls"
            " (session_key, origin, ref, seq, tool, args_json, status)"
            " VALUES (?,'body_run',?,?,?,?,'started')",
            (session_key, run_id, seq, tool,
             json.dumps({"preview": preview}, ensure_ascii=False)[:1000]),
        )


def body_tool_completed(run_id: str, tool: str, ok: bool,
                        excerpt: str = "",
                        session_key: str | None = None) -> None:
    """同じ run・tool の最新 'started' 行を完了にする。
    対象が無ければ（再接続で started を見逃した等）完了行を insert する"""
    now = _now_str()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM turn_tool_calls"
            " WHERE origin='body_run' AND ref=? AND tool=?"
            " AND status='started' ORDER BY id DESC LIMIT 1",
            (run_id, tool),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE turn_tool_calls SET status=?, result_excerpt=?,"
                " finished_at=? WHERE id=?",
                ("ok" if ok else "error", excerpt[:500], now, row[0]),
            )
        else:
            conn.execute(
                "INSERT INTO turn_tool_calls"
                " (session_key, origin, ref, tool, status, result_excerpt,"
                "  finished_at)"
                " VALUES (?,'body_run',?,?,?,?,?)",
                (session_key, run_id, tool,
                 "ok" if ok else "error", excerpt[:500], now),
            )


def record_body_approval_event(*, session_key, event: str, pattern_key: str,
                               ref: str = "", status: str,
                               args: dict | None = None,
                               excerpt: str = "") -> bool:
    """橋経由の Hermes 承認 hook イベントを監査写しとして記録する
    （runtime-hermes-tooling-impl.md R5a）。

    正本は Hermes 側の会話内承認。ここは turn_tool_calls(origin=
    'body_approval') の写しで、Core 側 approvals テーブル（body_run 決裁）
    とは別物として扱う。ref は companion が emit した tool_call_id で、
    origin='body_bridge' の trace 行と join できる。

    同一 (session_key, origin, ref, tool, status, result_excerpt) の直近
    重複は INSERT しない（hook 送信の再送・二重登録対策）。記録したら
    True、重複とみなしたら False。
    """
    now = _now_str()
    args_json = json.dumps(args or {}, ensure_ascii=False)[:1000]
    excerpt = (excerpt or "")[:500]
    with get_db() as conn:
        dup = conn.execute(
            "SELECT id FROM turn_tool_calls"
            " WHERE origin='body_approval' AND session_key=?"
            " AND ref=? AND tool=? AND status=? AND result_excerpt=?"
            " AND created_at >= datetime('now','localtime','-10 minutes')"
            " LIMIT 1",
            (str(session_key), ref, pattern_key, status, excerpt),
        ).fetchone()
        if dup is not None:
            return False
        conn.execute(
            "INSERT INTO turn_tool_calls"
            " (session_key, origin, ref, tool, args_json, status,"
            "  result_excerpt, finished_at)"
            " VALUES (?,'body_approval',?,?,?,?,?,?)",
            (str(session_key), ref, pattern_key, args_json,
             status, excerpt, now),
        )
        return True
