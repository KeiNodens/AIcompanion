"""身体（Hermes）側 run の記録・監視・承認・報告（runtime-hermes-tooling.md §4、B段）。

body_task ツールからの依頼は body_runs テーブルを正本として記録し、
run のイベント流（runtime.body.watch_run）を run ごとの watcher タスクで
追う。終了した run は「報告ターン」として engine.generate に通し、彼女の
口調でマスターへ通知する。

身体側が承認を求めてきたときは approvals テーブルに kind='body_run' の
決裁レコードを作り、4桁コード付きでマスターに通知する。決裁は Core 側が
正本（decide()）で、結果を Hermes の /v1/runs/{id}/approval に転送する。
承認コードは通知文にだけ載せ、ツール応答・会話文脈・ログには出さない。

循環 import 回避: engine は _report() 内で遅延 import する
（tools/body → bodyruns → engine → tools の循環を断つため）。
"""
import asyncio
import hashlib
import logging
import time
from datetime import datetime, timedelta

from . import db
from .config import BODY_APPROVAL_MIN, BODY_TASK_GRACE, BODY_TASK_POLL
from .runtime import body

log = logging.getLogger("companion")

_ACTIVE = db.BODY_RUN_ACTIVE
_TERMINAL = db.BODY_RUN_TERMINAL

_STATUS_LABEL = {
    "completed": "完了",
    "failed": "失敗",
    "cancelled": "キャンセル",
    "interrupted": "中断",
    "lost": "見失い",
    "waiting_for_approval": "承認待ち",
    "running": "実行中",
}

# run_id → watcher タスク（行ごとに1本）
_watchers: dict[str, asyncio.Task] = {}


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 依頼の受付（body_task ツールから呼ばれる同期経路）
# ---------------------------------------------------------------------------

def _kick_watcher(run_id: str) -> None:
    """メインループが生きていれば watcher をすぐ起動する。
    無ければ次の tick() が拾う"""
    try:
        from . import runtime
        loop = runtime.main_loop()
    except Exception:
        loop = None
    if loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(ensure_watcher(run_id), loop)
    except RuntimeError:
        # ループが閉じている等。tick が拾うので握る
        pass


def _fmt_terminal(run_id: str, status: str, output: str, error: str) -> str:
    """猶予内に終了した run の結果文（旧 run_task の _fmt_run 相当）"""
    label = _STATUS_LABEL.get(status, status)
    if status == "completed":
        text = (output or "").strip() or "(出力なし)"
        if len(text) > 1500:
            text = text[:1500] + "…"
        return f"身体からの報告（{label}）:\n{text}"
    tail = (error or output or "").strip()
    msg = f"身体での実行が{label}になった（run_id: {run_id}）"
    if tail:
        msg += f"\n{tail[:800]}"
    return msg


