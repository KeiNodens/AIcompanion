"""codex exec へのコーディング依頼委譲（owner限定）。

prepare → pending → confirm → running → done/failed/timeout/cancelled
pending は再起動を跨いで confirm 可能。running のまま bot が落ちると
起動後の最初の check_finished() で interrupted に掃き替える。
"""
import hashlib
import logging
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from ..config import (
    DELEGATE_APPROVAL_MIN,
    DELEGATE_CODEX_CMD,
    DELEGATE_CODEX_EFFORT,
    DELEGATE_CODEX_MODEL,
    DELEGATE_DIR,
    DELEGATE_TIMEOUT_MIN,
    EFFORT_CHOICES,
    ROOT,
)
from ..db import (
    enqueue_notification,
    get_db,
    new_approval_code,
    resolve_notify_channel,
)
from .actor import CURRENT_ACTOR, is_owner

log = logging.getLogger("companion")

_DENY = "そのお願いはマスターにしか聞けないの"

# running ジョブの Popen を保持（job_id -> Popen）。bot再起動で消失する
_procs: dict[int, subprocess.Popen] = {}
_swept = False

_PROMPT_HEAD = """あなたはコーディングエージェントとして作業を依頼されました。

## 依頼
"""

_PROMPT_RULES = """
## 守ること
- .env・認証情報・秘密鍵などの機密ファイルは読まない・変更しない
- git commit / push は依頼に明確に含まれる場合のみ行う
- セキュリティ・認証・権限まわりの設定を緩めない
- 既存のコード規約に従う（AGENTS.md があれば先に読む）
- 最終メッセージに「変更したファイルとやったことの要約」を簡潔に書く
"""

