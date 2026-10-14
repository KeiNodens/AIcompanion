"""Runtime Interface — companion が body に求める能力の抽象面
（docs/BIgchange.md §23 / runtime-hermes.md 項目F・Q-P7-213 決着済み）。

呼び出し側（engine・jobs・tools）は body の固有名を知らない。
輸送はここで選ぶ:
- send_message: Discord チャンネル → discord_out の REST 送信。
  将来 body 側経由や別チャンネル種別が出たらここで振り分ける
- start_task / run_check / steer / stop / decide_approval / watch_run /
  status / schedule_* / skills: Hermes REST 制御面（runtime.hermes）へ委譲

Q-P7-213（2026-10-07 決着）: body tool の発火は model 経路の形式橋ではなく
REST 制御面経由。彼女の tool_calls は :8788 に返さず、body 側の仕事は
「裏方モデル agent への依頼」として別 run に切り離す。run の記録・
監視・承認決裁は bodyruns.py が担い、ここは Hermes のイベント名・
ペイロードを正規化して上に出すだけの面に留める。
会話中の body tool 呼出しは別系統の形式橋（bridge.py、Q-P7-219）が担う。
"""
import hashlib
import logging
import re

from .. import discord_out, hermes_conf
from ..config import BODY_OPS_SESSION, CODEX_BODY_MODEL
from . import hermes

log = logging.getLogger("companion")

# body backend の選択肢。run 固有の操作は body_runs.backend に記録した値で
# 振り分け、start_task のみ呼び出し側が選ぶ（既定 hermes = 従来どおり）
BACKENDS = ("hermes", "codex")


def _backend(name):
    """backend 名を runtime モジュールに解決する。未知の名は安全側に失敗"""
    n = (name or "hermes").strip().lower()
    if n == "codex":
        from . import codex
        return codex
    if n == "hermes":
        return hermes
    raise hermes.BodyAPIError(f"未知の身体 backend: {name}")

# run に載せる body 側実行係への指示（人格は要らない、作業報告だけ欲しい口）
_OPS_INSTRUCTIONS = (
    "あなたはコンパニオンの身体側の実行係です。依頼された作業を"
    "利用可能なツールで実行し、結果を簡潔に報告してください。"
)

# 作業ラベル → Hermes 側 session_id の許容文字
_LABEL_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


async def send_message(channel, text: str) -> None:
    """通知の配送口。channel は Discord チャンネルID。
    配送失敗の例外（UndeliverableError 等）は discord_out のものをそのまま通す"""
    await discord_out.send_channel_message(int(channel), text)


def status(backend: str = "hermes") -> dict:
    """body との接続確認。{reachable, api_ok, key_source, error?, model?, features?}

    key_source は API 鍵の出所（"env" / "hermes-config" / "hermes-env" /
    見つからなければ "none"）。切り分け用で、鍵の値は返さない。
    codex backend は鍵を使わないので key_source は "none" 固定"""
    if (backend or "hermes") == "codex":
        try:
            from . import codex
            res = codex.health(spawn=False)
        except hermes.BodyUnreachableError as e:
            return {"key_source": "none", "reachable": False,
                    "error": str(e)}
        except hermes.BodyAPIError as e:
            return {"key_source": "none", "reachable": True,
                    "api_ok": False, "error": str(e)}
        return {
            "key_source": "none", "reachable": True, "api_ok": True,
            "model": CODEX_BODY_MODEL or "codex-default",
            "features": {"approvals": True, "steer": True,
                         "interrupt": True},
            "user_agent": res.get("userAgent"),
        }
    _key, key_source = hermes_conf.api_server_key()
    base = {"key_source": key_source or "none"}
    try:
        hermes.health()
    except hermes.BodyUnreachableError as e:
        return {**base, "reachable": False, "error": str(e)}
    except hermes.BodyAPIError as e:
        return {**base, "reachable": True, "api_ok": False, "error": str(e)}
    try:
        caps = hermes.capabilities()
    except (hermes.BodyUnreachableError, hermes.BodyAPIError) as e:
        return {**base, "reachable": True, "api_ok": False, "error": str(e)}
    return {
        **base,
        "reachable": True,
        "api_ok": True,
        "model": caps.get("model"),
        "features": caps.get("features") or {},
        "endpoints": caps.get("endpoints") or {},
    }


