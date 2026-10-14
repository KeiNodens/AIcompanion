import asyncio
import collections
import json
import logging
import os
import re
import tempfile
from datetime import datetime

from . import db, reflect
from .bridge import BridgeAborted
from .config import (
    BRIDGE_MAX_ROUNDS,
    DATA_DIR,
    DEFAULT_SYSTEM_PROMPT,
    HISTORY_LEN,
    INNER_MONOLOGUE,
    LLM_MODEL,
    MAX_TOOL_ROUNDS,
    PERSONA_PATH,
    RECENT_COMPRESS_EVERY,
    RECENT_CONTEXT_LIMIT,
    RECENT_CONTEXT_PATH,
    SYSTEM_PROMPT_PATH,
    USER_PROFILE_PATH,
)
from .llm import current_params, llm
from .memory import recall, recall_self, store_turn
from .adapters import live as canonical_lane
from .skills import prompt_index as skills_index
from .tools import TOOLS, dispatch_tool_result
from .tools.actor import (
    CURRENT_ACTOR,
    CURRENT_CHANNEL,
    CURRENT_PROFILE,
    CURRENT_SESSION,
)
from .tools.inner import inner_prompt, persona_notes

log = logging.getLogger("companion")

_recent = collections.defaultdict(lambda: collections.deque(maxlen=HISTORY_LEN))
# 同一セッションの「履歴取得→生成→履歴確定」を直列化するためのロック
_session_locks = collections.defaultdict(asyncio.Lock)

# 追跡中のバックグラウンドタスク（終了時に排水する）
_background_tasks: set[asyncio.Task] = set()
_compress_running = False

# 圧縮しない直近の往復数（HISTORY_LEN メッセージ ≒ 往復数に換算）
_KEEP_TURNS = max(1, (HISTORY_LEN + 1) // 2)


def _spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t


async def drain_background(timeout: float = 8.0) -> None:
    """終了前に未完了の記憶保存・圧縮タスクを待つ（best-effort）"""
    pending = [t for t in _background_tasks if not t.done()]
    if not pending:
        return
    _, pending = await asyncio.wait(pending, timeout=timeout)
    if pending:
        log.warning("%d background task(s) unfinished at shutdown", len(pending))


async def reset_session(session_key) -> None:
    # 生成中の会話が履歴を確定するまで待ってから消す（古い履歴での上書き防止）
    async with _session_locks[session_key]:
        _recent.pop(session_key, None)


def _read(path) -> str:
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _write_atomic(path, text: str) -> None:
    """同一ディレクトリの一時ファイル経由で原子置換する"""
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
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


async def _compress_recent() -> None:
    """直近履歴から外れた往復を一人称の rolling summary に折り畳む
    （connectome の autobiographical folding 相当の軽量版）。

    原料は turns テーブル。LLM失敗時は行を残して次回再試行し、
    圧縮本文と消費済み範囲は同じDBトランザクションで確定する。
    """
    global _compress_running
    if _compress_running:
        return
    _compress_running = True
    try:
        db.seed_recent_context(_read(RECENT_CONTEXT_PATH))
        rows = db.foldable_turns(_KEEP_TURNS)
        if not rows:
            return
        convo = "\n".join(
            f"マスター: {str(r[1])[:200]}\n私: {str(r[2])[:200]}" for r in rows
        )
        existing = db.get_recent_context_text()
        prompt = (
            "以下はあなた（コンパニオン）とマスターの会話のうち、直近履歴から溢れた部分です。\n"
            "あなたの一人称の視点で「最近マスターとどんな話をしていたか」の短い流れを"
            f"{RECENT_CONTEXT_LIMIT}字以内の文章としてまとめてください。\n"
            "事実の羅列ではなく「〜を話した」「〜と言われて嬉しかった」のような回想の口調で。\n"
            "既存の流れがある場合は、古くて薄い話題を落としつつ自然に続けてください。\n"
            "出力は本文のみ。\n\n"
            f"【既存の流れ】\n{existing or '（なし）'}\n\n【新しい会話】\n{convo}"
        )
        res = await llm.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            reasoning_effort="minimal",
        )
        text = (res.choices[0].message.content or "").strip()
        if not text:
            log.warning("recent_context compression returned empty; will retry")
            return
        text = text[: RECENT_CONTEXT_LIMIT * 2]
        db.commit_compressed_turns([r[0] for r in rows], text)
        log.info("recent_context compressed (%d turns -> %d chars)",
                 len(rows), len(text))
        try:
            _write_atomic(RECENT_CONTEXT_PATH, text + "\n")
        except Exception:
            # DBは確定済み。ファイルは次回の圧縮/起動時修復で再出力される
            log.exception("recent_context.md rewrite failed")
    except Exception:
        log.exception("recent_context compression failed")
    finally:
        _compress_running = False


