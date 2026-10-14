import asyncio
import base64
import collections
import io
import logging
import os
import re
import subprocess
from datetime import datetime

import discord
from discord.ext import commands
from . import engine, runtime
from .discord_out import UndeliverableError
from .config import (
    ALLOWED_CHANNEL_ID,
    DATA_DIR,
    DOCS_DIR,
    MAX_DOC_BYTES,
    MAX_IMAGE_BYTES,
    OWNER_USER_ID,
    REACT_MODE,
    ROOT,
    SPLIT_REPLY,
)
from .db import get_db, save_state, set_last_channel
from .memory import (all_agent, all_user, delete_item,
                     format_memories, search_user)
from .tools.delegate import delegate_approve
from .tools.docs import doc_ingest
from .tools.lists import list_names, list_show
from .tools.obsidian import sync_obsidian
from .tools.reminders import reminders_list
from .tools.tasks import tasks_list

log = logging.getLogger("companion")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.check
async def _owner_only_commands(ctx: commands.Context) -> bool:
    return bool(OWNER_USER_ID) and ctx.author.id == OWNER_USER_ID


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    # owner以外のコマンドはCheckFailureで止まる。無反応が仕様
    if isinstance(error, commands.CheckFailure):
        return
    log.warning("command error", exc_info=error)


# 同一チャンネルの受付→生成→送信を直列化して返信の順序を保つ
_channel_locks = collections.defaultdict(asyncio.Lock)


async def _bot_send(cid: int, text: str) -> None:
    """runtime の通知配送口: 接続済みクライアント経由でチャンネルに送る"""
    try:
        ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
        await ch.send(text[:1990])
    except (discord.NotFound, discord.Forbidden) as e:
        raise UndeliverableError(str(e)) from e


@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    # 常駐タスクの寿命と重複起動防止は runtime が持つ
    await runtime.ensure_started(_bot_send)