def start(task: str, label: str | None, origin_session,
          backend: str = "hermes") -> str:
    """身体側の作業を依頼する。

    - 同じラベル+backend の進行中 run があれば新規にせず追加指示
      （steer）を送る
    - 新規 run は BODY_TASK_GRACE 秒だけ早期完了・承認待ちを確認する。
      猶予内に終われば結果文を返し（report_status='inline'）、終わら
      なければ run_id を返して watcher に引き渡す
    """
    task = (task or "").strip()
    if not task:
        return "依頼内容（task）を入れてね"
    label = (label or "").strip() or None
    backend = (backend or "hermes").strip().lower()

    if label:
        row = db.body_run_active_by_label(label, backend=backend)
        if row is not None:
            if row["status"] == "waiting_for_approval":
                # 同じ身体セッションに2本走らせない。決裁が先
                return (
                    f"その作業（{row['run_id']}）は承認待ちで止まってる。"
                    "先に承認か却下をしてね"
                )
            try:
                steered = body.steer(
                    row["run_id"], task, backend=row.get("backend"))
            except body.hermes.BodyAPIError:
                steered = False
            if steered:
                return (
                    f"進行中の作業「{label}」（{row['run_id']}）"
                    "に追加指示を送ったよ"
                )
            # 409 等で受け付けられない → 新規 run に切り替える

    # codex は label→thread_id の写像を前回 run の body_session から引く
    prior_session = (
        db.body_run_last_session(label, backend)
        if label and backend == "codex" else None
    )
    created = body.start_task(
        task, session=label, backend=backend, body_session=prior_session)
    run_id = created["run_id"]
    db.body_run_insert(
        run_id, task, label=label,
        body_session=created.get("body_session") or "",
        origin_session=str(origin_session or ""),
        backend=backend,
    )

    deadline = time.monotonic() + BODY_TASK_GRACE
    while True:
        try:
            st = body.run_check(run_id, backend=backend)
        except body.hermes.BodyAPIError:
            # 作成直後の見え方遅延等。猶予内は確認を続ける
            if time.monotonic() < deadline:
                time.sleep(BODY_TASK_POLL)
                continue
            st = None
        status = (st or {}).get("status") or ""
        if status in _TERMINAL:
            db.body_run_finish(
                run_id, status=status, output=st.get("output") or "",
                error=st.get("error") or "", inline=True,
            )
            return _fmt_terminal(
                run_id, status, st.get("output") or "", st.get("error") or ""
            )
        if status == "waiting_for_approval":
            ap = st.get("approval") or {}
            register_approval(
                run_id, ap.get("request_id"),
                ap.get("summary") or "(内容不明)",
            )
            db.body_run_update(run_id, status="waiting_for_approval")
            _kick_watcher(run_id)
            return (
                f"承認待ちで止まってる（{run_id}）。マスターに承認コードを"
                "送ったから、承認されたら続きをやるね"
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(BODY_TASK_POLL)
    _kick_watcher(run_id)
    return f"始めたよ（{run_id}）。終わったら報告するね"


# ---------------------------------------------------------------------------
# watcher: run ごとのイベント追跡（scheduler の tick が起動・再接続を面倒見る）
# ---------------------------------------------------------------------------

async def ensure_watcher(run_id: str) -> None:
    """run の watcher タスクを起動する（多重起動・終了済み run はしない）"""
    t = _watchers.get(run_id)
    if t is not None and not t.done():
        return
    row = db.body_run_get(run_id)
    if not row or row["status"] not in _ACTIVE:
        return
    _watchers[run_id] = asyncio.create_task(
        _watch(run_id), name=f"body-run:{run_id}"
    )


def _handle_event(run_id: str, ev: dict) -> bool:
    """正規化イベント1件を記録する。終了イベントなら True"""
    kind = ev.get("kind")
    if kind == "tool_started":
        row = db.body_run_get(run_id) or {}
        db.body_tool_started(
            run_id, ev.get("tool") or "?",
            seq=ev.get("seq"), preview=str(ev.get("preview") or ""),
            session_key=row.get("origin_session"),
        )
    elif kind == "tool_completed":
        duration = ev.get("duration")
        excerpt = (
            f"{duration:.1f}s" if isinstance(duration, (int, float))
            else str(duration or "")
        )
        row = db.body_run_get(run_id) or {}
        db.body_tool_completed(
            run_id, ev.get("tool") or "?", ok=not ev.get("error"),
            excerpt=excerpt, session_key=row.get("origin_session"),
        )
    elif kind == "approval_request":
        register_approval(
            run_id, ev.get("request_id"), ev.get("summary") or "(内容不明)"
        )
        db.body_run_update(run_id, status="waiting_for_approval")
    elif kind == "approval_responded":
        db.body_run_update(run_id, status="running")
    elif kind == "finished":
        db.body_run_finish(
            run_id, status=ev.get("status") or "failed",
            output=ev.get("output") or "", error=ev.get("error") or "",
        )
        return True
    return False


def _mark_lost(run_id: str, backend: str = "hermes") -> None:
    db.body_run_finish(
        run_id, status="lost",
        error=f"{backend or 'body'} 側に run が見つからない（再起動等）",
    )


async def _watch(run_id: str) -> None:
    """run のイベント流を追う。stream が切れたら run_check で現在値を
    1回確認し、未終了ならバックオフで再接続する"""
    try:
        row = db.body_run_get(run_id)
        if not row:
            return
        backend = row.get("backend") or "hermes"
        last_seq = row.get("last_seq")
        if not isinstance(last_seq, int):
            last_seq = -1
        delay = 5.0
        while True:
            finished = False
            try:
                async for ev in body.watch_run(
                        run_id, last_seq=last_seq, backend=backend):
                    seq = ev.get("seq")
                    if isinstance(seq, int) and seq > last_seq:
                        last_seq = seq
                        db.body_run_set_seq(run_id, seq)
                    if _handle_event(run_id, ev):
                        finished = True
            except asyncio.CancelledError:
                raise
            except body.hermes.BodyAPIError as e:
                log.warning("body run %s events api error: %s", run_id, e)
            except body.hermes.BodyUnreachableError as e:
                log.warning("body run %s events unreachable: %s", run_id, e)
            except Exception:
                log.exception("body run %s watcher failed", run_id)
            if finished:
                return
            # stream が終了イベント無しで切れた → 現在値を1回確認して
            # 終了なら確定、404 なら lost、未終了/不明なら再接続する
            try:
                st = body.run_check(run_id, backend=backend)
            except body.hermes.BodyAPIError as e:
                if e.status_code == 404:
                    _mark_lost(run_id, backend)
                    return
                st = None
            except body.hermes.BodyUnreachableError:
                st = None
            except Exception:
                log.exception("body run %s status check failed", run_id)
                st = None
            if st is not None:
                status = st.get("status") or ""
                if status in _TERMINAL:
                    db.body_run_finish(
                        run_id, status=status, output=st.get("output") or "",
                        error=st.get("error") or "",
                    )
                    return
                if status == "waiting_for_approval":
                    ap = st.get("approval") or {}
                    register_approval(
                        run_id, ap.get("request_id"),
                        ap.get("summary") or "(内容不明)",
                    )
                    db.body_run_update(run_id, status="waiting_for_approval")
                elif status:
                    db.body_run_update(run_id, status=status)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
    finally:
        # 再起動された別タスクの登録を消さないよう自分のときだけ抜く
        if _watchers.get(run_id) is asyncio.current_task():
            _watchers.pop(run_id, None)


# ---------------------------------------------------------------------------
# tick: scheduler から定期呼出（watcher 復旧・報告・期限切れ承認の掃き替え）
# ---------------------------------------------------------------------------

async def tick() -> None:
    """(1) 進行中 run で watcher の無いものを起動（再起動後の再接続含む）
    (2) 終了済みで未報告の run を報告 (3) 期限切れ承認を掃き替える"""
    for row in db.body_runs_active():
        await ensure_watcher(row["run_id"])
    for row in db.body_runs_pending_report():
        await _report(row)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE approvals SET status='expired'"
            " WHERE kind='body_run' AND status='pending'"
            " AND expires_at IS NOT NULL AND expires_at<?",
            (_now_str(),),
        )