_PROMPT_REPO_EXTRA = (
    "\n- この作業対象はcompanion本体。ツール追加は companion/tools/ に関数を作り"
    " companion/tools/__init__.py の TOOLS/HANDLERS に登録する規約に従うこと"
    "\n- companion.db・core.db・state.json 等 data/ 配下の実行時データは"
    " 直接編集しない（db.* 等の低レイヤー関数呼出しによる変更も同様）。"
    " 状態変更は companion の管理経路（tools 関数・skills-inbox 投函・"
    " 管理API）を使うこと"
    "\n- 承認・決裁を要する遷移（スキル有効化・依頼の承認・レジストリの"
    " 状態遷移）は実行しない。成果物は staged 等の未決状態に留めて、"
    " 完了報告で人の承認を求めること\n"
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_job(job_id: int | None = None, status: str | None = None):
    with get_db() as conn:
        if job_id:
            return conn.execute(
                "SELECT * FROM delegate_jobs WHERE id=?", (job_id,)
            ).fetchone()
        q = "SELECT * FROM delegate_jobs"
        args = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        return conn.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()


_FINAL_STATUSES = {"done", "failed", "timeout", "cancelled", "interrupted"}


def _set_status(job_id: int, status: str, **extra) -> None:
    # 終了状態への遷移と同じトランザクションで「通知待ち」にする。
    # こうしないと完了確定後に通知だけ落ちて二度と拾えない
    cols = {"status": status, **extra}
    if status in _FINAL_STATUSES:
        cols.setdefault("notify_status", "pending")
    sql = "UPDATE delegate_jobs SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?"
    with get_db() as conn:
        conn.execute(sql, (*cols.values(), job_id))


def mark_notify_queued(job_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE delegate_jobs SET notify_status='queued' WHERE id=?",
            (job_id,),
        )


def jobs_needing_notify():
    """完了済みだが通知がまだ送信待ちに載っていないジョブ（再起動後も拾える）"""
    with get_db() as conn:
        return conn.execute(
            "SELECT id FROM delegate_jobs WHERE notify_status='pending'"
            " ORDER BY id"
        ).fetchall()


def _fmt_request(r) -> str:
    return (r[2] or "")[:60]


def _job_hash(prompt: str, workdir: str, model, effort) -> str:
    """承認対象の内容指紋。承認後に依頼内容が変わっていたら無効にする"""
    h = hashlib.sha256()
    for part in (prompt, workdir, model, effort):
        h.update(str(part or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _new_approval(job_id: int, content_hash: str, requester: str | None,
                  request: str = "") -> bool:
    """承認レコードを作り、4桁コードを通知に載せて送る。

    通知文にだけコードを載せる（ツール応答・会話文脈・ログには出さない）。
    通知を積めたら True（通知先が解決できなければ False）"""
    expires = (datetime.now() + timedelta(minutes=DELEGATE_APPROVAL_MIN)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with get_db() as conn:
        code = new_approval_code(conn)
        cur = conn.execute(
            "INSERT INTO approvals"
            " (job_id, content_hash, requester, expires_at, code)"
            " VALUES (?,?,?,?,?)",
            (job_id, content_hash, requester, expires, code),
        )
        ap_id = cur.lastrowid
    cid = resolve_notify_channel()
    if not cid:
        log.warning("delegate approval #%s: 通知先が解決できない", ap_id)
        return False
    enqueue_notification(
        f"delegate_approval:{ap_id}", cid,
        f"🔐 コーディング依頼 #{job_id} の実行承認を求めてる:"
        f" {(request or '')[:300]}\n"
        f"承認するなら「承認 {code}」、やめるなら「却下 {code}」と送って"
        f"（{DELEGATE_APPROVAL_MIN}分有効）",
    )
    return True


def _approval_guidance(notified: bool) -> str:
    """prepare/confirm が返す、人向けの承認手順文（コード自体は含めない）"""
    if notified:
        return ("マスターに承認コードを通知しました。"
                "マスターが「承認 NNNN」と送るか、デスクトップの承認ボタンを"
                "押すと実行します")
    return ("通知先が解決できませんでした。"
            "デスクトップの承認ボタンを押してください")


def find_pending_by_code(code: str) -> dict | None:
    """マスターの「承認 1234」を pending の委譲承認に引く。code は返さない"""
    code = (code or "").strip()
    if not code:
        return None
    now = _now()
    with get_db() as conn:
        conn.execute(
            "UPDATE approvals SET status='expired'"
            " WHERE kind='delegate' AND status='pending'"
            " AND expires_at IS NOT NULL AND expires_at<?",
            (now,),
        )
        row = conn.execute(
            "SELECT a.job_id, j.request FROM approvals a"
            " JOIN delegate_jobs j ON j.id=a.job_id"
            " WHERE a.kind='delegate' AND a.code=? AND a.status='pending'"
            " AND j.status='pending' ORDER BY a.id DESC LIMIT 1",
            (code,),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "request": (row[1] or "")[:120]}


def _latest_approval(job_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT id, content_hash, status, expires_at FROM approvals"
            " WHERE job_id=? AND kind='delegate' ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()


def pending_approvals() -> list[dict]:
    """desktop/API が提示する承認待ち一覧。期限切れは掃き替えてから返す"""
    now = _now()
    with get_db() as conn:
        conn.execute(
            "UPDATE approvals SET status='expired'"
            " WHERE status='pending' AND kind='delegate'"
            " AND expires_at IS NOT NULL AND expires_at<?",
            (now,),
        )
        rows = conn.execute(
            "SELECT a.job_id, j.request, a.expires_at FROM approvals a"
            " JOIN delegate_jobs j ON j.id=a.job_id"
            " WHERE a.kind='delegate' AND a.status='pending'"
            " AND j.status='pending' ORDER BY a.job_id"
        ).fetchall()
    return [
        {"id": r[0], "request": (r[1] or "")[:120], "expires_at": r[2]}
        for r in rows
    ]


def delegate_prepare(request: str, workdir: str | None = None,
                     model: str | None = None, effort: str | None = None) -> str:
    """依頼文を組み立てて pending 登録。実行は confirm で"""
    if not is_owner():
        return _DENY
    request = (request or "").strip()
    if not request:
        return "依頼内容が空です"
    wd = Path(workdir).expanduser() if workdir else ROOT
    wd = wd.resolve()
    if not wd.is_dir():
        return f"作業ディレクトリが見つかりません: {workdir}"
    eff = (effort or DELEGATE_CODEX_EFFORT or "").strip() or None
    if eff and eff not in EFFORT_CHOICES:
        return f"effort は {'/'.join(EFFORT_CHOICES)} のいずれかです"
    mdl = (model or DELEGATE_CODEX_MODEL or "").strip() or None

    prompt = _PROMPT_HEAD + request + _PROMPT_RULES
    if wd == ROOT:
        prompt += _PROMPT_REPO_EXTRA

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO delegate_jobs (prompt, request, workdir, model, effort, status)"
            " VALUES (?,?,?,?,?, 'pending')",
            (prompt, request, str(wd), mdl, eff),
        )
        job_id = cur.lastrowid
    notified = _new_approval(
        job_id, _job_hash(prompt, str(wd), mdl, eff), CURRENT_ACTOR.get(),
        request,
    )

    return (
        f"依頼 #{job_id} を用意しました（まだ実行していません）。\n"
        f"作業dir: {wd}\n"
        f"codex model: {mdl or '(既定)'} / effort: {eff or '(既定)'}\n"
        f"---\n{prompt}---\n"
        f"実行にはマスター本人の承認が必要です。"
        f"{_approval_guidance(notified)}（{DELEGATE_APPROVAL_MIN}分有効）。"
    )


def _codex_exe() -> str | None:
    cand = DELEGATE_CODEX_CMD or "codex"
    return shutil.which(cand) or (cand if Path(cand).exists() else None)


def _launch(job) -> str:
    """starting 状態のジョブを codex exec で起動する。
    起動失敗は failed に掃き替えて通知経路に乗せる。"""
    jid, prompt, _req, wd, mdl, eff = job[0], job[1], job[2], job[3], job[4], job[5]

    exe = _codex_exe()
    if not exe:
        _set_status(jid, "failed", finished_at=_now())
        return "codex コマンドが見つかりません（DELEGATE_CODEX_CMD を確認）"

    log_path = DELEGATE_DIR / f"job-{jid}.log"
    final_path = DELEGATE_DIR / f"job-{jid}.final.txt"
    prompt_path = DELEGATE_DIR / f"job-{jid}.prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    cmd = [
        exe, "exec", "-",
        "-C", wd,
        "-s", "workspace-write",
        "--skip-git-repo-check",
        "--json",
        "-o", str(final_path),
    ]
    if mdl:
        cmd += ["-m", mdl]
    if eff:
        cmd += ["-c", f'model_reasoning_effort="{eff}"']

    flags = 0
    for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    try:
        logf = open(log_path, "ab")
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT,
            cwd=wd, creationflags=flags,
        )
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        proc._logfile = logf  # 終了時に閉じるため保持
    except Exception as e:
        log.exception("codex spawn failed")
        _set_status(jid, "failed", finished_at=_now())
        return f"codex の起動に失敗しました: {e}"

    _procs[jid] = proc
    _set_status(jid, "running", started_at=_now())
    log.info("delegate job %d started (pid=%s, dir=%s)", jid, proc.pid, wd)
    return f"依頼 #{jid} をcodexに出しました。終わったら報告しますね"


def delegate_approve(job_id: int, approver: str) -> str:
    """本人承認を消費してジョブを起動する。LLMのツールではなく
    「承認 NNNN」発言・承認ボタン・API などの明示経路から呼ぶ。"""
    job = _get_job(job_id)
    if not job:
        return "依頼が見つかりません"
    if job[6] != "pending":
        return f"依頼 #{job[0]} は {job[6]} 状態です（pending のみ実行できます）"
    running = _get_job(status="running")
    if running:
        return f"依頼 #{running[0]} が実行中です。終わってからにしてください"

    content_hash = _job_hash(job[1], job[3], job[4], job[5])
    with get_db() as conn:
        ap = conn.execute(
            "SELECT id, content_hash, status, expires_at FROM approvals"
            " WHERE job_id=? AND kind='delegate' ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if not ap:
            # 移行前の pending ジョブには承認レコードが無いので補完する
            # （この場で消費するので code・通知は不要）
            _expire = (datetime.now() + timedelta(minutes=DELEGATE_APPROVAL_MIN)
                       ).strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                "INSERT INTO approvals (job_id, content_hash, requester, expires_at)"
                " VALUES (?,?,?,?)",
                (job_id, content_hash, approver, _expire),
            )
            ap_id = cur.lastrowid
        elif ap[2] != "pending":
            return f"依頼 #{job_id} の承認はすでに {ap[2]} です"
        elif ap[3] and ap[3] < _now():
            conn.execute(
                "UPDATE approvals SET status='expired' WHERE id=?", (ap[0],)
            )
            return f"依頼 #{job_id} の承認期限が切れています。もう一度 prepare してください"
        else:
            if ap[1] != content_hash:
                return "依頼内容が承認時と変わっています。もう一度 prepare してください"
            ap_id = ap[0]
        # 承認の消費と starting 遷移を同じトランザクションで行う。
        # 条件付きUPDATEで二重承認・二重起動を防ぐ
        cur = conn.execute(
            "UPDATE approvals SET status='consumed', decided_by=?, decided_at=?"
            " WHERE id=? AND status='pending'",
            (approver, _now(), ap_id),
        )
        if cur.rowcount != 1:
            return f"依頼 #{job_id} の承認はすでに処理されました"
        cur = conn.execute(
            "UPDATE delegate_jobs SET status='starting'"
            " WHERE id=? AND status='pending'",
            (job_id,),
        )
        if cur.rowcount != 1:
            return f"依頼 #{job_id} の状態が変わりました。delegate_status で確認してください"

    log.info("delegate job %d approved by %s", job_id, approver)
    return _launch(job)


def delegate_confirm(job_id: int | None = None) -> str:
    """実行前の最終確認。起動自体は「承認 NNNN」/ 承認ボタン等の
    明示経路のみが行う"""
    if not is_owner():
        return _DENY
    job = _get_job(job_id, status=None if job_id else "pending")
    if not job:
        return "承認待ちの依頼はありません"
    if job[6] == "starting":
        return f"依頼 #{job[0]} は起動処理中です"
    if job[6] != "pending":
        return f"依頼 #{job[0]} は {job[6]} 状態です（pending のみ実行できます）"
    notified = True
    ap = _latest_approval(job[0])
    if not ap:
        notified = _new_approval(
            job[0], _job_hash(job[1], job[3], job[4], job[5]),
            CURRENT_ACTOR.get(), job[2],
        )
    return (
        f"依頼 #{job[0]} の実行には本人の明示的な承認が必要です。\n"
        f"{_approval_guidance(notified)}。"
    )


def delegate_deny(job_id: int, decided_by: str = "") -> str:
    """pending の委譲依頼を却下する（実行せず取り消し）。
    「却下 NNNN」などの明示経路専用（owner 束縛は呼び出し側で済んでいる）"""
    job = _get_job(job_id)
    if not job:
        return "依頼が見つかりません"
    if job[6] != "pending":
        return f"依頼 #{job[0]} は {job[6]} 状態です"
    with get_db() as conn:
        conn.execute(
            "UPDATE approvals SET status='denied', decided_by=?, decided_at=?"
            " WHERE job_id=? AND kind='delegate' AND status='pending'",
            (decided_by or None, _now(), job_id),
        )
    _set_status(job_id, "cancelled", finished_at=_now())
    return f"依頼 #{job_id} は却下されました（実行しません）"


def delegate_status(job_id: int | None = None) -> str:
    if not is_owner():
        return _DENY
    with get_db() as conn:
        if job_id:
            rows = conn.execute(
                "SELECT * FROM delegate_jobs WHERE id=?", (job_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM delegate_jobs ORDER BY id DESC LIMIT 5"
            ).fetchall()
    if not rows:
        return "依頼はまだありません"
    lines = []
    for r in rows:
        lines.append(
            f"- #{r[0]} [{r[6]}] {_fmt_request(r)}"
            + (f"（{r[3]}）" if r[3] != str(ROOT) else "（companion本体）")
            + (f" exit={r[7]}" if r[7] is not None else "")
        )
    return "## 委譲ジョブ\n" + "\n".join(lines)


def delegate_log(job_id: int | None = None, lines: int = 40) -> str:
    if not is_owner():
        return _DENY
    job = _get_job(job_id)
    if not job:
        return "依頼が見つかりません"
    log_path = DELEGATE_DIR / f"job-{job[0]}.log"
    if not log_path.exists():
        return f"依頼 #{job[0]} のログはまだありません（{job[6]}）"
    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(tail[-max(1, min(lines, 200)):])
    return f"## 依頼 #{job[0]} ログ末尾\n```\n{body[-3000:]}\n```"


def delegate_cancel(job_id: int | None = None) -> str:
    if not is_owner():
        return _DENY
    job = _get_job(job_id)
    if not job:
        return "依頼が見つかりません"
    jid, status = job[0], job[6]
    if status == "pending":
        _set_status(jid, "cancelled", finished_at=_now())
        return f"依頼 #{jid} をキャンセルしました"
    if status == "running":
        proc = _procs.get(jid)
        if proc and proc.poll() is None:
            proc.terminate()
        _set_status(jid, "cancelled", finished_at=_now())
        return f"依頼 #{jid} を止めました"
    return f"依頼 #{jid} はすでに {status} です"


def _tail(path: Path, chars: int = 1500) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-chars:]
    except Exception:
        return ""


def job_report(job_id: int) -> dict:
    """通知本文の組み立てに使う確定済みジョブの情報（ファイル末尾は都度読み直す）"""
    job = _get_job(job_id)
    if not job:
        return {}
    return {
        "id": job[0],
        "status": job[6],
        "exit_code": job[7],
        "request": _fmt_request(job),
        "workdir": job[3],
        "final": _tail(DELEGATE_DIR / f"job-{job[0]}.final.txt", 800).strip(),
        "log_tail": _tail(DELEGATE_DIR / f"job-{job[0]}.log"),
    }


def _finalize(jid: int, proc: subprocess.Popen) -> dict:
    exit_code = proc.returncode
    job = _get_job(jid)
    cur = job[6] if job else ""
    status = cur if cur in ("timeout", "cancelled") else (
        "done" if exit_code == 0 else "failed"
    )
    _set_status(jid, status, exit_code=exit_code, finished_at=_now())
    try:
        proc._logfile.close()
    except Exception:
        pass
    return job_report(jid)


def check_finished() -> list[dict]:
    """scheduler tick から呼ぶ。終了したジョブを確定して報告用dictを返す"""
    global _swept
    finished = []

    if not _swept:
        _swept = True
        with get_db() as conn:
            stale = conn.execute(
                "SELECT id FROM delegate_jobs WHERE status IN ('running','starting')"
            ).fetchall()
        for (jid,) in stale:
            if jid not in _procs:
                _set_status(jid, "interrupted", finished_at=_now())
                log.warning("delegate job %d interrupted by restart", jid)

    for jid, proc in list(_procs.items()):
        job = _get_job(jid)
        if not job or job[6] != "running":
            proc.poll()  # reap
            _procs.pop(jid, None)
            continue
        if proc.poll() is not None:
            finished.append(_finalize(jid, proc))
            _procs.pop(jid, None)
            continue
        try:
            started = datetime.strptime(job[9], "%Y-%m-%d %H:%M:%S")
            if datetime.now() - started > timedelta(minutes=DELEGATE_TIMEOUT_MIN):
                proc.terminate()
                _set_status(jid, "timeout", finished_at=_now())
                finished.append(_finalize(jid, proc))
                _procs.pop(jid, None)
        except Exception:
            log.exception("delegate timeout check failed for job %d", jid)
    return finished