def _split_reply(reply: str) -> list[str]:
    """段落境界で最大3メッセージに分割（人間味の演出用）"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", reply) if p.strip()]
    if len(paras) <= 1:
        chunks = [reply]
    else:
        chunks, cur = [], ""
        for p in paras:
            nxt = (cur + "\n\n" + p) if cur else p
            if len(nxt) <= 900 and len(chunks) < 2:
                cur = nxt
            else:
                if cur:
                    chunks.append(cur)
                cur = p
        if cur:
            chunks.append(cur)
    out = []
    for c in chunks:
        for i in range(0, len(c), 1900):
            out.append(c[i : i + 1900])
    return out


@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.author.bot or message.content.startswith("!"):
        return
    # 応答も含めて全機能マスター専用。他者の発言は拾わない
    if not OWNER_USER_ID or message.author.id != OWNER_USER_ID:
        return
    is_dm = isinstance(message.channel, discord.DMChannel)
    mentioned = bot.user in message.mentions
    addressed = is_dm or mentioned or message.channel.id == ALLOWED_CHANNEL_ID
    if not addressed:
        return
    async with _channel_locks[message.channel.id]:
        await _respond(message, is_dm=is_dm, mentioned=mentioned)


async def _respond(message: discord.Message, *, is_dm: bool, mentioned: bool):
    text = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
    images = []
    pdf_paths = []
    for att in message.attachments:
        ct = att.content_type or ""
        if ct.startswith("image/") and att.size <= MAX_IMAGE_BYTES:
            try:
                data = await att.read()
                images.append(
                    f"data:{ct};base64,{base64.b64encode(data).decode()}"
                )
            except Exception:
                log.exception("attachment read failed")
        elif (
            ct == "application/pdf" or att.filename.lower().endswith(".pdf")
        ) and att.size <= MAX_DOC_BYTES:
            try:
                data = await att.read()
                safe = re.sub(r'[\\/:*?"<>|]', "_", att.filename).strip() or "doc.pdf"
                path = DOCS_DIR / safe
                path.write_bytes(data)
                pdf_paths.append(path)
            except Exception:
                log.exception("pdf attachment save failed")
    if not text and not images and not pdf_paths:
        return
    set_last_channel(message.channel.id)
    save_state(last_channel_id=message.channel.id)
    # 許可チャンネルでの自然発言のみ、絵文字/無言を許す（DM・メンションは必ず返信）
    allow_skip = (
        REACT_MODE
        and message.channel.id == ALLOWED_CHANNEL_ID
        and not is_dm
        and not mentioned
    )
    async with message.channel.typing():
        doc_notes = []
        for p in pdf_paths:
            try:
                doc_notes.append(await asyncio.to_thread(doc_ingest, str(p), "discord"))
            except Exception as e:
                log.exception("pdf ingest failed")
                doc_notes.append(f"添付PDF「{p.name}」の索引化に失敗しました: {e}")
        user_text = text
        if doc_notes:
            user_text = (user_text + "\n\n" if user_text else "") + "\n".join(
                f"[{n}]" for n in doc_notes
            )
        try:
            result = await engine.generate(
                message.channel.id, user_text, ts=message.created_at,
                images=images or None, allow_skip=allow_skip,
                actor=str(message.author.id),
                notify_channel=message.channel.id, source="discord",
            )
            reply = result["reply"]
        except Exception as e:
            log.exception("generate failed")
            reply = f"エラーが出ました: {e}\n(ChatMock が動いているか確認してください)"

    if allow_skip:
        m = re.match(r"^\[react:(.+)\]$", reply.strip())
        if m:
            try:
                await message.add_reaction(m.group(1).strip())
            except Exception:
                log.exception("reaction failed")
            return
        if reply.strip() == "[skip]":
            return

    parts = _split_reply(reply) if SPLIT_REPLY else [
        reply[i : i + 1900] for i in range(0, len(reply), 1900)
    ]
    for i, part in enumerate(parts):
        if i > 0:
            async with message.channel.typing():
                await asyncio.sleep(min(1.5, len(part) / 80))
        await message.channel.send(part)


@bot.command(name="memories")
async def memories_cmd(ctx: commands.Context):
    res = await asyncio.to_thread(
        all_user, top_k=50
    )
    text = format_memories(res)
    for i in range(0, len(text), 1900):
        await ctx.send(text[i : i + 1900])


@bot.command(name="self")
async def self_cmd(ctx: commands.Context):
    res = await asyncio.to_thread(
        all_agent, top_k=50
    )
    text = format_memories(res)
    for i in range(0, len(text), 1900):
        await ctx.send(text[i : i + 1900])


@bot.command(name="forget")
async def forget_cmd(ctx: commands.Context, *, query: str):
    res = await asyncio.to_thread(
        search_user, query, top_k=5
    )
    items = res.get("results", []) if isinstance(res, dict) else (res or [])
    if not items:
        await ctx.send("該当する記憶が見つかりませんでした")
        return
    await asyncio.to_thread(delete_item, items[0]["id"])
    await ctx.send(f"削除しました: {items[0].get('memory', items[0])}")


@bot.command(name="tasks")
async def tasks_cmd(ctx: commands.Context):
    await ctx.send("**未完了タスク**\n" + tasks_list())


@bot.command(name="lists")
async def lists_cmd(ctx: commands.Context, *, name: str = ""):
    await ctx.send(list_show(name) if name else "**リスト一覧**\n" + list_names())


@bot.command(name="reminders")
async def reminders_cmd(ctx: commands.Context):
    await ctx.send("**リマインダー**\n" + reminders_list())


@bot.command(name="export")
async def export_cmd(ctx: commands.Context):
    res = await asyncio.to_thread(
        all_user, top_k=500
    )
    mems = format_memories(res)
    self_res = await asyncio.to_thread(
        all_agent, top_k=500
    )
    self_mems = format_memories(self_res)
    with get_db() as conn:
        tasks = conn.execute(
            "SELECT id,title,due,note,done FROM tasks ORDER BY done, id"
        ).fetchall()
        items = conn.execute(
            "SELECT list_name,id,item,note FROM lists ORDER BY list_name, id"
        ).fetchall()
        rems = conn.execute(
            "SELECT id,text,due_at,fired FROM reminders ORDER BY due_at"
        ).fetchall()

    out = [f"# companion export {datetime.now():%Y-%m-%d %H:%M}", "\n## 長期記憶（マスター）\n", mems]
    out.append("\n## 長期記憶（私）\n" + self_mems)
    out.append("\n## タスク\n" + "\n".join(
        f"#{r[0]} [{'x' if r[4] else ' '}] {r[1]}"
        + (f" (期限:{r[2]})" if r[2] else "")
        + (f" {r[3]}" if r[3] else "")
        for r in tasks
    ) or "なし")
    out.append("\n## リスト\n" + "\n".join(
        f"[{r[0]}] #{r[1]} {r[2]}" + (f" ({r[3]})" if r[3] else "") for r in items
    ) or "なし")
    out.append("\n## リマインダー\n" + "\n".join(
        f"#{r[0]} {r[2]} — {r[1]}" + (" (発火済)" if r[3] else "") for r in rems
    ) or "なし")
    buf = io.BytesIO("\n".join(out).encode("utf-8"))
    await ctx.send(file=discord.File(buf, filename="companion-export.txt"))


@bot.command(name="sync")
async def sync_cmd(ctx: commands.Context):
    await ctx.send(await sync_obsidian())


@bot.command(name="reset")
async def reset_cmd(ctx: commands.Context):
    await engine.reset_session(ctx.channel.id)
    await ctx.send("この会話の短期記憶をリセットしました（長期記憶は残っています）")


@bot.command(name="approve")
async def approve_cmd(ctx: commands.Context, job_id: int = 0):
    """委譲ジョブの本人承認（owner専用）。LLMのツール経路では実行できない"""
    if not job_id:
        await ctx.send("使い方: `!approve <依頼番号>`")
        return
    result = await asyncio.to_thread(
        delegate_approve, job_id, str(ctx.author.id)
    )
    await ctx.send(result)


@bot.command(name="restart")
async def restart_cmd(ctx: commands.Context):
    """botを再起動する（owner専用。delegateでの変更反映用）"""
    await ctx.send("再起動するね。ちょっと待ってて")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    subprocess.Popen(
        ["cmd", "/c", "start", "", "/min", "restart.bat"],
        cwd=str(ROOT), creationflags=flags,
    )
    # 未完了の記憶保存・圧縮タスクを排水し、常駐タスクを止めてから落ちる
    try:
        await engine.drain_background(timeout=5.0)
    except Exception:
        log.exception("background drain failed")
    try:
        await runtime.shutdown(timeout=3.0)
    except Exception:
        log.exception("runtime shutdown failed")
    await asyncio.sleep(1)
    os._exit(0)


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN が .env に設定されていません")
    bot.run(token)


if __name__ == "__main__":
    main()
