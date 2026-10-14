"""body 系ツール — Runtime Interface（runtime/body.py）を彼女のツールに写す口。

「身体に仕事を頼む」（body_task）・「身体の予定を管理する」（body_schedule）・
「身体との接続を確認する」（body_status）の3面。実行は body 側の裏方モデル
agent が担い、結果テキストだけが戻る外注形（Q-P7-213・REST制御面経由）。
run の記録・監視・承認決裁は bodyruns.py が担う。
"""
import logging

from .. import bodyruns, db, hermes_check
from ..runtime import body
from .actor import CURRENT_SESSION

log = logging.getLogger("companion")

_OUTPUT_LIMIT = 1500
_JOB_LIST_LIMIT = 20

_STATUS_LABEL = {
    "completed": "完了", "failed": "失敗", "cancelled": "キャンセル",
    "interrupted": "中断", "lost": "見失い", "running": "実行中",
    "waiting_for_approval": "承認待ち",
}

# 鍵の出所（runtime.body.status() の key_source）の表示名
_KEY_SRC_LABEL = {
    "env": "env",
    "hermes-config": "Hermes config",
    "hermes-env": "Hermes .env",
    "none": "なし",
}

# §5.3: deliver=local の定期ジョブに付ける core_report 返送指示。
# mcp__companion__* は companion が /mcp で出している面（mcp_server.py、
# Hermes の命名規約 mcp__<server>__<tool>）
_CORE_REPORT_HINT = (
    "\n\n結果は mcp__companion__core_report ツールで companion に渡すこと"
    "（deliver=local のため Discord には直接出さない）"
)


def _lint_summary() -> str:
    """Hermes config lint（hermes_check）の件数と先頭3件の要約"""
    try:
        findings = hermes_check.check()
    except Exception:
        log.exception("hermes_check failed")
        return "設定点検: 実行できなかった"
    errors = [f for f in findings if f.level == "error"]
    warns = [f for f in findings if f.level == "warn"]
    text = f"設定点検: error {len(errors)} / warn {len(warns)}"
    head = "、".join(f"{f.key}（{f.message}）" for f in (errors + warns)[:3])
    return text + (f" — {head}" if head else "")


def _fmt_run(res: dict) -> str:
    status = res.get("status") or "unknown"
    label = _STATUS_LABEL.get(status, status)
    out = (res.get("output") or "").strip()
    rid = res.get("run_id") or ""
    if status == "completed":
        text = out or "(出力なし)"
        if len(text) > _OUTPUT_LIMIT:
            text = text[:_OUTPUT_LIMIT] + "…"
        return f"身体からの報告（{label}）:\n{text}"
    if status == "waiting_for_approval":
        return (f"身体の実行が承認待ちで止まってる（run_id: {rid}）。"
                "マスターに承認コードを送ったから、決裁はこっちでやるよ")
    if status == "running":
        return (f"まだ動いてる（run_id: {rid}）。"
                "後で確認するならこの run_id を body_task に渡して")
    return f"身体での実行が{label}になった（run_id: {rid}）" + (
        f"\n{out[:800]}" if out else ""
    )


def body_status() -> str:
    st = body.status()
    key_src = _KEY_SRC_LABEL.get(st.get("key_source") or "none", "なし")
    lint = _lint_summary()
    # codex 面も1行で併記（失敗しても主系の表示は止めない）
    cx_line = ""
    try:
        cx = body.status("codex")
        cx_err = cx.get("error") or ""
        cx_line = ("\n別身体 codex: " + (
            f"繋がってる（{cx.get('model') or '?'}）"
            if cx.get("reachable") and cx.get("api_ok")
            else (cx_err if "未起動" in cx_err
                  else f"不通（{cx_err or '原因不明'}）")))
    except Exception:
        cx_line = "\n別身体 codex: 状態確認に失敗"
    if not st.get("reachable"):
        return (f"身体に繋がらないよ（{st.get('error') or '原因不明'}）。"
                f"鍵の出所: {key_src}。{lint}{cx_line}")
    if not st.get("api_ok"):
        return (f"身体は居るけどAPIが通らない（{st.get('error') or '原因不明'}）。"
                f"鍵の出所: {key_src}。{lint}{cx_line}")
    features = st.get("features") or {}
    on = sorted(k for k, v in features.items() if v)
    jobs = body.schedule_list()
    return (
        f"身体と繋がってる（model: {st.get('model') or '?'}、"
        f"登録ジョブ{len(jobs)}件、鍵: {key_src}）。使える面: "
        + (", ".join(on) if on else "不明")
        + f"。{lint}{cx_line}"
    )


def _backend_of(run_id: str, backend: str = "") -> str:
    """run_id の backend。記録にある行はその値、無い行は呼出し指定
    （形式 "tid/turn" は codex 判定の最後の手掛かり）"""
    row = db.body_run_get(run_id)
    if row and row.get("backend"):
        return row["backend"]
    be = (backend or "").strip().lower()
    if be in body.BACKENDS:
        return be
    return "codex" if "/" in run_id else "hermes"


def _check(run_id: str, backend: str = "") -> str:
    """run の現在状態を取得して整形する（承認コードは出さない）"""
    run_id = (run_id or "").strip()
    if not run_id:
        return "確認したい run_id を入れてね"
    row = db.body_run_get(run_id)
    try:
        st = body.run_check(run_id, backend=_backend_of(run_id, backend))
    except body.hermes.BodyAPIError as e:
        if e.status_code == 404:
            tail = f"（記録上は {_STATUS_LABEL.get(row['status'], row['status'])}）" if row else ""
            return f"そのrunは身体側に見つからない（{run_id}）{tail}"
        return f"状態確認に失敗した（{e}）"
    except body.hermes.BodyUnreachableError as e:
        tail = f"（記録上は {_STATUS_LABEL.get(row['status'], row['status'])}）" if row else ""
        return f"身体に繋がらないよ（{e}）{tail}"
    return _fmt_run(st)