def sync_recent_context_file() -> None:
    """起動時修復: DB確定済みの本文と recent_context.md がずれていれば再出力する"""
    try:
        db.seed_recent_context(_read(RECENT_CONTEXT_PATH))
        text = db.get_recent_context_text()
        if text and _read(RECENT_CONTEXT_PATH) != text:
            _write_atomic(RECENT_CONTEXT_PATH, text + "\n")
    except Exception:
        log.exception("recent_context.md sync failed")


async def _store_turn_task(turn_id, user_text: str, reply: str, ts) -> None:
    """mem0への往復保存。成否を turns.mem0_status に記録する"""
    try:
        await store_turn(user_text, reply, ts=ts)
        if turn_id is not None:
            db.mark_turn_mem0_done(turn_id)
    except Exception as e:
        log.exception("store_turn failed")
        if turn_id is not None:
            try:
                db.mark_turn_mem0_failed(turn_id, str(e)[:300])
            except Exception:
                log.exception("turn mem0 status update failed")


async def retry_turn_mem0(limit: int = 10) -> int:
    """保存に失敗した往復の再試行（schedulerから定期呼出）。処理件数を返す"""
    done = 0
    for tid, utext, atext, ts in db.turns_pending_mem0(limit):
        try:
            await store_turn(utext, atext, ts=ts)
            db.mark_turn_mem0_done(tid)
            done += 1
        except Exception as e:
            db.mark_turn_mem0_failed(tid, str(e)[:300])
    return done


def _push_turn(session_key, msg: dict) -> None:
    _recent[session_key].append(msg)


def _load_prompt() -> str:
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return DEFAULT_SYSTEM_PROMPT


def _load_persona() -> str:
    if PERSONA_PATH.exists():
        return PERSONA_PATH.read_text(encoding="utf-8").strip()
    return ""


_INNER_INSTRUCTION = """
# 内部独白
返信の前に、あなたの素の思考・気持ちを <inner> と </inner> の間に1-2文で書いてください。
これはユーザーには見せません（あなただけの心の中です）。
その後に <reply> と </reply> の間にマスターへの返信を書いてください。
"""

_SKIP_INSTRUCTION = """
# 返信の自由
この会話では、返信が不要だと感じたら返信本文の代わりに `[react:絵文字]` とだけ出力してよい
（例: `[react:😊]`。メッセージへの絵文字リアクションになる）。
何もしなくていいなら `[skip]` とだけ出力してよい。
ただしマスターが明確に話しかけている・質問している場合は必ず通常どおり返信すること。
"""


def render_system(now: str, memories: str, self_memories: str, allow_skip: bool,
                  profile=None) -> str:
    tpl = _load_prompt()
    repl = {
        "{datetime}": now,
        "{memories}": memories,
        "{self_memories}": self_memories,
        "{persona}": _load_persona() or "(persona.md 未作成)",
        "{persona_notes}": persona_notes() or "(まだありません)",
        "{inner}": inner_prompt(),
        "{user_profile}": _read(USER_PROFILE_PATH) or "(まだ生成されていません)",
        "{skills}": skills_index(profile) or "(まだない)",
        "{recent_context}": _read(RECENT_CONTEXT_PATH) or "(特になし)",
    }
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    # 未対応の {placeholder} がテンプレートに残っていたら除去（漏洩防止）
    tpl = re.sub(r"\{[a-z_]+\}", "", tpl)
    if profile:
        # 呼出し元 bot を明示: 記憶の文脈づけ・返答のしかたの判断材料にする
        tpl += f"\n（この会話は Hermes profile「{profile}」経由で届いている）"
    if INNER_MONOLOGUE:
        tpl += _INNER_INSTRUCTION
    if allow_skip:
        tpl += _SKIP_INSTRUCTION
    return tpl


_INNER_FALLBACK = "……ごめん、うまく言葉にできなかった"

# 推論モデルの <think>…</think> はユーザーに見せない（fail-closed:
# 未閉鎖なら以降すべて落とす）。INNER_MONOLOGUE 無効時でも最終 reply に
# 残さないよう、split 後ではなく常にこの関数で剥がす
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_THINK_TAIL_RE = re.compile(r"<think>.*$", re.S | re.I)


