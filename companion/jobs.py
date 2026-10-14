"""定期ジョブ: リマインダー通知・日次digest・週次・Obsidian同期・夜間reflection
・委譲ジョブ完了の通知取り込み・通知の配送（outbox）

失敗時の方針:
- 通知は notify_outbox に先入れてから送る。送信成功まで起点（リマインダーの
  fired、委譲の notify_status）を完了扱いにしない
- 日次処理は job_runs(job, run_date) で「未実行/実行中/完了/失敗」を管理し、
  時刻ぴったりを外しても同日中に拾う。失敗は当日最大3回まで再試行
- 起動時に pending のまま残った mem0 保存は「結果不明」、running のままの
  ジョブ実行は「中断」とみなして掃き替える
"""
import asyncio
import hashlib
import logging
import time
from datetime import datetime
from pathlib import Path
from . import (
    backup,
    bodyruns,
    db,
    engine,
    profile_provision,
    reflect,
    skill_export,
    skill_gen,
    skill_stats,
    skills,
    soul,
    watchdog,
)
from .adapters import live as canonical_lane
from .discord_out import UndeliverableError
from .runtime import body
from .config import (
    BACKUP_TIME,
    DIGEST_TIME,
    LLM_MODEL,
    OBSIDIAN_SYNC_TIME,
    REFLECT_NOTIFY,
    REFLECT_TIME,
    ROOT,
    SKILL_GEN_TIME,
    SKILL_SCAN_MIN,
    SKILL_STATS_TIME,
    SKILL_SUMMARY_MODEL,
    WEEKLY_DAY,
    WEEKLY_TIME,
)
from .db import enqueue_notification, get_db, load_state, resolve_notify_channel
from .llm import llm
from .memory import add_self_note, all_user, format_memories
from . import memory as memory_mod
from .tools import delegate
from .tools.obsidian import sync_obsidian
from .tools.tasks import fmt_tasks

log = logging.getLogger("companion")

_NOTIFY_MAX_ATTEMPTS = 6


# ---------- リマインダー → 送信待ち ----------

async def _enqueue_due_reminders() -> None:
    """期限到来のリマインダーを送信待ちに載せる。
    fired=1 になるのは実際に送信できたとき（mark_notification_sent）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,channel_id,text FROM reminders WHERE fired=0 AND due_at<=?",
            (now,),
        ).fetchall()
    for rid, cid, text in rows:
        cid = cid or resolve_notify_channel()
        if not cid:
            log.warning("reminder %s has no channel; left unfired", rid)
            continue
        # dedupe_key で冪等。毎tick再評価しても重複登録されない
        enqueue_notification(f"reminder:{rid}", cid, f"⏰ {text}")


# ---------- 通知の配送 ----------

async def _deliver_notifications(send) -> None:
    for nid, key, cid, body, attempts in db.due_notifications():
        try:
            await send(cid, body)
        except UndeliverableError as e:
            # チャンネル自体が消えた/権限なし → 再試行しても変わらないので要対応
            db.mark_notification_failed(nid, str(e)[:200], give_up=True)
            log.warning("notify %s undeliverable: %s", key, e)
        except Exception as e:
            give_up = attempts + 1 >= _NOTIFY_MAX_ATTEMPTS
            db.mark_notification_failed(nid, str(e)[:200], give_up=give_up)
            log.exception(
                "notify %s failed (attempt %d)%s",
                key, attempts + 1, " -> needs_attention" if give_up else "",
            )
        else:
            db.mark_notification_sent(nid)


# ---------- 日次ジョブの実行管理 ----------

async def _run_daily(job: str, sched_hm: str, legacy_key: str, fn) -> None:
    """予定時刻を過ぎた未実行分を1日1回単位で実行。失敗は限定回数だけ再試行"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.strftime("%H:%M") < sched_hm:
        return
    legacy_done = load_state().get(legacy_key) == today
    if not db.job_run_begin(job, today, legacy_done=legacy_done):
        return
    try:
        status = await fn(now)  # "done" or "skipped"; 失敗は例外で
    except Exception as e:
        log.exception("%s run failed", job)
        db.job_run_finish(job, today, "failed", str(e)[:300])
        return
    db.job_run_finish(job, today, status)