async def _report(row: dict) -> None:
    """終了した run を「身体からの報告」として generate に通して通知する。
    通知先が解決できなければ pending のまま次の tick に残す"""
    from . import engine  # 循環回避のため遅延 import

    run_id = row["run_id"]
    cid = db.resolve_notify_channel()
    if not cid:
        return
    label = row.get("label") or (row.get("task") or "")[:40]
    status_label = _STATUS_LABEL.get(row.get("status"), row.get("status"))
    text = (
        f"[身体からの報告] {label}: {status_label}\n"
        f"{(row.get('output') or '')[:1500] or (row.get('error') or '')}"
    )
    try:
        res = await engine.generate(
            row.get("origin_session") or "desktop", text,
            actor="desktop", source="body",
        )
        reply = res.get("reply") or ""
    except Exception:
        log.exception("body run report generate failed: %s", run_id)
        db.body_run_report_status(run_id, "failed")
        db.enqueue_notification(
            f"body_run:{run_id}", cid,
            f"身体の作業「{label}」が{status_label}になった"
            "（報告文の生成に失敗）",
        )
        return
    db.enqueue_notification(f"body_run:{run_id}", cid, reply)
    db.body_run_report_status(run_id, "done")


# ---------------------------------------------------------------------------
# 承認: 身体側の承認要求を Core が決裁し、Hermes に転送する
# ---------------------------------------------------------------------------