def _strip_think(text: str) -> str:
    """<think> ブロックと残りの </think> を取り除く"""
    if not text or ("<think" not in text and "</think" not in text):
        return text
    text = _THINK_BLOCK_RE.sub("", text)
    text = _THINK_TAIL_RE.sub("", text)
    return text.replace("</think>", "").strip()


def _split_inner(text: str) -> tuple[str, str]:
    """<inner>独白</inner><reply>返信</reply> を分離する（fail-closed）。

    <inner> が開かれたのに <reply> が取れない壊れた出力では、独白が
    チャンネルに漏れないようフォールバック文だけを返す。
    """
    m = re.search(r"<inner>(.*?)</inner>\s*<reply>(.*?)</reply>", text, re.S)
    if m:
        return m.group(1).strip(), (m.group(2).strip() or _INNER_FALLBACK)
    m = re.search(r"<inner>(.*?)</inner>\s*(.*)", text, re.S)
    if m:
        rest = re.sub(r"</?reply>", "", m.group(2)).strip()
        return m.group(1).strip(), (rest or _INNER_FALLBACK)
    if "<inner>" in text:
        # 未閉鎖: 以降はすべて独白扱い（漏洩防止）
        return text.split("<inner>", 1)[1].strip(), _INNER_FALLBACK
    m = re.search(r"<reply>(.*?)</reply>", text, re.S)
    if m:
        return "", m.group(1).strip()
    return "", text.strip()


async def generate(
    session_key, user_text: str, ts=None, images=None, allow_skip: bool = False,
    actor=None, notify_channel: int | None = None, source: str | None = None,
    bridge=None, profile=None,
) -> dict:
    # 同じセッションに連続して依頼が来ても会話順を保つ。
    # 別セッションは別ロックなので妨げない。
    async with _session_locks[session_key]:
        return await _generate(
            session_key, user_text, ts=ts, images=images,
            allow_skip=allow_skip, actor=actor, notify_channel=notify_channel,
            source=source, bridge=bridge, profile=profile,
        )