async def _post_daily_digest(now: datetime) -> str:
    today = now.strftime("%Y-%m-%d")
    with get_db() as conn:
        tasks = conn.execute(
            "SELECT id,title,due,note FROM tasks WHERE done=0 "
            "AND due IS NOT NULL AND due<=date(?, '+3 days') ORDER BY due",
            (today,),
        ).fetchall()
        rems = conn.execute(
            "SELECT text,due_at FROM reminders WHERE fired=0 AND due_at LIKE ?",
            (f"{today}%",),
        ).fetchall()
    if not tasks and not rems:
        return "skipped"
    parts = [f"**{today} のまとめ**"]
    if tasks:
        parts.append("期限3日以内のタスク:\n" + fmt_tasks(tasks))
    if rems:
        parts.append("今日のリマインド:\n" + "\n".join(f"- {r[1][-5:]} {r[0]}" for r in rems))
    cid = resolve_notify_channel()
    if not cid:
        raise RuntimeError("通知先チャンネルが未解決です")
    enqueue_notification(f"digest:{today}", cid, "\n\n".join(parts))
    return "done"


async def _post_weekly_summary(now: datetime) -> str:
    res = await asyncio.to_thread(
        all_user, top_k=200
    )
    mems = format_memories(res)
    prompt = (
        "以下はユーザーについての長期記憶の一覧です。直近1週間分っぽい内容を中心に、"
        "今週の出来事・気分・進んだことを5行程度の振り返りにまとめてください。"
        "情報がなければ「今週はまだ記憶が少ないです」とだけ返してください。\n\n" + mems
    )
    res2 = await llm.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        reasoning_effort="low",
    )
    summary = res2.choices[0].message.content or ""
    cid = resolve_notify_channel()
    if not cid:
        raise RuntimeError("通知先チャンネルが未解決です")
    today = now.strftime("%Y-%m-%d")
    enqueue_notification(f"weekly:{today}", cid, "**今週の振り返り**\n" + summary)
    return "done"


async def _run_obs_sync(now: datetime) -> str:
    res = await sync_obsidian()
    return "skipped" if "未設定" in str(res) else "done"


async def _run_backup(now: datetime) -> str:
    res = await asyncio.to_thread(backup.run_backup)
    for e in res["errors"]:
        log.warning("backup partial: %s", e)
    return "done"


async def _run_reflect(now: datetime) -> str:
    today = now.strftime("%Y-%m-%d")
    result = await reflect.run_reflection()
    if REFLECT_NOTIFY and result.get("insights"):
        cid = resolve_notify_channel()
        if cid:
            body = "\n".join(f"- {t}" for t in result["insights"])
            enqueue_notification(
                f"reflect:{today}", cid, "**おやすみ前の振り返り**\n" + body
            )
        else:
            log.warning("reflect notify skipped: no channel")
    return "done"


# ---------- 委譲ジョブの完了取り込み ----------

_DELEGATE_STATUS_LABEL = {
    "done": "完了", "failed": "失敗", "timeout": "タイムアウト",
    "cancelled": "キャンセル", "interrupted": "再起動で中断",
}


async def _record_delegate_results() -> None:
    # 終了検知・タイムアウト処理・再起動後の interrupted 掃き替え
    delegate.check_finished()
    # 通知は notify_status='pending' の完了ジョブから。check_finished の
    # 一時的な戻り値ではなく永続化された印を見るので再起動しても拾える
    for (jid,) in delegate.jobs_needing_notify():
        rep = delegate.job_report(jid)
        if not rep:
            delegate.mark_notify_queued(jid)
            continue
        label = _DELEGATE_STATUS_LABEL.get(rep["status"], rep["status"])
        try:
            await asyncio.to_thread(
                add_self_note,
                f"codexへの依頼 #{jid}（{rep['request'][:40]}）が{label}になった",
                importance=3,
            )
        except Exception:
            log.exception("delegate self-note failed")
        parts = [f"🔧 依頼 #{jid} {label}: {rep['request']}"]
        if rep["status"] == "done":
            if rep.get("final"):
                parts.append(rep["final"][:800])
            if Path(rep["workdir"]) == ROOT:
                parts.append("反映するには再起動が必要だよ（self_restart でできる）")
        else:
            tail = (rep.get("log_tail") or "").strip()
            if tail:
                parts.append("```\n" + tail[-600:] + "\n```")
        cid = resolve_notify_channel()
        if not cid:
            continue  # 通知先が解決できるまで pending のまま残す
        enqueue_notification(f"delegate_done:{jid}", cid, "\n\n".join(parts))
        delegate.mark_notify_queued(jid)


