"""§7.6 Core スキル生成（runtime-hermes-tooling-impl.md R5f）＋失敗レーン。

2つのレーンを持つ。どちらも往復（turns）＋tool trace（turn_tool_calls）
から SKILL.md を起こして skills-inbox に投函し、scan_inbox の通常
レーン（決定的検査→staged→マスター承認）に乗せるので、自動有効化は
しない。frontmatter の origin: self が出自の印。

- 成功レーン: 「tool 呼出しが SKILL_GEN_MIN_CALLS 回以上あり、全て
  status='ok' で終わった往復」から再利用できる手順を起票する
- 失敗レーン: 「SKILL_FAIL_MIN_CALLS 回以上の呼出しがあり、そのうち
  1件以上が非 ok で終わった往復」から、同じ失敗を繰り返さないための
  注意・回避（ガードレール）を起票する。成功レーンでは拾えない材料
  （ExpeL 系の「失敗からの学習」に相当）
- LLM には伏字化済みの抜粋だけを渡し、SKILL.md のレンダは決定的テンプレ
  （LLM に Markdown 全体は書かせない）
- 処理水位は state.json の skill_gen_last_turn_id /
  skill_gen_fail_last_turn_id（レーン別）。生成不可と判断した候補も
  水位を進める（同じ材料を翌日も掘らない）。LLM 障害時は残りを
  次回に持ち越す
"""
import asyncio
import json
import logging
import re
import sqlite3
from pathlib import Path

from . import db, skills
from .config import (
    LLM_MODEL,
    SKILL_FAIL_MAX,
    SKILL_FAIL_MIN_CALLS,
    SKILL_GEN_MAX,
    SKILL_GEN_MIN_CALLS,
    SKILL_GEN_MODEL,
    SKILL_SUMMARY_MODEL,
    SKILLS_INBOX_DIR,
)
from .llm import llm
from .redact import scrub_text

log = logging.getLogger("companion")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WATERMARK_KEY = "skill_gen_last_turn_id"
_WATERMARK_FAIL_KEY = "skill_gen_fail_last_turn_id"


def _model() -> str:
    return SKILL_GEN_MODEL or SKILL_SUMMARY_MODEL or LLM_MODEL


def _candidate_turns(after_id: int, limit: int) -> list:
    """水位より後で、tool 呼出しが MIN_CALLS 回以上かつ全て ok の往復。
    turn_tool_calls の trace を伴わない往復・1件でも失敗のある往復は除く"""
    with db.get_db() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT t.id, t.session_key, t.user_text, t.assistant_text,"
            " t.inner_text, t.ts"
            " FROM turns t"
            " WHERE t.id > ? AND EXISTS ("
            "   SELECT 1 FROM turn_tool_calls c WHERE c.turn_id = t.id"
            "   GROUP BY c.turn_id"
            "   HAVING COUNT(*) >= ?"
            "   AND SUM(CASE WHEN c.status = 'ok' THEN 0 ELSE 1 END) = 0"
            " ) ORDER BY t.id LIMIT ?",
            (after_id, SKILL_GEN_MIN_CALLS, limit),
        ).fetchall()


def _failure_candidate_turns(after_id: int, limit: int) -> list:
    """水位より後で、tool 呼出しが FAIL_MIN_CALLS 回以上あり、そのうち
    1件以上が非 ok（error/unknown/aborted 等）で終わった往復。
    成功レーン（全 ok 必須）では拾えない材料をこちらで見る"""
    with db.get_db() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT t.id, t.session_key, t.user_text, t.assistant_text,"
            " t.inner_text, t.ts"
            " FROM turns t"
            " WHERE t.id > ? AND EXISTS ("
            "   SELECT 1 FROM turn_tool_calls c WHERE c.turn_id = t.id"
            "   GROUP BY c.turn_id"
            "   HAVING COUNT(*) >= ?"
            "   AND SUM(CASE WHEN c.status = 'ok' THEN 0 ELSE 1 END) >= 1"
            " ) ORDER BY t.id LIMIT ?",
            (after_id, SKILL_FAIL_MIN_CALLS, limit),
        ).fetchall()