def _new_code(conn) -> str:
    """pending 中で重複しない4桁コードを引く（kind 横断の共用採番）"""
    return db.new_approval_code(conn)


def register_approval(run_id: str, request_id, summary: str) -> dict | None:
    """身体側の承認要求を approvals に登録し、コード付きで通知する。

    ref = "{run_id}:{request_id}" で同じ要求の再登録は冪等にする。
    通知文にだけコードを載せる（ツール応答・会話・ログには出さない）。
    request_id の無い要求は ref が "run_id:" に潰れるため、dedup は
    pending で同一 summary の行に限る（決裁済み/別内容の要求は別行にする）。
    """
    ref = f"{run_id}:{request_id or ''}"
    row = db.body_run_get(run_id) or {}
    label = row.get("label") or ""
    task = row.get("task") or ""
    summary = (summary or "").strip() or "(内容不明)"
    content_hash = hashlib.sha256((ref + summary).encode()).hexdigest()[:16]
    expires = (datetime.now() + timedelta(minutes=BODY_APPROVAL_MIN)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with db.get_db() as conn:
        if request_id:
            existing = conn.execute(
                "SELECT id, status FROM approvals"
                " WHERE kind='body_run' AND ref=?",
                (ref,),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT id, status FROM approvals"
                " WHERE kind='body_run' AND ref=? AND summary=?"
                " AND status='pending'",
                (ref, summary),
            ).fetchone()
        if existing:
            return {"id": existing[0], "status": existing[1]}
        code = _new_code(conn)
        cur = conn.execute(
            "INSERT INTO approvals"
            " (job_id, content_hash, requester, status, expires_at,"
            "  kind, ref, code, summary)"
            " VALUES (0,?,?,'pending',?,'body_run',?,?,?)",
            (content_hash, "body", expires, ref, code, summary),
        )
        ap_id = cur.lastrowid
    cid = db.resolve_notify_channel()
    if cid:
        db.enqueue_notification(
            f"body_approval:{ap_id}", cid,
            f"🔐 身体が承認を求めてる（{label or task[:30]}）: {summary[:300]}\n"
            f"承認するなら「承認 {code}」、やめるなら「却下 {code}」と送って"
            f"（{BODY_APPROVAL_MIN}分有効）",
        )
    else:
        log.warning("body approval #%s: 通知先が解決できない", ap_id)
    return {"id": ap_id, "status": "pending", "ref": ref}


def find_pending_by_code(code: str) -> dict | None:
    """マスターの「承認 1234」を pending 承認に引く。code は返さない"""
    code = (code or "").strip()
    if not code:
        return None
    now = _now_str()
    with db.get_db() as conn:
        conn.execute(
            "UPDATE approvals SET status='expired'"
            " WHERE kind='body_run' AND status='pending'"
            " AND expires_at IS NOT NULL AND expires_at<?",
            (now,),
        )
        row = conn.execute(
            "SELECT id, ref, summary, expires_at FROM approvals"
            " WHERE kind='body_run' AND code=? AND status='pending'",
            (code,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "ref": row[1], "request": (row[2] or "")[:120],
        "expires_at": row[3],
    }


def pending_approvals() -> list[dict]:
    """desktop/API が提示する身体承認の待ち一覧。code は絶対に含めない"""
    now = _now_str()
    with db.get_db() as conn:
        conn.execute(
            "UPDATE approvals SET status='expired'"
            " WHERE kind='body_run' AND status='pending'"
            " AND expires_at IS NOT NULL AND expires_at<?",
            (now,),
        )
        rows = conn.execute(
            "SELECT id, summary, expires_at FROM approvals"
            " WHERE kind='body_run' AND status='pending'"
            " AND (expires_at IS NULL OR expires_at>=?) ORDER BY id",
            (now,),
        ).fetchall()
    return [
        {"id": r[0], "kind": "body_run", "request": (r[1] or "")[:120],
         "expires_at": r[2]}
        for r in rows
    ]


def decide(*, code: str | None = None, approval_id: int | None = None,
           approve: bool, decided_by: str) -> tuple[bool, str]:
    """マスターの決裁を Core に記録し、身体（Hermes）に転送する。

    条件付き UPDATE で二重決裁を防ぎ、期限切れは expired、身体側が
    もう承認待ちでない（409）は stale に掃き替える。到達不能のときは
    pending に戻さない（誤承認を防ぐ安全側）。
    戻り値は (成功?, 彼女向け文言)。文言にコードは含めない。
    """
    now = _now_str()
    with db.get_db() as conn:
        if approval_id is not None:
            row = conn.execute(
                "SELECT id, ref, expires_at, status FROM approvals"
                " WHERE kind='body_run' AND id=?",
                (approval_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, ref, expires_at, status FROM approvals"
                " WHERE kind='body_run' AND code=? AND status='pending'",
                ((code or "").strip(),),
            ).fetchone()
        if not row:
            return False, "その承認は見つからないよ（もう処理済みか期限切れ）"
        ap_id, ref, expires_at, status = row
        if status != "pending":
            return False, "その承認はもう処理済みだよ"
        if expires_at and expires_at < now:
            conn.execute(
                "UPDATE approvals SET status='expired' WHERE id=?", (ap_id,)
            )
            return False, "その承認は期限切れだよ（身体には届いていない）"
        new_status = "approved" if approve else "denied"
        cur = conn.execute(
            "UPDATE approvals SET status=?, decided_by=?, decided_at=?"
            " WHERE id=? AND status='pending'",
            (new_status, decided_by, now, ap_id),
        )
        if cur.rowcount != 1:
            return False, "その承認はもう処理済みだよ"

    run_id, _, request_id = (ref or "").partition(":")
    run_row = db.body_run_get(run_id) or {}
    backend = run_row.get("backend") or "hermes"
    try:
        res = body.decide_approval(
            run_id, approve, request_id=request_id or None,
            backend=backend)
    except body.hermes.BodyUnreachableError:
        return (
            False,
            "決裁は記録したけど身体に届かなかった（身体に繋がらない）。"
            "身体が戻っても勝手には進まないから確認してね",
        )
    except Exception:
        log.exception("body approval decide failed for run %s", run_id)
        return False, "身体への決裁が通らなかった（内部エラー）。承認は記録済み"
    if res.get("stale"):
        with db.get_db() as conn:
            conn.execute(
                "UPDATE approvals SET status='stale' WHERE id=?", (ap_id,)
            )
        return False, "身体側の承認待ちはもう解消されてる（期限切れか別経路で応答済み）"
    if not res.get("ok"):
        return False, f"身体への決裁が通らなかった（{res.get('error') or '原因不明'}）。承認は記録済み"
    if approve:
        return True, "承認を身体に送ったよ。続きが動き出すはず"
    return True, "却下を身体に送ったよ。その操作はやめてくれるはず"