# ---------- SOUL 写し / スキル配布・取り込み ----------

async def _sync_skills_export() -> None:
    """§7.2-7.3: data/skills-export/ の同期と upstream 照合。

    写しのずれは正本復元＋改訂提案として inbox に回す（この tick の
    skills_inbox 走査で拾えるよう、このステップは scan より先に置く）。
    upstream 消失（upstream_gone）は自己メモに残す（通知は check_upstream
    内で outbox 登録済み）。"""
    res = await asyncio.to_thread(skill_export.sync_export)
    if res.get("proposed"):
        log.info(
            "skills export drift -> revision proposals: %s",
            res["proposed"],
        )
    up = await asyncio.to_thread(skill_export.check_upstream)
    for name in up.get("flagged", []):
        try:
            await asyncio.to_thread(
                add_self_note,
                f"スキル「{name}」が身体側の技能一覧から消えた（upstream_gone）",
                importance=2,
            )
        except Exception:
            log.exception("skill upstream self-note failed")


async def _skill_summary(name: str, kind: str, diff) -> str:
    """staged 通知に添える裏方モデルの事前要約（§7.5）。
    判断材料であって自動承認の根拠にはしない。失敗時は空（通知は出す）"""
    _row, body, _warn = await asyncio.to_thread(skills.read_skill, name)
    if not body:
        return ""
    head = "新しいスキル候補" if kind == "new" else "既存スキルの改訂提案"
    prompt = (
        f"以下はスキル候補「{name}」（{head}）の SKILL.md です。"
        "何をする手順かと、危ない点・前提となるツールがあればそれを、"
        "合計2行以内の日本語で要約してください。\n\n" + body[:3000]
    )
    try:
        res = await llm.chat.completions.create(
            model=SKILL_SUMMARY_MODEL or LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            reasoning_effort="minimal",
        )
        return (res.choices[0].message.content or "").strip()[:300]
    except Exception:
        log.exception("skill summary failed: %s", name)
        return ""


_skill_scan_last = 0.0


async def _skill_scan_tick() -> None:
    """export 同期→inbox 走査を SKILL_SCAN_MIN 秒間隔でまとめて回す。

    export の改訂提案を同じ走査で拾えるよう2段の順序は保つ。
    毎tickのディスク走査を減らすための間引き"""
    global _skill_scan_last
    now = time.monotonic()
    if _skill_scan_last and now - _skill_scan_last < SKILL_SCAN_MIN:
        return
    _skill_scan_last = now
    await _sync_skills_export()
    await _scan_skills_inbox()


async def _scan_skills_inbox() -> None:
    """skills-inbox を走査し、新たに承認待ちになった技能を通知する。

    §7.5: staged の通知は1件ずつ飛ばさず scan ごとに「N件届いた」と
    まとめる。添える判断材料はリスク旗（決定的）・前版との差分行数・
    裏方モデルの要約。承認するのはあくまでマスター。
    rejected も1件につき1回だけ知らせる。
    """
    res = await asyncio.to_thread(skills.scan_inbox)
    meta = res.get("meta") or {}
    staged = [(n, "new") for n in res["staged"]]
    staged += [(n, "restaged") for n in res["restaged"]]
    rejected = res["rejected"]
    if staged:
        lines = []
        keys = []
        for name, kind in staged:
            try:
                await asyncio.to_thread(
                    add_self_note, f"スキル候補「{name}」が届いた（要承認）",
                    importance=2,
                )
            except Exception:
                log.exception("skill self-note failed")
            row = db.skill_get(name) or {}
            keys.append(f"{name}:{row.get('content_hash', '')[:8]}")
            label = "新しい候補" if kind == "new" else "更新版"
            if (meta.get(name) or {}).get("origin") == "self":
                label += "（自分で起票）"
            d = (meta.get(name) or {}).get("diff")
            diff_s = f"、差分 +{d[0]}/-{d[1]}行" if d else ""
            head = f"・{name}（{label}{diff_s}）"
            if row.get("description"):
                head += f" — {row['description']}"
            flags = skills.risk_flags(row)
            if flags:
                head += f" ⚠ {'・'.join(flags)}言及"
            summary = await _skill_summary(name, kind, d)
            if summary:
                head += "\n  要約: " + summary
            lines.append(head)
        cid = resolve_notify_channel()
        if not cid:
            log.warning("skills staged but no notify channel")
        else:
            digest = hashlib.sha256(
                ";".join(sorted(keys)).encode()
            ).hexdigest()[:12]
            enqueue_notification(
                f"skill_staged_batch:{digest}",
                cid,
                f"📥 スキル候補が{len(staged)}件届いたよ"
                "（承認するまで私の技能にはならない）\n" + "\n".join(lines)
                + "\n中身は skill_read、承認は skill_approve で"
                '（"all-staged" なら一括で可）',
            )
    for name, reason in rejected:
        cid = resolve_notify_channel()
        if not cid:
            continue
        row = db.skill_get(name)
        h = (row or {}).get("content_hash", "")[:8]
        enqueue_notification(
            f"skill_rejected:{name}:{h}",
            cid,
            f"⚠️ スキル候補「{name}」は取り込み検査で弾いたよ（{reason[:120]}）",
        )


