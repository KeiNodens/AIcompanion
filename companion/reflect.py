"""コンパニオン自身の記憶（agent scope）への書き込みと夜間reflection。

- maybe_store_self_episode: 会話の一定間隔で、一人称の経験メモを生成して保存
- run_reflection: 直近の経験メモから上位の洞察を合成して記憶に戻し、日記を書く。
  あわせてユーザープロファイル（static/dynamic）の再生成と、想起されない
  古い経験メモの demote を行う
"""
import asyncio
import collections
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from .config import (
    LLM_MODEL,
    OBSIDIAN_SYNC_DIR,
    OBSIDIAN_VAULT,
    SELF_MEMO_EVERY_N,
    TURNS_RETENTION_DAYS,
    USER_PROFILE,
    USER_PROFILE_PATH,
)
from .db import (
    get_reflection_insights,
    mark_turns_episode_done,
    purge_old_turns,
    save_reflection_insights,
    turns_pending_episode,
)
from .llm import llm
from .memory import (
    add_self_note,
    all_user,
    demote_stale,
    format_memories,
    self_memories_recent,
)

log = logging.getLogger("companion")

_turn_counter = collections.Counter()


async def _ask_json(prompt: str):
    """JSON配列を期待するLLM呼び出し。パース失敗は None（正当な空は []）"""
    res = await llm.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        reasoning_effort="minimal",
    )
    text = res.choices[0].message.content or ""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return [] if not text.strip() else None
    try:
        return json.loads(m.group(0))
    except Exception:
        log.warning("self-episode json parse failed: %s", text[:200])
        return None


async def maybe_store_self_episode(session_key) -> None:
    """SELF_MEMO_EVERY_N ターン毎に、未処理の往復を一人称の経験メモにする。
    材料は turns テーブルの episode_done=0（前回処理以降の全会話）"""
    _turn_counter[session_key] += 1
    if _turn_counter[session_key] % max(1, SELF_MEMO_EVERY_N) != 0:
        return
    await process_episode_material()


async def process_episode_material() -> None:
    """未処理の往復をまとめて経験メモ化。失敗しても材料（turns行）は残る"""
    rows = turns_pending_episode(30)
    if not rows:
        return
    try:
        parts = []
        for r in rows:
            stamp = f"[{r[5][:16]}] " if r[5] else ""
            parts.append(
                f"{stamp}マスター: {str(r[2])[:300]}\n私: {str(r[3])[:300]}"
            )
            if r[4]:
                parts.append(f"私（心の中）: {str(r[4])[:200]}")
        convo = "\n".join(parts)
        prompt = (
            "以下はあなた（コンパニオン）とマスターの最近の会話です。\n"
            "あなた自身の一人称の視点で「経験として残しておきたいこと」を書いてください。\n"
            "書くもの: あなたが感じたこと、言ったこと、二人の関係で気づいたこと、印象に残った出来事。\n"
            "マスター個人の事実情報は別の場所で記録されるので、ここではあなたの視点・感情・気づきを書く。\n"
            "出力はJSON配列のみ: [{\"text\": \"一文\", \"importance\": 1-5}]\n"
            "importanceは日常の気づき=1-2、印象的な出来事=3、関係が変わる節目=4-5。\n"
            "残す価値がない雑談なら [] とだけ返す。\n\n"
            f"会話:\n{convo}"
        )
        items = await _ask_json(prompt)
        if items is None:
            log.warning("self-episode generation failed (bad json); will retry")
            return
        for it in items[:3]:
            text = (it.get("text") or "").strip()
            if not text:
                continue
            imp = it.get("importance")
            try:
                imp = max(0, min(5, int(imp)))
            except Exception:
                imp = 3
            if imp < 1:
                continue
            await asyncio.to_thread(add_self_note, text, importance=imp)
            log.info("self memory stored (imp=%s): %s", imp, text[:60])
        mark_turns_episode_done([r[0] for r in rows])
    except Exception:
        log.exception("maybe_store_self_episode failed")


