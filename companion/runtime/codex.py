"""Codex body — `codex app-server` (JSON-RPC 2.0) クライアント
（research/design/codex-body.md）。

Core→Codex 方向の最下層。`codex app-server` を stdio subprocess として
spawn し、thread/turn の制御・server→client 承認要求・notification
購読を行う。app-server の method 名・型を知るのはこのモジュールだけ。

構成:
- 長命 subprocess を遅延 spawn（未使用なら起動しない）。死んだら次の
  呼出し・watcher 再接続で spawn し直す
- reader スレッドが stdout の JSONL を demux する:
  - 応答（id+result/error）→ pending 待ちの _request へ
  - server→client 要求（id+method）→ 承認系は保留して購読者へ通知、
    それ以外は JSON-RPC error で即答
  - notification（method）→ thread_id で購読キューへ正規化配送
- 制御系は sync（hermes.py と同じく tools/bodyruns から to_thread 経由）。
  run_events だけ async ジェネレータ

run_id は "{thread_id}/{turn_id}"（steer/interrupt/read に thread_id が
要るため複合形）。body_session = thread_id。app-server 側の thread
購読は接続ごとなので、再接続のたびに thread/resume で join し直す。
"""
import asyncio
import itertools
import json
import logging
import queue
import shutil
import subprocess
import threading
from pathlib import Path

from ..config import (
    CODEX_APP_SERVER_CMD,
    CODEX_BODY_APPROVAL_POLICY,
    CODEX_BODY_CWD,
    CODEX_BODY_ENABLED,
    CODEX_BODY_EFFORT,
    CODEX_BODY_MODEL,
    CODEX_BODY_SANDBOX,
)
from .errors import BodyAPIError, BodyUnreachableError

log = logging.getLogger("companion")

_CLIENT_INFO = {"name": "companion", "version": "1.0"}
_REQUEST_TIMEOUT = 45.0
_SPAWN_TIMEOUT = 30.0

# turn.status → body run の正規化 status
_TURN_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "interrupted": "interrupted",
    "cancelled": "interrupted",
}

# tool_started/completed に写す item 種別（会話アイテムは省く）
_TOOLISH_ITEMS = {
    "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
    "webSearch", "collabAgentToolCall",
}

# 承認系 server→client 要求 → 応答 decision の写像
# （once→accept/approved、deny→decline/denied）
_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": ("accept", "decline"),
    "item/fileChange/requestApproval": ("accept", "decline"),
    "execCommandApproval": ("approved", "denied"),
    "applyPatchApproval": ("approved", "denied"),
    # item/permissions/requestApproval は decision 形でなく granted
    # permissions を返す形なので別途処理
}

_STATE_LOCK = threading.Lock()
_SPAWN_LOCK = threading.Lock()
_proc: subprocess.Popen | None = None
_reader_t: threading.Thread | None = None
_errdrain_t: threading.Thread | None = None
_seq = itertools.count(1)
_pending: dict[int, "queue.Queue"] = {}
_ev_seq = itertools.count(1)
_subs: dict[str, list["queue.Queue"]] = {}
_joined: set[str] = set()
_appr: dict[str, dict] = {}
_item_started: dict[str, int] = {}
_init_ok = False
_init_result: dict = {}
_DEAD = object()  # プロセス死亡を購読者に伝える sentinel


def _exe() -> str | None:
    cand = CODEX_APP_SERVER_CMD or "codex"
    return shutil.which(cand) or (cand if Path(cand).exists() else None)


def _mark_dead(err: Exception | None = None) -> None:
    """プロセス死亡を確定し、pending・購読者・保留承認を掃く"""
    global _proc, _init_ok, _init_result
    with _STATE_LOCK:
        _proc = None
        _init_ok = False
        _init_result = {}
        for q in list(_pending.values()):
            q.put({"error": {"code": -32000,
                             "message": "app-server process died"}})
        _pending.clear()
        for subs in _subs.values():
            for q in subs:
                q.put(_DEAD)
        _subs.clear()
        _joined.clear()
        _appr.clear()
        _item_started.clear()