async def _run_skill_stats(now: datetime) -> str:
    """active 技能の成果追跡（週次）。使われ方レポートを通知し、
    「読まれた往復で tool 失敗が多い」技能は自己メモにも残す。
    自動 disable はしない（改訂・停止の判断はマスター）"""
    rep = await asyncio.to_thread(skill_stats.collect)
    body = skill_stats.format_report(rep)
    if not body:
        return "skipped"
    cid = resolve_notify_channel()
    if not cid:
        raise RuntimeError("通知先チャンネルが未解決です")
    today = now.strftime("%Y-%m-%d")
    enqueue_notification(f"skillstats:{today}", cid, body)
    for name in rep["flagged"]:
        try:
            await asyncio.to_thread(
                add_self_note,
                f"スキル「{name}」は読まれた往復で tool 失敗が多い"
                "（要見直し。停止・改訂はマスター判断）",
                importance=3,
            )
        except Exception:
            log.exception("skill stats self-note failed")
    return "done"


def _canonical_shadow_tick():
    """extraction→admission（＋増分索引）を回す。

    dual_run では mem0 の影、canonical 権威ではこれが記憶生成の
    本経路になる（mem0 権威では無操作）。"""
    try:
        if memory_mod.authority() in ("dual_run", "canonical"):
            canonical_lane.lane.shadow_tick()
    except Exception:
        log.exception("canonical shadow tick failed")


def _canonical_tier_tick():
    """L-TIER の日次 cycle。dual_run/canonical 権威で lane が開いていれば
    user/agent 両 scope を評価する（同日再実行は cycle_id で冪等）。
    writer 接続は lane worker 所有なので lane.call 経由で実行する。"""
    try:
        if memory_mod.authority() not in ("dual_run", "canonical"):
            return
        if canonical_lane.lane.writer is None:
            canonical_lane.lane.ensure_open(timeout=10.0)
        if canonical_lane.lane.writer is None:
            return

        def _work():
            w = canonical_lane.lane.writer
            adm = canonical_lane.lane.admission()
            if w is None or adm is None:
                return
            from .adapters.tiering import TierCycle
            tc = TierCycle(w, adm)
            tc.install_defaults()
            for scope in ("user", "agent"):
                res = tc.run(scope=scope)
                if res.get("transitions") or res.get("archive_pending"):
                    log.info(
                        "tier cycle %s/%s: transitions=%s archive_pending=%s",
                        res.get("cycle_id"), scope, res["transitions"],
                        res["archive_pending"])
        canonical_lane.lane.call(_work, timeout=120.0)
    except Exception:
        log.exception("canonical tier tick failed")


def _canonical_consolidate_tick():
    """近重複記憶の統合 cycle（C-5）。tier と同じく日次冪等で、
    提案は admission→候補/承認の正規ルートに流す（直接書き込まない）。"""
    try:
        if memory_mod.authority() not in ("dual_run", "canonical"):
            return
        if canonical_lane.lane.writer is None:
            canonical_lane.lane.ensure_open(timeout=10.0)
        if canonical_lane.lane.writer is None:
            return

        def _work():
            w = canonical_lane.lane.writer
            adm = canonical_lane.lane.admission()
            if w is None or adm is None:
                return
            from .adapters.consolidate import Consolidation
            cn = Consolidation(w, adm)
            for scope in ("user", "agent"):
                res = cn.run(scope=scope)
                if res.get("proposed"):
                    log.info(
                        "consolidation %s/%s: pairs=%s proposed=%s",
                        res.get("cycle_id"), scope, res["pairs"],
                        res["proposed"])
        canonical_lane.lane.call(_work, timeout=120.0)
    except Exception:
        log.exception("canonical consolidate tick failed")


