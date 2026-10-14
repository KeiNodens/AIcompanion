"""会話履歴検索ツール — turns 原文の部分一致。

recall_memory（mem0 の意味検索）が「あの話何だっけ」の曖昧想起向きなのに対し、
こちらは固有名詞・正確な文言の掘り起こし向き。bot 会話では既定でその
profile の往復だけを見る（profile="all" で全件）。
"""
import logging

from .. import db
from .actor import CURRENT_PROFILE

log = logging.getLogger("companion")


def history_search(query: str, profile: str | None = None,
                   limit: int | None = None) -> str:
    query = (query or "").strip()
    if not query:
        return "検索語を指定してください"
    limit = max(1, min(int(limit or 10), 50))
    if profile is None:
        profile = CURRENT_PROFILE.get()  # bot 経由の会話では既定でその profile
    elif profile in ("", "all", "*"):
        profile = None
    rows = db.search_turns(query, profile=profile, limit=limit)
    if not rows:
        scope = f"profile「{profile}」" if profile else "全会話"
        return f"{scope}の履歴に「{query}」は見つからなかった"
    lines = []
    for tid, _skey, utext, atext, ts, _src, prof in rows:
        prof_s = f" @{prof}" if prof else ""
        u = (utext or "").replace("\n", " ")[:120]
        a = (atext or "").replace("\n", " ")[:120]
        lines.append(f"[#{tid} {ts or ''}{prof_s}] 相手> {u} / 私> {a}")
    return "\n".join(lines)