def _turn_calls(turn_id: int) -> list:
    with db.get_db() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT seq, tool, origin, status, args_json, result_excerpt"
            " FROM turn_tool_calls WHERE turn_id = ? ORDER BY seq, id",
            (turn_id,),
        ).fetchall()


def _trace_block(calls) -> str:
    lines = []
    for i, c in enumerate(calls, 1):
        args = scrub_text(c["args_json"], cap=200)
        result = scrub_text(c["result_excerpt"], cap=120)
        lines.append(
            f"{i}. {c['tool']} [{c['origin']}] {args}"
            f" → {c['status']}: {result}"
        )
    return "\n".join(lines)


def _existing_index() -> str:
    rows = db.skill_list()
    if not rows:
        return "（なし）"
    return "\n".join(
        f"- {r['name']} [{r['status']}]: {r['description']}" for r in rows
    )


_PROMPT = """\
あなたはコンパニオンの技能起票係です。以下は会話で実際に成功した往復と、
その中で呼ばれたツールの記録です。会話固有の事実を排した「再利用できる
手順」があれば SKILL.md の素材を JSON で返し、無ければ {{"skip": true}}
だけ返してください。

すでに登録済みの技能（内容が被るなら skip する）:
{index}

往復 #{tid} ({ts}):
ユーザー: {user}
コンパニオン: {assistant}
{inner}
ツール呼出し記録:
{trace}

返す JSON（skip しない場合）:
{{"name": "英字kebab-case", "description": "一文",
  "scopes": "カンマ区切り", "requires_tools": "使うtool名カンマ区切り",
  "when": "どんなときに使うか一文",
  "steps": ["手順1", "手順2", "…"], "cautions": ["注意点"]}}

条件:
- 手順は「何をどう呼ぶか」の形で。人名・日付・URL など会話固有の値は
  手順に残さず、どう調べて決めるかを書く
- 秘密っぽい値（トークン・パスワード・個人情報）は一切書かない
- 単発の挨拶・雑談・事実確認だけの往復は skip
- name は既存技能と重複しないこと
"""


_FAIL_PROMPT = """\
あなたはコンパニオンの技能起票係です。以下は会話で tool 呼出しが
失敗した往復と、その記録です。この失敗から「同じ作業で気をつける
べきこと」が再利用できる形で残せるなら SKILL.md の素材を JSON で
返し、無ければ {{"skip": true}} だけ返してください。

すでに登録済みの技能（内容が被るなら skip する）:
{index}

往復 #{tid} ({ts}):
ユーザー: {user}
コンパニオン: {assistant}
{inner}
ツール呼出し記録（失敗を含む）:
{trace}

返す JSON（skip しない場合）:
{{"name": "英字kebab-case", "description": "一文",
  "scopes": "カンマ区切り", "requires_tools": "関係するtool名カンマ区切り",
  "when": "どんな作業・状況で起きた失敗か一文",
  "cautions": ["注意・やってはいけないこと"],
  "steps": ["確認・回復の手順（あれば）"]}}

条件:
- cautions は「何を確認・回避すべきか」の形で1〜6件
- 単発の通信エラーやタイムアウトだけで学べることがなければ skip
- 人名・日付・URL など会話固有の値は書かず、どう確認するかを書く
- 秘密っぽい値（トークン・パスワード・個人情報）は一切書かない
- name は既存技能と重複しないこと
"""