def _session_for_label(label: str | None) -> str | None:
    """作業ラベルを身体側の継続セッションIDに写す。

    空は一発 run（None）、"ops" は決まった継続セッション BODY_OPS_SESSION、
    それ以外は companion-<slug>-<hash> の決定的ID（同じラベルは同じ
    transcript に戻る。日本語等のラベルでも衝突しないよう末尾にハッシュ）。
    """
    label = (label or "").strip()
    if not label:
        return None
    if label == "ops":
        return BODY_OPS_SESSION
    slug = _LABEL_SAFE.sub("-", label)[:32].strip("-") or "x"
    digest = hashlib.sha1(label.encode("utf-8")).hexdigest()[:8]
    return f"companion-{slug}-{digest}"


def _approval_summary(approval: dict) -> str:
    """approval.request ペイロードから人が読める要約を取る
    （command は Hermes が既に redact 済み）"""
    return str(
        approval.get("command") or approval.get("description") or "(内容不明)"
    )


def start_task(task: str, session: str | None = None,
               backend: str = "hermes",
               body_session: str | None = None) -> dict:
    """body 側 agent に依頼して run_id を返す（結果は待たない）。

    session は作業ラベル。hermes では _session_for_label で決定的な
    session_id に写す。codex ではラベル→thread_id の写像が body_runs に
    あるため、呼び出し側が引いた前回の body_session（thread_id）を
    渡して resume する（空なら新規 thread）。
    """
    if (backend or "hermes") == "codex":
        from . import codex
        created = codex.create_run(
            task, session_id=body_session or None,
            instructions=_OPS_INSTRUCTIONS,
        )
        return {"run_id": created["run_id"],
                "body_session": created.get("body_session") or ""}
    created = _backend(backend).create_run(
        task, session_id=_session_for_label(session),
        instructions=_OPS_INSTRUCTIONS,
    )
    run_id = created.get("run_id")
    if not run_id:
        raise hermes.BodyAPIError(f"/v1/runs が run_id を返しません: {created}")
    return {"run_id": run_id, "body_session": created.get("session_id") or ""}


def run_check(run_id: str, backend: str = "hermes") -> dict:
    """進行中/完了済みの run の現在状態を1回だけ取る。
    承認待ちなら approval={"request_id", "summary"} が入る"""
    be = _backend(backend)
    st = be.run_status(run_id)
    out = {
        "status": st.get("status") or "unknown",
        "output": st.get("output") or "",
        "error": st.get("error") or "",
        "run_id": run_id,
        "session_id": st.get("session_id") or "",
    }
    # codex は run_status が "running" のままでも承認要求を保留している
    # （server→client 要求で止まる）のでモジュール側の保留表を見る
    if (backend or "hermes") == "codex" and out["status"] == "running":
        ap = be.pending_approval_for(run_id)
        if ap:
            out["status"] = "waiting_for_approval"
            out["approval"] = {
                "request_id": ap.get("request_id"),
                "summary": ap.get("summary") or "(内容不明)",
            }
            return out
    if out["status"] == "waiting_for_approval":
        ap = st.get("approval") or {}
        out["approval"] = {
            "request_id": ap.get("request_id"),
            "summary": _approval_summary(ap),
        }
    return out


def steer(run_id: str, text: str, backend: str = "hermes") -> bool:
    """走行中の run に追加指示を送る。受け付けない状態（409等）なら False"""
    try:
        res = _backend(backend).run_steer(run_id, text)
    except hermes.BodyAPIError as e:
        if e.status_code == 409:
            return False
        raise
    return bool(res.get("accepted", True))


def stop(run_id: str, backend: str = "hermes") -> bool:
    """走行中の run を止める。止められない状態（409等）なら False"""
    try:
        res = _backend(backend).run_stop(run_id)
    except hermes.BodyAPIError as e:
        if e.status_code == 409:
            return False
        raise
    return bool(res.get("ok", True))


def decide_approval(run_id: str, approve: bool,
                    request_id: str | None = None,
                    backend: str = "hermes") -> dict:
    """身体側の承認要求に Core の決裁を転送する。

    {"ok": bool, "stale": bool, "error"?: str}。409（承認待ちでない/
    期限切れ）は stale=True で返す。到達不能は BodyUnreachableError
    のまま投げる（呼び出し側が「届いていない」を扱う）。
    """
    try:
        _backend(backend).run_approval(
            run_id, "once" if approve else "deny", request_id=request_id
        )
    except hermes.BodyAPIError as e:
        if e.status_code == 409:
            return {"ok": False, "stale": True}
        return {"ok": False, "stale": False, "error": str(e)}
    return {"ok": True, "stale": False}