def _write(msg: dict) -> None:
    """stdin へ1行 JSON を書く。壊れていれば死亡扱いにする"""
    line = json.dumps(msg, ensure_ascii=False)
    with _STATE_LOCK:
        proc = _proc
    if proc is None or proc.poll() is not None or proc.stdin is None:
        raise BodyUnreachableError("codex app-server が起動していません")
    try:
        with _STATE_LOCK:
            proc.stdin.write((line + "\n").encode("utf-8"))
            proc.stdin.flush()
    except (BrokenPipeError, OSError) as e:
        _mark_dead()
        raise BodyUnreachableError(
            f"codex app-server への書き込みに失敗: {e}") from e


def _send_response(req_id, result=None, error=None) -> None:
    msg: dict = {"id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result if result is not None else {}
    try:
        _write(msg)
    except BodyUnreachableError:
        pass


def _reader(proc: subprocess.Popen) -> None:
    """stdout の JSONL を demux し続けるスレッド本体"""
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log.debug("codex app-server: 非JSON行: %.200s", line)
                continue
            if "method" in msg and "id" in msg:
                _on_server_request(msg)
            elif "id" in msg:
                rid = msg.get("id")
                try:
                    key = int(rid)
                except (TypeError, ValueError):
                    key = -1
                q = _pending.pop(key, None)
                if q is not None:
                    q.put(msg)
                else:
                    log.debug("codex app-server: 宛先不明の応答 id=%r", rid)
            elif "method" in msg:
                _on_notification(msg)
            else:
                log.debug("codex app-server: 解釈不能メッセージ %.200s", line)
    except Exception:
        log.exception("codex app-server reader failed")
    finally:
        _mark_dead()


def _errdrain(proc: subprocess.Popen) -> None:
    try:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                log.debug("codex app-server stderr: %s", line[:500])
    except Exception:
        pass


def _emit(thread_id: str, ev: dict) -> None:
    """正規化イベントを購読キューへ（seq はここで採番）"""
    ev = {"seq": next(_ev_seq), **ev}
    for q in list(_subs.get(thread_id, [])):
        q.put(ev)


def _item_preview(item: dict) -> str:
    return str(
        item.get("command")
        or item.get("tool")
        or item.get("reason")
        or item.get("title")
        or ""
    )[:300]


def _item_name(item: dict) -> str:
    t = item.get("type") or "item"
    for k in ("tool", "server", "name"):
        if item.get(k):
            return f"{t}:{item[k]}"
    return t


def _approval_summary(method: str, params: dict) -> str:
    if method == "item/commandExecution/requestApproval":
        acts = params.get("commandActions") or []
        act = ""
        if acts and isinstance(acts[0], dict):
            act = str(acts[0].get("description") or acts[0].get("type") or "")
        return str(params.get("command") or act or "(コマンド不明)")[:300]
    if method == "execCommandApproval":
        cmd = params.get("command") or []
        return (" ".join(str(c) for c in cmd) if isinstance(cmd, list)
                else str(cmd))[:300]
    if method == "item/fileChange/requestApproval":
        return str(params.get("reason") or "file changes")[:300]
    if method == "applyPatchApproval":
        files = list((params.get("fileChanges") or {}).keys())
        return ("patch: " + ", ".join(files[:3]) if files else "patch")[:300]
    if method == "item/permissions/requestApproval":
        return str(params.get("reason") or "permissions 昇格要求")[:300]
    return f"{method}"[:300]


def _on_server_request(msg: dict) -> None:
    """server→client 要求の振り分け。承認系は購読者に通知して保留、
    それ以外は JSON-RPC error で即答（身体側を待たせない）"""
    method = msg.get("method") or ""
    params = msg.get("params") or {}
    req_id = msg.get("id")
    thread_id = str(params.get("threadId") or params.get("conversationId") or "")
    turn_id = str(params.get("turnId") or "")
    run_id = f"{thread_id}/{turn_id}" if turn_id else thread_id

    if method == "item/permissions/requestApproval":
        # granted profile を返す形。Core 決裁に乗せるため保留
        _appr[str(req_id)] = {
            "run_id": run_id, "thread_id": thread_id,
            "method": method, "params": params,
        }
        _emit(thread_id, {
            "kind": "approval_request",
            "request_id": str(req_id),
            "summary": _approval_summary(method, params),
        })
        return
    if method in _APPROVAL_METHODS:
        _appr[str(req_id)] = {
            "run_id": run_id, "thread_id": thread_id,
            "method": method, "params": params,
        }
        _emit(thread_id, {
            "kind": "approval_request",
            "request_id": str(req_id),
            "summary": _approval_summary(method, params),
        })
        return
    # 身体面では扱わない要求: dynamic tool・elicitation・attestation・
    # auth refresh・currentTime 等は全て error 応答
    log.info("codex app-server: 未対応の server request %s を拒否", method)
    _send_response(req_id, error={
        "code": -32601,
        "message": f"companion body does not serve {method}",
    })


def _on_notification(msg: dict) -> None:
    method = msg.get("method") or ""
    params = msg.get("params") or {}
    thread_id = str(params.get("threadId") or "")
    if method == "turn/completed":
        turn = params.get("turn") or {}
        raw = turn.get("status") or "failed"
        status = _TURN_STATUS.get(raw, "failed")
        out = _turn_output(turn)
        err = ""
        te = turn.get("error") or {}
        if isinstance(te, dict):
            err = str(te.get("message") or "")
        _emit(thread_id, {
            "kind": "finished", "status": status,
            "output": out, "error": err,
            "turn_id": str(turn.get("id") or ""),
        })
    elif method == "item/started":
        item = params.get("item") or {}
        if (item.get("type") or "") in _TOOLISH_ITEMS:
            key = _item_key(params, item)
            try:
                _item_started[key] = int(params.get("startedAtMs") or 0)
            except (TypeError, ValueError):
                pass
            _emit(thread_id, {
                "kind": "tool_started",
                "tool": _item_name(item),
                "preview": _item_preview(item),
                "turn_id": str(params.get("turnId") or ""),
            })
    elif method == "item/completed":
        item = params.get("item") or {}
        if (item.get("type") or "") in _TOOLISH_ITEMS:
            dur = None
            key = _item_key(params, item)
            started = _item_started.pop(key, None)
            try:
                if started:
                    dur = max(
                        0.0,
                        (int(params.get("completedAtMs") or 0) - started)
                        / 1000.0,
                    )
            except (TypeError, ValueError):
                dur = None
            has_err = bool(item.get("error") or item.get("exitCode") not in
                           (None, 0))
            _emit(thread_id, {
                "kind": "tool_completed",
                "tool": _item_name(item),
                "duration": dur,
                "error": has_err,
                "turn_id": str(params.get("turnId") or ""),
            })
    elif method == "serverRequest/resolved":
        _emit(thread_id, {
            "kind": "approval_responded",
            "request_id": str(params.get("requestId") or ""),
        })
    elif method == "error":
        log.warning("codex turn error: %s", json.dumps(params)[:300])
    elif method in ("turn/started", "thread/started",
                    "thread/status/changed"):
        log.debug("codex %s: %s", method, json.dumps(params)[:200])
    else:
        log.debug("codex notification: %s %.160s", method,
                  json.dumps(params, ensure_ascii=False)[:160])


def _item_key(params: dict, item: dict) -> str:
    return "%s:%s:%s" % (
        params.get("threadId"), params.get("turnId"), item.get("id"))


def _turn_output(turn: dict) -> str:
    """完了 turn の最終 agentMessage テキストを拾う"""
    out = ""
    for it in turn.get("items") or []:
        if (it or {}).get("type") == "agentMessage":
            txt = str(it.get("text") or "")
            if txt:
                out = txt
    return out


def _spawn() -> subprocess.Popen:
    """app-server を spawn して initialize まで済ませる"""
    global _proc, _reader_t, _errdrain_t, _init_ok, _init_result
    if not CODEX_BODY_ENABLED:
        raise BodyUnreachableError("codex body は無効（CODEX_BODY_ENABLED=0）")
    exe = _exe()
    if not exe:
        raise BodyUnreachableError(
            "codex コマンドが見つかりません（CODEX_APP_SERVER_CMD を確認）")
    flags = 0
    for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    try:
        proc = subprocess.Popen(
            [exe, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(CODEX_BODY_CWD),
            creationflags=flags,
        )
    except Exception as e:
        raise BodyUnreachableError(f"codex app-server の起動に失敗: {e}") from e
    with _STATE_LOCK:
        _proc = proc
        _init_ok = False
    _reader_t = threading.Thread(
        target=_reader, args=(proc,), daemon=True, name="codex-appserver-reader")
    _errdrain_t = threading.Thread(
        target=_errdrain, args=(proc,), daemon=True,
        name="codex-appserver-err")
    _reader_t.start()
    _errdrain_t.start()
    try:
        res = _raw_request("initialize", {
            "clientInfo": dict(_CLIENT_INFO),
            "capabilities": {"experimentalApi": True},
        }, timeout=_SPAWN_TIMEOUT)
        _write({"method": "initialized", "params": {}})
    except Exception:
        _mark_dead()
        try:
            proc.kill()
        except Exception:
            pass
        raise
    with _STATE_LOCK:
        _init_result = res or {}
        _init_ok = True
    log.info("codex app-server ready: %s",
             (res or {}).get("userAgent") or "unknown")
    return proc


def _ensure_proc() -> subprocess.Popen:
    """生きた app-server を返す。無ければ spawn（同時 spawn 防止つき）"""
    with _STATE_LOCK:
        proc = _proc
        if proc is not None and proc.poll() is None and _init_ok:
            return proc
    with _SPAWN_LOCK:
        # _spawn は必ずこのロック配下。2番目の呼出しは前の spawn+init が
        # 終わってから再確認するので二重起動にならない
        with _STATE_LOCK:
            proc = _proc
            if proc is not None and proc.poll() is None and _init_ok:
                return proc
        return _spawn()


def _raw_request(method: str, params: dict, timeout: float):
    """_proc へ request を投げて result を待つ。spawn 中の initialize も
    ここを通る（_ensure_proc を挟まないので再入しない）"""
    rid = next(_seq)
    q: queue.Queue = queue.Queue()
    _pending[rid] = q
    try:
        _write({"id": rid, "method": method, "params": params})
    except BodyUnreachableError:
        _pending.pop(rid, None)
        raise
    try:
        msg = q.get(timeout=timeout)
    except queue.Empty:
        _pending.pop(rid, None)
        raise BodyAPIError(
            f"{method} 応答待ちが {timeout:.0f}s で切れた",
            status_code=408)
    if "error" in msg and msg["error"] is not None:
        err = msg["error"] or {}
        raise BodyAPIError(
            f"{method} -> {err.get('code')}: {err.get('message')}",
            status_code=err.get("code"))
    return msg.get("result") or {}


def _request(method: str, params: dict, timeout: float = _REQUEST_TIMEOUT):
    """JSON-RPC request を投げて result を返す。error は BodyAPIError"""
    _ensure_proc()
    return _raw_request(method, params, timeout)


# ---------------------------------------------------------------------------
# 関数面（runtime/body.py から呼ばれる。全て sync）
# ---------------------------------------------------------------------------

def health(spawn: bool = True) -> dict:
    """app-server の spawn + initialize 疎通。InitializeResponse を返す。
    spawn=False なら起動済みのときだけ値を返し、未起動は到達不可扱い
    （status 確認で勝手にプロセスを立てないため）"""
    with _STATE_LOCK:
        up = _proc is not None and _proc.poll() is None and _init_ok
    if not up and not spawn:
        raise BodyUnreachableError("未起動（初めての依頼で起動）")
    _ensure_proc()
    return dict(_init_result)


def _sandbox_policy() -> str:
    name = (CODEX_BODY_SANDBOX or "workspace-write").strip()
    if name not in ("read-only", "workspace-write", "danger-full-access"):
        return "workspace-write"
    return name


# thread/start では SandboxMode（kebab 文字列）、turn/start では
# SandboxPolicy オブジェクト（camel type）と表記が違う
_SANDBOX_TURN = {
    "read-only": {"type": "readOnly", "networkAccess": False},
    "workspace-write": {
        "type": "workspaceWrite",
        "writableRoots": [],
        "networkAccess": False,
    },
    "danger-full-access": {"type": "dangerFullAccess"},
}


def _approval_policy():
    pol = (CODEX_BODY_APPROVAL_POLICY or "on-request").strip()
    return pol if pol in ("untrusted", "on-request", "never") else "on-request"


def create_run(task: str, session_id: str | None = None,
               instructions: str | None = None) -> dict:
    """codex thread に作業を出す。session_id=thread_id（label 継続）。
    run_id="{thread_id}/{turn_id}" を返す"""
    turn_params: dict = {
        "input": [{"type": "text", "text": task}],
        "approvalPolicy": _approval_policy(),
        "approvalsReviewer": "user",
        "sandboxPolicy": _SANDBOX_TURN[_sandbox_policy()],
    }
    if CODEX_BODY_MODEL:
        turn_params["model"] = CODEX_BODY_MODEL
    if CODEX_BODY_EFFORT:
        turn_params["effort"] = CODEX_BODY_EFFORT

    if session_id:
        resume_params: dict = {
            "threadId": session_id,
            "approvalPolicy": _approval_policy(),
            "approvalsReviewer": "user",
            "excludeTurns": True,
        }
        res = _request("thread/resume", resume_params, timeout=60)
        thread_id = session_id
    else:
        start_params: dict = {
            "cwd": str(CODEX_BODY_CWD),
            "sandbox": _sandbox_policy(),
            "approvalPolicy": _approval_policy(),
            "approvalsReviewer": "user",
        }
        if instructions:
            start_params["developerInstructions"] = instructions
        if CODEX_BODY_MODEL:
            start_params["model"] = CODEX_BODY_MODEL
        res = _request("thread/start", start_params, timeout=60)
        thread = res.get("thread") or {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise BodyAPIError(
                f"thread/start が thread.id を返しません: {res}")

    turn_params["threadId"] = thread_id
    try:
        res = _request("turn/start", turn_params, timeout=60)
    except BodyAPIError:
        raise
    turn = res.get("turn") or {}
    turn_id = str(turn.get("id") or "")
    if not turn_id:
        raise BodyAPIError(f"turn/start が turn.id を返しません: {res}")
    _joined.add(thread_id)
    return {
        "run_id": f"{thread_id}/{turn_id}",
        "thread_id": thread_id,
        "turn_id": turn_id,
        "body_session": thread_id,
    }


def _split_run(run_id: str) -> tuple[str, str]:
    tid, _, turn = (run_id or "").partition("/")
    return tid, turn


def run_status(run_id: str) -> dict:
    """turn の現在状態。{status, output, error, run_id, session_id}"""
    tid, turn_id = _split_run(run_id)
    if not tid or not turn_id:
        raise BodyAPIError(f"run_id の形式が不正: {run_id}", status_code=404)
    res = _request("thread/turns/list",
                   {"threadId": tid, "limit": 50, "itemsView": "full"},
                   timeout=30)
    items = res.get("items") or res.get("turns") or []
    turn = next((t for t in items if str(t.get("id")) == turn_id), None)
    if turn is None:
        raise BodyAPIError(
            f"thread/turns/list に turn が見つからない: {run_id}",
            status_code=404)
    raw = turn.get("status") or ""
    status = _TURN_STATUS.get(raw, "running")
    err = ""
    te = turn.get("error") or {}
    if isinstance(te, dict):
        err = str(te.get("message") or "")
    return {
        "status": status,
        "output": _turn_output(turn),
        "error": err,
        "run_id": run_id,
        "session_id": tid,
    }


def run_steer(run_id: str, text: str) -> dict:
    """走行中 turn に追加指示（turn/steer）。非activeなら 409 相当"""
    tid, turn_id = _split_run(run_id)
    try:
        return _request("turn/steer", {
            "threadId": tid,
            "expectedTurnId": turn_id,
            "input": [{"type": "text", "text": text}],
        })
    except BodyAPIError as e:
        # expectedTurnId 不一致・非active の失敗を 409 に正規化
        raise BodyAPIError(str(e), status_code=409) from e


def run_stop(run_id: str) -> dict:
    tid, turn_id = _split_run(run_id)
    try:
        return _request("turn/interrupt",
                        {"threadId": tid, "turnId": turn_id})
    except BodyAPIError as e:
        raise BodyAPIError(str(e), status_code=409) from e


def run_approval(run_id: str, choice: str,
                 request_id: str | None = None) -> dict:
    """保留中の server→client 承認要求へ応答を返す。
    choice は "once"（許可）/"deny"（却下）"""
    if request_id is None:
        cands = [k for k, v in _appr.items() if v.get("run_id") == run_id]
        if not cands:
            raise BodyAPIError(
                "その run の承認待ちが見つかりません", status_code=409)
        request_id = cands[-1]
    ent = _appr.pop(str(request_id), None)
    if ent is None:
        raise BodyAPIError(
            f"承認要求 {request_id} は保留されていません", status_code=409)
    method = ent["method"]
    params = ent.get("params") or {}
    approve = choice == "once"

    if method == "item/permissions/requestApproval":
        if approve:
            # 要求された profile をそのまま返す（scope は turn 単位に絞る）
            _send_response(_num_or_str(request_id), result={
                "permissions": params.get("permissions") or {},
                "scope": "turn",
            })
        else:
            _send_response(_num_or_str(request_id), error={
                "code": -32000, "message": "denied by companion owner"})
        return {"ok": True}

    on_ok, on_ng = _APPROVAL_METHODS[method]
    _send_response(_num_or_str(request_id), result={
        "decision": on_ok if approve else on_ng})
    return {"ok": True}


def _num_or_str(rid: str):
    """JSON-RPC id は数値で来たものは数値で返す"""
    try:
        return int(rid)
    except (TypeError, ValueError):
        return rid


def pending_approval_for(run_id: str) -> dict | None:
    """run ごとの保留承認（request_id, summary）。run_check の補完用"""
    for rid, ent in _appr.items():
        if ent.get("run_id") == run_id:
            return {
                "request_id": rid,
                "summary": _approval_summary(
                    ent["method"], ent.get("params") or {}),
            }
    return None


async def run_events(run_id: str, last_seq: int = -1):
    """run（turn）のイベント流を (seq, ev) で yield する。

    接続ごとの thread 購読が前提なので、先に thread/resume で join して
    から購読キューに繋ぐ。現在値のスナップショット（既終了なら
    finished を即 yield）を先に出してから live を垂れ流す。
    """
    tid, _turn_id = _split_run(run_id)
    _ensure_proc()
    # 再接続: thread/resume でこの接続の購読を復帰させる。
    # 同じ接続で二重 resume すると通知が二重に届くので初回だけ
    if tid not in _joined:
        await asyncio.to_thread(
            _request, "thread/resume",
            {"threadId": tid, "excludeTurns": True,
             "approvalPolicy": _approval_policy(),
             "approvalsReviewer": "user"},
            60)
        _joined.add(tid)

    q: queue.Queue = queue.Queue()
    _subs.setdefault(tid, []).append(q)
    try:
        # スナップショット: 既に終了している turn なら finished を即出す
        try:
            st = await asyncio.to_thread(run_status, run_id)
        except BodyAPIError:
            st = None
        if st and st.get("status") in (
                "completed", "failed", "interrupted", "cancelled"):
            yield (next(_ev_seq), {
                "kind": "finished", "status": st["status"],
                "output": st.get("output") or "",
                "error": st.get("error") or "",
            })
            return
        while True:
            ev = await asyncio.to_thread(q.get)
            if ev is _DEAD:
                return
            yield (ev.get("seq"), ev)
    finally:
        subs = _subs.get(tid)
        if subs and q in subs:
            subs.remove(q)
            if not subs:
                _subs.pop(tid, None)


def shutdown() -> None:
    """app-server subprocess を止める（runtime.shutdown から呼ぶ）"""
    with _STATE_LOCK:
        proc = _proc
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    _mark_dead()