def _render(data: dict, turn) -> str | None:
    """LLM の JSON を決定的に SKILL.md へ。要件を満たさないとき None"""
    name = str(data.get("name") or "").strip()
    desc = str(data.get("description") or "").strip()
    when = str(data.get("when") or "").strip()
    steps = [str(s).strip() for s in (data.get("steps") or [])]
    steps = [s for s in steps if s]
    cautions = [str(s).strip() for s in (data.get("cautions") or [])]
    cautions = [s for s in cautions if s]
    scopes = skills._parse_scopes(str(data.get("scopes") or ""))
    requires = skills._parse_scopes(str(data.get("requires_tools") or ""))
    if not (_NAME_RE.match(name) and desc and when and 2 <= len(steps) <= 10):
        return None
    parts = [
        "---",
        f"name: {name}",
        f"description: {desc}",
        f"scopes: {scopes}",
        f"requires_tools: {requires}",
        "origin: self",
        "---",
        "",
        f"# {name}",
        "",
        "## いつ使うか",
        when,
        "",
        "## 手順",
    ]
    parts += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    if cautions:
        parts += ["", "## 注意"] + [f"- {c}" for c in cautions[:5]]
    # provenance: どの往復から起票したか（内容には出さない写しの印）
    parts += [
        "",
        f"<!-- companion skill_gen: source turn #{turn['id']}"
        f" ({turn['ts'] or '?'}) -->",
        "",
    ]
    return "\n".join(parts)


def _render_guardrail(data: dict, turn) -> str | None:
    """失敗レーンの JSON をガードレール形 SKILL.md へ。要件を満たさない
    とき None。手順ではなく「注意・やってはいけないこと」が本体なので
    cautions が必須、steps（確認・回復）は任意"""
    name = str(data.get("name") or "").strip()
    desc = str(data.get("description") or "").strip()
    when = str(data.get("when") or "").strip()
    cautions = [str(s).strip() for s in (data.get("cautions") or [])]
    cautions = [s for s in cautions if s]
    steps = [str(s).strip() for s in (data.get("steps") or [])]
    steps = [s for s in steps if s]
    scopes = skills._parse_scopes(str(data.get("scopes") or ""))
    requires = skills._parse_scopes(str(data.get("requires_tools") or ""))
    if not (_NAME_RE.match(name) and desc and when
            and 1 <= len(cautions) <= 8 and len(steps) <= 8):
        return None
    parts = [
        "---",
        f"name: {name}",
        f"description: {desc}",
        f"scopes: {scopes}",
        f"requires_tools: {requires}",
        "origin: self",
        "---",
        "",
        f"# {name}",
        "",
        "## どんなときに起きるか",
        when,
        "",
        "## 注意・やってはいけないこと",
    ]
    parts += [f"- {c}" for c in cautions]
    if steps:
        parts += ["", "## 確認・回復の手順"]
        parts += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    # provenance: どの往復から起票したか（内容には出さない写しの印）
    parts += [
        "",
        f"<!-- companion skill_gen fail-lane: source turn #{turn['id']}"
        f" ({turn['ts'] or '?'}) -->",
        "",
    ]
    return "\n".join(parts)


def _inbox_dest(name: str) -> Path:
    dest = SKILLS_INBOX_DIR / name
    if not dest.exists():
        return dest
    from datetime import datetime
    for i in range(100):
        cand = SKILLS_INBOX_DIR / f"{name}-{datetime.now():%Y%m%d}-{i}"
        if not cand.exists():
            return cand
    return SKILLS_INBOX_DIR / f"{name}-{datetime.now():%Y%m%d%H%M%S}"


async def _ask_obj(prompt: str):
    """LLM の JSON オブジェクト応答を parse して返す。
    JSON が取れない・parse 失敗は None（skip 扱い。LLM 障害は raise）"""
    res = await llm.chat.completions.create(
        model=_model(),
        messages=[{"role": "user", "content": prompt}],
        reasoning_effort="minimal",
    )
    text = res.choices[0].message.content or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        log.warning("skill_gen: json が取れない（skip 扱い）: %s", text[:120])
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        log.warning("skill_gen: json parse 失敗（skip 扱い）")
        return None