async def _generate(
    session_key, user_text: str, ts=None, images=None, allow_skip: bool = False,
    actor=None, notify_channel: int | None = None, source: str | None = None,
    bridge=None, profile=None,
) -> dict:
    memories = await recall(user_text)
    self_memories = await recall_self(user_text)
    now = datetime.now().strftime("%Y-%m-%d %H:%M (%a)")
    system = render_system(now, memories, self_memories, allow_skip,
                           profile=profile)
    history = list(_recent[session_key])

    if images:
        user_content = [{"type": "text", "text": user_text or "（画像）"}]
        for url in images:
            user_content.append({"type": "image_url", "image_url": {"url": url}})
    else:
        user_content = user_text

    msgs = [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": user_content},
    ]

    reply = ""
    calls_log = []
    aborted = False
    model, effort = current_params()
    # 形式橋（§3）: 身体ツールは body__* に改名して見せ、呼出しは emit で
    # Hermes に返して結果を待つ。内部ツールは従来どおりこの場で実行する
    tools = TOOLS + (bridge.schemas if bridge else [])
    max_rounds = BRIDGE_MAX_ROUNDS if bridge else MAX_TOOL_ROUNDS
    inner_rounds = 0   # 外部呼出しを含まないラウンド数（従来の上限カウント）
    actor_token = CURRENT_ACTOR.set(actor)
    channel_token = CURRENT_CHANNEL.set(notify_channel)
    session_token = CURRENT_SESSION.set(session_key)
    profile_token = CURRENT_PROFILE.set(profile)
    try:
        for _ in range(max_rounds):
            res = await llm.chat.completions.create(
                model=model,
                messages=msgs,
                tools=tools,
                tool_choice="auto",
                reasoning_effort=effort,
            )
            msg = res.choices[0].message
            if not msg.tool_calls:
                reply = msg.content or "(空の応答)"
                break
            msgs.append(msg.model_dump(exclude_none=True))
            external = (
                [tc for tc in msg.tool_calls if tc.function.name in bridge.names]
                if bridge else []
            )
            ext_ids = {id(tc) for tc in external}
            texts: dict[str, str] = {}
            for tc in msg.tool_calls:
                # 内部ツールを先に順に実行する（外部は emit 後にまとめて積む）
                if id(tc) in ext_ids:
                    continue
                # 不正な引数・未知のツール名で会話全体を止めない
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                log.info("tool call: %s %s", tc.function.name, args)
                result = await dispatch_tool_result(tc.function.name, args)
                calls_log.append(
                    {
                        "tool": tc.function.name,
                        "call_id": tc.id,
                        "args": args,
                        "status": result.status,
                        "origin": "core",
                        "result": (result.text or "")[:500],
                    }
                )
                texts[tc.id] = result.text
            if external:
                outgoing = [
                    {
                        "id": tc.id,
                        "name": bridge.names[tc.function.name],
                        "arguments": tc.function.arguments or "{}",
                    }
                    for tc in external
                ]
                try:
                    results = await bridge.emit(outgoing)
                except BridgeAborted:
                    aborted = True
                    results = []
                for i, tc in enumerate(external):
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                        if not isinstance(args, dict):
                            args = {}
                    except Exception:
                        args = {}
                    text = (
                        results[i] if not aborted and i < len(results)
                        else "（身体の操作が中断された）"
                    )
                    calls_log.append(
                        {
                            # 正本には Hermes 元名で残す
                            "tool": bridge.names[tc.function.name],
                            "call_id": tc.id,
                            "args": args,
                            "status": "aborted" if aborted else "ok",
                            "origin": "body_bridge",
                            "result": str(text)[:500],
                        }
                    )
                    texts[tc.id] = str(text)
            for tc in msg.tool_calls:
                # tool メッセージは元の tool_calls の順で積む
                msgs.append(
                    {"role": "tool", "tool_call_id": tc.id,
                     "content": texts[tc.id]}
                )
            if aborted:
                reply = "（身体での操作が途中で中断された）"
                break
            if not external:
                inner_rounds += 1
                if inner_rounds >= MAX_TOOL_ROUNDS:
                    reply = "(ツール呼び出しが多すぎたため中断しました)"
                    break
        else:
            reply = "(ツール呼び出しが多すぎたため中断しました)"
    finally:
        CURRENT_ACTOR.reset(actor_token)
        CURRENT_CHANNEL.reset(channel_token)
        CURRENT_SESSION.reset(session_token)
        CURRENT_PROFILE.reset(profile_token)

    inner_text = ""
    if INNER_MONOLOGUE:
        inner_text, reply = _split_inner(reply)
    reply = _strip_think(reply) or _INNER_FALLBACK

    try:
        with open(DATA_DIR / "tool_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "text": user_text,
                "has_images": bool(images),
                "profile": profile,
                "calls": calls_log,
            }, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("tool_log write failed")

    # 往復を先に永続化してから後処理を起こす。記録自体に失敗した場合は
    # 追跡なしで従来どおり保存を試みる（記憶の喪失を最優先で防ぐ）
    ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else ts
    try:
        turn_id = db.record_turn(
            session_key, user_text, reply, ts=ts_iso, source=source,
            inner_text=inner_text or None, profile=profile,
        )
    except Exception:
        log.exception("turn record failed; falling back to untracked write")
        turn_id = None

    # P8-B: canonical 記録 lane（「記録のみ」配線。障害は lane 側で
    # degraded 印＋通知に閉じ込め、live 応答を止めない）
    if turn_id is not None:
        try:
            canonical_lane.lane.submit_turn(
                turn_id, session_key, user_text, reply,
                ts_iso=ts_iso, source=source)
        except Exception:
            log.exception("canonical record lane submit failed")

    # ツール呼出しを turn_tool_calls テーブルにも残す。記録失敗で往復を止めない
    if turn_id is not None and calls_log:
        try:
            db.record_tool_calls(turn_id, session_key, calls_log)
        except Exception:
            log.exception("tool call trace record failed")

    _push_turn(session_key, {"role": "user", "content": user_text})
    _push_turn(session_key, {"role": "assistant", "content": reply})

    _spawn(_store_turn_task(turn_id, user_text, reply, ts))
    _spawn(reflect.maybe_store_self_episode(session_key))
    try:
        if db.count_foldable_turns(_KEEP_TURNS) >= RECENT_COMPRESS_EVERY:
            _spawn(_compress_recent())
    except Exception:
        log.exception("compression trigger check failed")
    return {
        "reply": reply,
        "inner": inner_text,
        "tools": [c["tool"] for c in calls_log],
        "model": model,
        "aborted": aborted,
    }