# ---------- メインループ ----------

async def scheduler(send=None):
    """常駐ループ。send(cid, text) が通知の配送口。
    省略時は Runtime Interface（runtime.body.send_message = Discord REST）"""
    if send is None:
        send = body.send_message
    # 起動時の回復: 半書込みの疑いがある保存を「結果不明」に、
    # 中断した日次実行を「失敗」に、確定済み圧縮本文とファイルのずれを修復
    n = db.sweep_stale_pending_turns()
    if n:
        log.warning("%d turn(s) left pending at shutdown -> unknown", n)
    db.sweep_running_jobs()
    engine.sync_recent_context_file()
    summary = db.pending_outbox_summary()
    if any(summary.values()):
        log.warning("pending backlog at startup: %s", summary)

    while True:
        steps = [
            ("enqueue_reminders", _enqueue_due_reminders),
            ("retry_turn_mem0", engine.retry_turn_mem0),
            ("episode_material", reflect.process_episode_material),
            ("delegate_results", _record_delegate_results),
            ("body_runs", bodyruns.tick),
            ("soul_sync", lambda: asyncio.to_thread(soul.sync_soul)),
            # Hermes bot profile の自動補完（PROFILE_PROVISION_MIN 間隔）
            ("profile_provision",
             lambda: asyncio.to_thread(
                 profile_provision.ensure_companion_profiles)),
            # P8-B: canonical 記録 lane の滞留 drain＋worker 生存確認
            ("canonical_lane",
             lambda: asyncio.to_thread(canonical_lane.lane.tick)),
            # P8-7 Phase1: dual_run 中は extraction→admission(shadow) を回す
            ("canonical_shadow",
             lambda: asyncio.to_thread(_canonical_shadow_tick)),
            # P8-8: canonical 権威のとき tier cycle（夜間 reflection 同周期）
            ("canonical_tier",
             lambda: asyncio.to_thread(_canonical_tier_tick)),
            # C-5: 近重複記憶の統合 cycle（日次冪等・admission 経由）
            ("canonical_consolidate",
             lambda: asyncio.to_thread(_canonical_consolidate_tick)),
            # SKILL.md 走査は SKILL_SCAN_MIN 間隔に間引き（export→inbox の順序維持）
            ("skill_scan", _skill_scan_tick),
            # LLM/Hermes の不調検知（通知は discord_out で LLM 非依存）
            ("watchdog", watchdog.tick),
        ]
        for name, step in steps:
            try:
                await step()
            except Exception:
                log.exception("%s failed", name)

        now = datetime.now()
        await _run_daily("digest", DIGEST_TIME, "last_digest", _post_daily_digest)
        if now.weekday() == WEEKLY_DAY:
            await _run_daily("weekly", WEEKLY_TIME, "last_weekly", _post_weekly_summary)
            # active 技能の成果追跡（読まれ×往復失敗の相関。報告のみ）
            await _run_daily(
                "skillstats", SKILL_STATS_TIME, "last_skillstats",
                _run_skill_stats)
        await _run_daily("obs_sync", OBSIDIAN_SYNC_TIME, "last_obs_sync", _run_obs_sync)
        await _run_daily("reflect", REFLECT_TIME, "last_reflect", _run_reflect)
        await _run_daily("backup", BACKUP_TIME, "last_backup", _run_backup)
        # §7.6 Core スキル生成（R5f）。成功 trace → SKILL.md → inbox。
        # 同日の inbox 走査で拾えるよう reflect の直後に置く
        await _run_daily(
            "skillgen", SKILL_GEN_TIME, "last_skillgen",
            lambda now: skill_gen.run(),
        )

        try:
            await _deliver_notifications(send)
        except Exception:
            log.exception("deliver_notifications failed")

        await asyncio.sleep(30)