async def _file_candidate(name: str, md: str, turn,
                          note_tmpl: str) -> str | None:
    """レンダ済み SKILL.md を skills-inbox に投函して自己メモを残す。
    投函した技能名。同名の既存技能があるときは何もせず None。
    note_tmpl の {name} には投函先の技能名が入る"""
    if db.skill_get(name):
        log.info("skill_gen: %s は既存技能と同名なので skip", name)
        return None
    dest = _inbox_dest(name)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(md, encoding="utf-8")
    log.info("skill_gen: turn #%s -> %s を inbox に投函", turn["id"], dest.name)
    try:
        from .memory import add_self_note
        await asyncio.to_thread(
            add_self_note, note_tmpl.format(name=dest.name), importance=2)
    except Exception:
        log.exception("skill_gen self-note failed")
    return dest.name


def _lane_prompt(template: str, turn, calls) -> str:
    return template.format(
        index=_existing_index(), tid=turn["id"], ts=turn["ts"] or "",
        user=scrub_text(turn["user_text"], cap=400),
        assistant=scrub_text(turn["assistant_text"], cap=400),
        inner=(
            f"（心の中）: {scrub_text(turn['inner_text'], cap=200)}"
            if turn["inner_text"] else ""
        ),
        trace=_trace_block(calls),
    )


async def _gen_one(turn, calls) -> str | None:
    """成功レーンの1往復を処理。投函した技能名、skip/失敗なら None。
    LLM 障害は raise"""
    data = await _ask_obj(_lane_prompt(_PROMPT, turn, calls))
    if not isinstance(data, dict) or data.get("skip"):
        return None
    md = _render(data, turn)
    if md is None:
        log.warning("skill_gen: 生成 JSON が要件を満たさない: %s", data)
        return None
    return await _file_candidate(
        str(data["name"]).strip(), md, turn,
        f"往復 #{turn['id']} の成功手順から技能候補「{{name}}」を"
        "書き起こして inbox に置いた（要承認）",
    )


async def _gen_guardrail(turn, calls) -> str | None:
    """失敗レーンの1往復を処理。投函した技能名、skip/失敗なら None。
    LLM 障害は raise"""
    data = await _ask_obj(_lane_prompt(_FAIL_PROMPT, turn, calls))
    if not isinstance(data, dict) or data.get("skip"):
        return None
    md = _render_guardrail(data, turn)
    if md is None:
        log.warning("skill_gen: ガードレール JSON が要件を満たさない: %s",
                    data)
        return None
    return await _file_candidate(
        str(data["name"]).strip(), md, turn,
        f"往復 #{turn['id']} の失敗から注意スキル候補「{{name}}」を"
        "書き起こして inbox に置いた（要承認）",
    )


async def _run_lane(watermark_key: str, find_candidates, gen_one) -> int:
    """1レーン分を水位から走査。処理した候補数を返す（0=材料なし）。
    LLM 障害は途中で打ち切り、残りは次回に持ち越す（水位は進めない）"""
    state = db.load_state()
    watermark = int(state.get(watermark_key) or 0)
    candidates = find_candidates(watermark)
    if not candidates:
        return 0
    made = 0
    done_mark = watermark
    for turn in candidates:
        calls = _turn_calls(turn["id"])
        try:
            produced = await gen_one(turn, calls)
        except Exception:
            # LLM 障害など。残りの候補は次回に持ち越す（水位は進めない）
            log.exception("skill_gen failed at turn #%s", turn["id"])
            break
        done_mark = turn["id"]
        made += bool(produced)
    db.save_state(**{watermark_key: done_mark})
    log.info(
        "skill_gen[%s]: scanned %d turn(s), produced %d"
        " (watermark %s -> %s)",
        watermark_key, len(candidates), made, watermark, done_mark,
    )
    return len(candidates)


async def run() -> str:
    """日次エントリ。scheduler の _run_daily から呼ぶ（"done"/"skipped"）。
    成功→技能・失敗→ガードレールの2レーンを順に回す"""
    scanned = await _run_lane(
        _WATERMARK_KEY,
        lambda wm: _candidate_turns(wm, SKILL_GEN_MAX),
        _gen_one,
    )
    scanned += await _run_lane(
        _WATERMARK_FAIL_KEY,
        lambda wm: _failure_candidate_turns(wm, SKILL_FAIL_MAX),
        _gen_guardrail,
    )
    return "done" if scanned else "skipped"