def _normalize_event(seq, ev: dict) -> dict | None:
    """Hermes の run イベントを bodyruns 向けの正規化形に写す。
    関係ないイベント（message.delta 等）は None（捨てる）"""
    name = ev.get("event") or ""
    if name == "tool.started":
        return {
            "seq": seq, "kind": "tool_started",
            "tool": ev.get("tool"), "preview": ev.get("preview"),
        }
    if name == "tool.completed":
        return {
            "seq": seq, "kind": "tool_completed",
            "tool": ev.get("tool"), "duration": ev.get("duration"),
            "error": bool(ev.get("error")),
        }
    if name == "approval.request":
        return {
            "seq": seq, "kind": "approval_request",
            "request_id": ev.get("request_id"), "summary": _approval_summary(ev),
        }
    if name == "approval.responded":
        return {
            "seq": seq, "kind": "approval_responded",
            "choice": ev.get("choice"),
        }
    if name in ("run.completed", "run.failed", "run.cancelled",
                "run.interrupted"):
        return {
            "seq": seq, "kind": "finished", "status": name.split(".", 1)[1],
            "output": ev.get("output") or "", "error": ev.get("error") or "",
        }
    return None


async def watch_run(run_id: str, last_seq: int = -1,
                    backend: str = "hermes"):
    """run のイベント流を正規化して yield する。

    last_seq 以降から再開できる（Last-Event-ID。codex 側はイベント再送
    が無いので無視される）。通信断・APIエラーは runtime の例外を
    そのまま投げる（再接続の判断は bodyruns の watcher）。
    """
    if (backend or "hermes") == "codex":
        from . import codex
        async for _seq, ev in codex.run_events(run_id, last_seq=last_seq):
            yield ev
        return
    async for seq, ev in hermes.run_events(run_id, last_seq=last_seq):
        norm = _normalize_event(seq, ev)
        if norm is not None:
            yield norm


# ---------- body 側の予定管理（cron） ----------

def schedule_list() -> list:
    return hermes.jobs_list()


def schedule_create(name: str, schedule: str, prompt: str,
                    deliver: str = "discord", repeat: int | None = None) -> dict:
    return hermes.job_create(
        name, schedule, prompt, deliver=deliver, repeat=repeat
    )


def schedule_delete(job_id: str) -> bool:
    return hermes.job_delete(job_id)


def schedule_pause(job_id: str) -> dict:
    return hermes.job_pause(job_id)


def schedule_resume(job_id: str) -> dict:
    return hermes.job_resume(job_id)


def schedule_run_now(job_id: str) -> dict:
    return hermes.job_run_now(job_id)


def skills() -> list:
    """身体側が現在認識している技能の一覧（GET /v1/skills の data）。
    [{name, description, category}] — §7.3 の upstream 照合に使う"""
    return hermes.skills_list()


# ---------- 話者解決（入口 actor 判定） ----------

# x-companion-session が指す会話の source うち、発言者を「このPCの人間」と
# みなせるローカル面。api_server は Hermes APIキー必須の特権面（ローカル）。
# リモート面（discord 等）や未知の source は user_id 一致のみに倒す
_LOCAL_SOURCES = {
    "api_server", "cli", "tui", "desktop", "oneshot", "cron",
    "serve", "web", "run", "batch_runner", "mini_swe", "",
}


def resolve_actor(affinity: str) -> str | None:
    """x-companion-session ヘッダ値から話者を解決する。

    - Hermes 会話に user_id があればそれ（Discord 発言者のID）
    - user_id の無いローカル面（cli/desktop等）なら "desktop"
    - 解決不能・リモート面で user_id 無しなら None（非owner扱い）

    None を返してもリクエスト自体は拒否しない——返答は出すが
    owner専用ツール・承認コードの決裁は is_owner() 側で止まる。
    """
    ident = hermes.session_identity(affinity)
    if ident is None:
        return None
    uid = ident.get("user_id")
    if uid:
        return str(uid)
    if str(ident.get("source") or "") in _LOCAL_SOURCES:
        return "desktop"
    return None