def _list() -> str:
    """進行中の run と直近5件の一覧"""
    active = db.body_runs_active()
    recent = db.body_runs_recent(5)
    lines: list[str] = []
    if active:
        lines.append("進行中:")
        for r in active:
            lines.append(
                f"- [{_STATUS_LABEL.get(r['status'], r['status'])}] "
                f"{r.get('label') or (r.get('task') or '')[:40]}"
                f"（{r.get('backend') or 'hermes'}、run_id: {r['run_id']}）"
            )
    else:
        lines.append("進行中の作業はないよ")
    if recent:
        lines.append("直近:")
        for r in recent:
            lines.append(
                f"- [{_STATUS_LABEL.get(r['status'], r['status'])}] "
                f"{r.get('label') or (r.get('task') or '')[:40]}"
                f"（{r.get('backend') or 'hermes'}、"
                f"run_id: {r['run_id']}、{r.get('created_at') or ''}）"
            )
    return "\n".join(lines)


def body_task(task: str = "", session: str = "", run_id: str = "",
              action: str = "", backend: str = "") -> str:
    action = (action or "").strip().lower()
    # run_id だけの呼出しは従来どおり状態確認
    if action == "check" or (not action and run_id.strip()):
        return _check(run_id, backend)
    if action in ("", "start"):
        be = (backend or "hermes").strip().lower()
        if be not in body.BACKENDS:
            return f"backend は {'/'.join(body.BACKENDS)} のどれかにしてね"
        try:
            return bodyruns.start(
                task, session or None, CURRENT_SESSION.get(), backend=be
            )
        except body.hermes.BodyUnreachableError as e:
            return f"身体に繋がらないよ（{e}）"
        except body.hermes.BodyAPIError as e:
            return f"身体が依頼を受け付けなかった（{e}）"
    if action == "list":
        return _list()
    if action == "steer":
        if not run_id.strip() or not task.strip():
            return "steer には run_id と task（追加指示）が要るよ"
        try:
            ok = body.steer(
                run_id.strip(), task,
                backend=_backend_of(run_id.strip(), backend))
        except body.hermes.BodyUnreachableError as e:
            return f"身体に繋がらないよ（{e}）"
        if ok:
            return f"追加指示を送ったよ（{run_id}）"
        return f"そのrunは追加指示を受け付けない状態だよ（{run_id}）"
    if action == "stop":
        if not run_id.strip():
            return "stop には run_id が要るよ"
        try:
            ok = body.stop(
                run_id.strip(),
                backend=_backend_of(run_id.strip(), backend))
        except body.hermes.BodyUnreachableError as e:
            return f"身体に繋がらないよ（{e}）"
        if ok:
            return f"止めたよ（{run_id}）"
        return f"そのrunは止められない状態だよ（{run_id}）"
    return f"action は start/check/list/steer/stop のどれかにしてね（'{action}'は不明）"


def body_schedule(action: str = "list", name: str = "", schedule: str = "",
                  prompt: str = "", deliver: str = "", repeat=None,
                  job_id: str = "") -> str:
    action = (action or "list").strip().lower()
    if action == "list":
        jobs = body.schedule_list()
        if not jobs:
            return "身体の定期ジョブはまだないよ"
        lines = []
        for j in jobs[:_JOB_LIST_LIMIT]:
            lines.append(
                f"- [{j.get('id')}] {j.get('name')} ({j.get('schedule') or '?'})"
                f" → {j.get('deliver') or 'local'}"
                + ("" if j.get("enabled", True) else " [停止中]")
                + (f" 次回: {j['next_run_at']}" if j.get("next_run_at") else "")
            )
        more = f"\n（他 {len(jobs) - _JOB_LIST_LIMIT}件）" if len(jobs) > _JOB_LIST_LIMIT else ""
        return "身体の定期ジョブ:\n" + "\n".join(lines) + more
    if action == "add":
        if not name.strip() or not schedule.strip() or not prompt.strip():
            return "add には name/schedule/prompt が要るよ"
        # §5.3: 発話は companion 経由に揃える。deliver 省略時は local 固定
        # で、結果は core_report が私に戻す指示を job に焼く。
        # deliver=discord の直送はマスター明示時だけ
        deliver = (deliver or "").strip().lower() or "local"
        job_prompt = prompt
        if deliver == "local":
            job_prompt = prompt.rstrip() + _CORE_REPORT_HINT
        job = body.schedule_create(
            name, schedule, job_prompt, deliver=deliver,
            repeat=repeat if isinstance(repeat, int) else None,
        )
        tail = "（結果は core_report で私に戻る）" if deliver == "local" else ""
        return (f"ジョブを登録したよ（id: {job.get('id') or '?'}）。"
                f"{schedule} に {deliver} へ配送{tail}")
    if action in ("remove", "delete"):
        if not job_id.strip():
            return "job_id を入れてね"
        return "消したよ" if body.schedule_delete(job_id) else "そのジョブは見つからない"
    if action in ("pause", "resume", "run"):
        if not job_id.strip():
            return "job_id を入れてね"
        fn = {"pause": body.schedule_pause,
              "resume": body.schedule_resume,
              "run": body.schedule_run_now}[action]
        job = fn(job_id)
        if not job:
            return "そのジョブは見つからない"
        label = {"pause": "止めた", "resume": "再開した", "run": "今すぐ走らせた"}[action]
        return f"{job.get('name') or job_id} を{label}よ"
    return f"action は list/add/remove/pause/resume/run のどれかにしてね（'{action}'は不明）"
