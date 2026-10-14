"""MCP endpoint — companion を Hermes の MCP server にする面
（runtime-hermes-tooling.md §5 S3、R3）。

:8788 に /mcp の streamable HTTP endpoint を増やし、身体側 agent
（Hermes の run・cron）が Core の記憶・技能・通知経路に届く口を開ける。
露出する tool は3つに限る:

- core_context(query):   ユーザー記憶の recall + user_profile + recent_context
- core_report(text,...): 身体側の成果を「身体からの報告」ターンに通して配送
- core_skill_read(...):  active 技能の SKILL.md とパッケージ内 support files

入口は専用鍵 COMPANION_MCP_KEY（server.py のミドルウェアが検査し、/v1 の
鍵では /mcp は開かない面分離）。S1 の橋は bridge._SELF_PREFIXES が
mcp__companion* を弾くので、彼女が自分を呼ぶループは構造上起きない。
"""
import asyncio
import hashlib
import logging
import time
from pathlib import Path

from . import db, skills
from .config import (
    RECENT_CONTEXT_PATH,
    USER_PROFILE_PATH,
)

log = logging.getLogger("companion")

# core_context の各ブロック上限（文字）と core_report の本文上限
_CONTEXT_LIMIT = 3000
_REPORT_TEXT_LIMIT = 2000

# パッケージ内で core_skill_read が出してよいファイル名（SKILL.md は本体として返す）


def _read_text(path: Path) -> str:
    """存在して読めるときだけ中身を返す。無い・読めないときは空"""
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass
    return ""


async def core_context(query: str) -> str:
    """マスターに関する記憶・プロフィール・直近文脈を返す。

    身体側の run が「依頼文に書かれていない前提」を自分で引くための
    読取面。query はユーザー記憶の検索語（空なら記憶検索は飛ばす）。"""
    from . import memory  # 遅延import（Qdrant遅延起動・テスト差し替えに乗る）

    parts: list[str] = []
    q = (query or "").strip()
    if q:
        try:
            mems = await memory.recall(q)
        except Exception:
            log.exception("core_context recall failed")
            mems = ""
        if mems and mems.strip():
            parts.append("## ユーザーの記憶\n" + mems.strip())
    profile = _read_text(USER_PROFILE_PATH)
    if profile:
        parts.append("## プロフィール\n" + profile[:_CONTEXT_LIMIT])
    recent = _read_text(RECENT_CONTEXT_PATH)
    if recent:
        parts.append("## 直近の会話（圧縮）\n" + recent[-_CONTEXT_LIMIT:])
    return "\n\n".join(parts) if parts else "（引ける文脈はない）"


async def core_report(text: str, kind: str = "report", ref: str = "") -> str:
    """身体側の成果・報告を彼女に渡す（§5.1 core_report）。

    「[身体からの報告]」ターンを起こして彼女の口調の通知文を作り、
    既定の通知先（resolve_notify_channel）へ outbox 経由で配送する。
    kind は報告の種別ラベル（result/progress/alert 等の自由記述）、
    ref は run_id などの参照（同じ ref の配送中重複は冪等に潰れる）。"""
    from . import engine  # 遅延import（tools→engine→tools の循環回避）

    text = (text or "").strip()
    if not text:
        return "報告本文（text）が空"
    kind = ((kind or "").strip() or "report")[:30]
    ref = (ref or "").strip()[:80]
    cid = await asyncio.to_thread(db.resolve_notify_channel)
    if not cid:
        return "通知先が解決できない（Discord 宛先不明）。未配送"
    head = f"[身体からの報告:{kind}]"
    if ref:
        head += f" {ref}"
    try:
        res = await engine.generate(
            "desktop", head + "\n" + text[:_REPORT_TEXT_LIMIT],
            actor="desktop", source="body",
        )
        reply = res.get("reply") or ""
    except Exception:
        log.exception("core_report generate failed")
        # 生成に失敗しても報告そのものは届ける（素文フォールバック）
        reply = f"身体から{kind}の報告が届いた:\n{text[:800]}"
    key = (
        f"mcp_report:{ref}" if ref
        else f"mcp_report:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
    )
    await asyncio.to_thread(
        db.enqueue_notification, key, cid, reply or text[:800]
    )
    return "彼女に渡した（通知キューへ登録済み）"


def core_skill_read(name: str, file: str = "") -> str:
    """active 技能の SKILL.md 本文＋同梱ファイル一覧（§7.4）。
    file 指定でパッケージ内の support file（相対パス）を読む。
    staged/disabled/rejected は出さない。

    file はパッケージ根幹からの相対パスに限定し、".." や絶対パスで
    外に出る参照は拒否する。"""
    row, body, warn = skills.read_skill(name)
    if row is None:
        return f"技能「{(name or '').strip()}」は見つからない"
    if row.get("status") != "active":
        return f"技能「{name}」は {row.get('status')} なので読めない（active のみ）"
    rel = (file or "").strip().replace("\\", "/").lstrip("/")
    if not rel or rel.lower() == "skill.md":
        others = [
            f for f in skills.package_files(row) if f.lower() != "skill.md"
        ]
        tail = ("\n\n同梱ファイル: " + ", ".join(others)) if others else ""
        return body + warn + tail
    text, err = skills.read_skill_file(row, rel)
    return err or (text or "")


def build_mcp():
    """FastMCP インスタンスを作る。create_app ごとに新規にする
    （session_manager は run() を1度しか受け付けないため、複数の
    app/テストで共有しない）"""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    m = FastMCP(
        "companion",
        instructions=(
            "companion の Core 面。身体側 run が記憶・技能に届き、"
            "成果を core_report で彼女に返すための口"
        ),
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
    )
    m.add_tool(
        core_context,
        annotations=ToolAnnotations(
            readOnlyHint=True, title="Core context (memory/profile/recent)"
        ),
    )
    m.add_tool(
        core_report,
        annotations=ToolAnnotations(
            destructiveHint=False, title="Report result to companion"
        ),
    )
    m.add_tool(
        core_skill_read,
        annotations=ToolAnnotations(
            readOnlyHint=True, title="Read an active companion skill"
        ),
    )
    return m