async def _gen_user_profile() -> None:
    """ユーザー記憶を static/dynamic に再構成して user_profile.md を全量再生成する
    （supermemory のユーザープロファイル相当。検索せず常時注入するための安定文書）"""
    res = await asyncio.to_thread(
        all_user, top_k=200
    )
    mems = format_memories(res)
    if mems == "(まだ記憶はありません)":
        return
    prompt = (
        "以下はユーザーについての長期記憶の一覧です。これをもとに、"
        "ユーザーのプロフィールを2つのセクションに分けて書いてください。\n"
        "## 不変のこと（static）: 属性・好み・性格・人間関係など、めったに変わらないこと\n"
        "## 最近のこと（dynamic）: 最近の関心・進行中の出来事・直近の気分など、今の旬の話題\n"
        "各項目は箇条書きで簡潔に。全体で15行以内。記憶に無いことは書かない。\n\n"
        f"記憶一覧:\n{mems}"
    )
    res2 = await llm.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        reasoning_effort="low",
    )
    text = (res2.choices[0].message.content or "").strip()
    if text:
        USER_PROFILE_PATH.write_text(
            "<!-- companion が自動生成。夜間reflectionで全量更新 -->\n\n" + text + "\n",
            encoding="utf-8",
        )
        log.info("user_profile.md regenerated (%d chars)", len(text))


def _recent_lines(items) -> str:
    return "\n".join(
        f"- {str(it.get('created_at') or '')[:10]} {it.get('memory', '')}"
        for it in items
    )


async def run_reflection(run_date: str | None = None) -> dict:
    """直近の経験メモを反省し、上位の洞察を記憶に戻し、日記を書く。

    洞察候補は生成した時点で reflection_state に確定し、1件ずつ保存済み印を
    付ける。途中で失敗しても同日の再実行は未保存分から再開し、LLMを
    呼び直さない（再生成による重複記憶を防ぐ）。
    """
    today = run_date or datetime.now().strftime("%Y-%m-%d")
    saved = 0
    diary = ""

    insights = get_reflection_insights(today)
    if insights is None:
        items = await self_memories_recent(50)
        candidates = []
        if items:
            prompt = (
                "以下はあなた（コンパニオン）の最近の経験メモです。\n"
                "これらを振り返って、より大きな気づき・感情・関係の変化を2-3件まとめてください。\n"
                "例:「マスターは最近疲れ気味で、私はもっと労わりたい」「最近ゲームの話が多くて楽しい」\n"
                "出力はJSON配列のみ: [{\"text\": \"一文\", \"importance\": 3-5}]\n\n"
                f"経験メモ:\n{_recent_lines(items)}"
            )
            raw = await _ask_json(prompt)
            if raw is None:
                raise RuntimeError("振り返りの洞察生成に失敗しました（再試行します）")
            for it in raw[:3]:
                text = (it.get("text") or "").strip()
                if not text:
                    continue
                try:
                    imp = max(3, min(5, int(it.get("importance", 4))))
                except Exception:
                    imp = 4
                candidates.append(
                    {"text": text, "importance": imp, "saved": False}
                )
        # 空結果でも確定として記録（正当な空と失敗を区別する）
        save_reflection_insights(today, candidates)
        insights = candidates

    for it in insights:
        if it.get("saved") or not it.get("text"):
            continue
        await asyncio.to_thread(
            add_self_note, f"[振り返り] {it['text']}", importance=it["importance"]
        )
        it["saved"] = True
        save_reflection_insights(today, insights)
        saved += 1

    if OBSIDIAN_VAULT:
        items = await self_memories_recent(50)
        lines = _recent_lines(items)
        if lines:
            diary_prompt = (
                "以下はあなた（コンパニオン）の最近の経験メモと、今日の振り返りです。\n"
                "これをもとに、あなたの一人称で短い日記を書いてください。"
                "事実の羅列ではなく、あなたが感じたことや思ったことを中心に。"
                "マスターには見せない私的な日記です。5行以内。\n\n"
                f"経験メモ:\n{lines}\n\n振り返り:\n"
                + "\n".join(f"- {i.get('text','')}" for i in insights)
            )
            res = await llm.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": diary_prompt}],
                reasoning_effort="low",
            )
            diary = res.choices[0].message.content or ""
            d = Path(OBSIDIAN_VAULT) / OBSIDIAN_SYNC_DIR / "日記"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{today}.md").write_text(
                "<!-- companion が自動生成した日記 -->\n\n" + diary + "\n",
                encoding="utf-8",
            )

    if USER_PROFILE:
        try:
            await _gen_user_profile()
        except Exception:
            log.exception("user profile generation failed")

    try:
        demoted = await demote_stale()
        if demoted:
            log.info("demoted %d stale self-memories", demoted)
    except Exception:
        log.exception("demote_stale failed")

    try:
        purged = purge_old_turns(TURNS_RETENTION_DAYS)
        if purged:
            log.info("purged %d processed turns", purged)
    except Exception:
        log.exception("turn purge failed")

    log.info("reflection done: %d insights, diary=%d chars", saved, len(diary))
    return {
        "result": f"振り返り完了（洞察{saved}件）",
        "insights": [i.get("text", "") for i in insights if i.get("text")],
        "diary": diary,
    }
