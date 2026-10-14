"""adapters.llm_extract —— 本格 LLM 抽出器（C-1、write-path §3）

dualrun.verbatim_extractor の本格版。message event 単位で、同じ turn の
response も材料にして LLM が user scope の型付き記憶（fact/preference/
episode/relationship/intention 等）を抽出する。

設計上の約束:

- SD-77 自己完結化: 指示語は具体対象へ、相対時刻は絶対日付へ展開
  させる（残渣は admission の concreteness 検査が pending にする）
- basis_events は message event（＋ペア response event）の実IDのみ。
  LLM の出力を信じず、こちらで機械的に付ける
- producer="extraction"（RULE_PRODUCERS 外 —— 証拠資格・concreteness・
  review.required すべての検査対象。importance 4+ は pending に流れる
  設計通りの動き）
- message 以外の event（response/inner/tool_result）は [] を返し、
  claim は no_lesson で正しく終端する
- LLM 障害・JSON 不成立は raise —— Extraction の試行回数/poison 機構
  が処理する（ここで握り潰さない）
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

from ..kernel import canon, vocab

log = logging.getLogger("companion")

EXTRACT_VERSION = "extract-llm-v2"

_TYPE_GUIDE = {
    "fact": "事実（住所・職業・所有物など）",
    "preference": "好み・嗜好・嫌いなもの",
    "episode": "出来事・体験（いつ何があったか）",
    "relationship": "人間関係（誰が誰の何か・相手との関係）",
    "intention": "意図・予定・やりたいこと",
    "procedure": "手順・習慣・やり方",
    "insight": "気づき・価値観・考え方",
}

_PROMPT = """あなたはコンパニオンの長期記憶係です。下の「マスターの発言」とそれへの「返答」から、マスターについて長く覚えておく価値のある事実だけを抽出してください。

ルール:
- 各記憶はその文だけで意味が通る完全な一文にする
- 「彼」「それ」「あれ」「この前」などの指示語・曖昧語は、分かる範囲で実名や具体対象に展開する
- 「明日」「先週」「来年」などの相対時刻は、発言日時 {date} を基準に絶対日付（YYYY-MM-DD、曖昧なら YYYY-MM や YYYY 年）に展開する
- 発言と返答に明示されていないことは書かない。推測で補わない
- 挨拶・相槌・雑談など覚える価値のない発言からは何も抽出しない
- 個別の記憶として最大 {max_facts} 件まで

importance の目安: 1=ささいな話題、2=軽い好み、3=予定・出来事・確かな好み、4-5=人生や関係の節目（4-5 は人間の承認待ちになるので本当に大事なものだけ）

出力は JSON 配列のみ（説明文・コードフェンスは不要）:
[{{"text": "一文", "type": "{types}", "importance": 1,
   "occurred_at": "その出来事・予定・期限が実際に起きた/来る日付",
   "entities": ["その記憶に出てくる人・場所・物の名前"]}}]
type は: {type_guide}
occurred_at は YYYY-MM-DD（月・年だけ分かるなら YYYY-MM・YYYY）。
明示日付か、発言日時 {date} を基準に解決できる相対表現だけを書く。
「最近」「この前」など曖昧で解決できない時、日付の無い一般的事実
（好み・属性など）には null。日付を創作しない・今日の日付を使い回さない。
entities は固有名詞中心に0〜4個（無ければ省略可）。
抽出するものがなければ [] とだけ返す。

発言日時: {date}
マスターの発言: {user}
コンパニオンの返答: {assistant}"""

_MAX_BODY = 2000
_MAX_RESP = 1500
_DEF_MAX_FACTS = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_importance(v) -> int:
    try:
        return max(1, min(5, int(v)))
    except (TypeError, ValueError):
        return 2


_OCC_RE = re.compile(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?(?:[T ].*)?$")


def _norm_occurred(v) -> str | None:
    """LLM が出力した出来事日付を canonical 時刻（期間の先頭）へ正規化。

    "YYYY"→その年の1/1、"YYYY-MM"→その月の1日、"YYYY-MM-DD"/ISO→当日0時。
    粒度が荒い値は期間先頭に畳むことで、自身の月・年の時間窓に必ず
    含まれる（lexicographic 比較で落ちない）。解釈不能・null は None。
    """
    s = str(v or "").strip()
    if not s or s.lower() in ("null", "none"):
        return None
    m = _OCC_RE.match(s)
    if not m:
        return None
    try:
        dt = datetime(int(m[1]), int(m[2] or 1), int(m[3] or 1),
                      tzinfo=timezone.utc)
    except ValueError:
        return None
    return canon.canon_ts(dt)


def _parse_items(raw: str):
    """LLM 出力 → [item, ...]（先頭の JSON 配列を拾う）。壊れていれば
    例外を投げて呼び出し側（poison 機構）に委ねる。"""
    text = (raw or "").strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise ValueError("llm extract: no json array in output")
    items = json.loads(m.group(0))
    if not isinstance(items, list):
        raise ValueError("llm extract: json root is not array")
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text_i = str(it.get("text") or "").strip()
        if text_i:
            ents = it.get("entities") or []
            if not isinstance(ents, list):
                ents = []
            out.append({"text": text_i,
                        "type": str(it.get("type") or "fact"),
                        "importance": _clamp_importance(
                            it.get("importance")),
                        "occurred_at": _norm_occurred(
                            it.get("occurred_at")),
                        "entities": [str(e).strip() for e in ents
                                     if str(e).strip()][:6]})
    return out


class LLMExtractor:
    """extractor callable: ev desc -> proposal の list。

    writer を受けるのは「同 turn の response ペア」を events 表から
    拾うため（lane worker thread 内で実行される前提 = writer 所有
    スレッドと同一）。
    """

    def __init__(self, writer, *, model=None, client=None, ask_fn=None,
                 clock=None, max_facts=None):
        self.writer = writer
        self.model = model or os.getenv("EXTRACT_MODEL") or _llm_model()
        self._client = client
        self._ask_fn = ask_fn
        self._clock = clock or _now_iso
        self._max_facts = max_facts if max_facts is not None else int(
            os.getenv("EXTRACT_MAX_FACTS", str(_DEF_MAX_FACTS)))

    # ------------------------------------------------------------- LLM
    def _ask(self, prompt: str) -> str:
        if self._ask_fn is not None:
            return self._ask_fn(prompt)
        if self._client is None:
            from openai import OpenAI
            from ..config import LLM_BASE_URL
            self._client = OpenAI(base_url=LLM_BASE_URL, api_key="unused",
                                  timeout=60)
        res = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2)
        return res.choices[0].message.content or ""

    # ------------------------------------------------------- pair 解決
    def _pair_response(self, ev: dict):
        """message event と同じ turn の response event を events 表から拾う。
        turn_id が無い/ペア無しなら None。"""
        try:
            row = self.writer._conn.execute(
                "SELECT turn_id FROM events WHERE event_id=?",
                (ev.get("event_id"),)).fetchone()
            if not row or not row["turn_id"]:
                return None
            return self.writer._conn.execute(
                "SELECT event_id, payload_json FROM events"
                " WHERE turn_id=? AND event_type='response'"
                " ORDER BY seq LIMIT 1", (row["turn_id"],)).fetchone()
        except Exception:
            log.exception("llm extract: pair response lookup failed")
            return None

    # ----------------------------------------------------------- entry
    def __call__(self, ev: dict) -> list:
        if ev.get("event_type") != "message":
            return []
        body = str((ev.get("payload") or {}).get("body") or "").strip()
        if not body:
            return []

        basis = [{"event_id": ev["event_id"]}]
        resp_body = ""
        pair = self._pair_response(ev)
        if pair is not None:
            basis.append({"event_id": pair["event_id"]})
            try:
                resp_body = str((canon.loads(pair["payload_json"]) or {})
                                .get("body") or "")
            except Exception:
                resp_body = ""

        occurred = str(ev.get("occurred_at") or "")
        prompt = _PROMPT.format(
            date=occurred[:10] or "不明",
            user=body[:_MAX_BODY],
            assistant=(resp_body[:_MAX_RESP] or "（返答なし）"),
            max_facts=self._max_facts,
            types="|".join(sorted(vocab.MEMORY_TYPES)),
            type_guide="、".join(f"{k}={v}" for k, v in _TYPE_GUIDE.items()))
        items = _parse_items(self._ask(prompt))
        return [self._proposal(it, ev, basis)
                for it in items[:self._max_facts]]

    # --------------------------------------------------------- proposal
    def _proposal(self, it: dict, ev: dict, basis: list) -> dict:
        text = it["text"]
        mtype = it["type"] if it["type"] in vocab.MEMORY_TYPES else "fact"
        now = self._clock()
        rid = "mem_x_" + canon.hash_tuple(
            "pcore.extract_mem.v1", ev["event_id"], text)[:20]
        return {
            "record_family": "memory",
            "target_id": None,
            "new_state": {
                "record_id": rid,
                "record_family": "memory",
                "scope": "user",
                "status": "active",
                "record_version": 1,
                "memory_type": mtype,
                "content": text,
                "content_hash": canon.content_hash(text),
                "sensitivity": "normal",
                "importance": it["importance"],
                "retrieval_strength": 0.5,
                "tier": "hot",
                "pinned": False,
                # 出来事の時刻（時間腕・decay の起点）
                "valid_from": ev.get("occurred_at") or now,
                "created_at": now,
                "updated_at": now,
                # 記述された出来事自体の時刻（語った時刻=valid_from と
                # 区別する event-time。「先週の話」を今日記録しても
                # 先週の時間窓に乗る）
                **({"occurred_at": it["occurred_at"]}
                   if it.get("occurred_at") else {}),
                **({"entities": it["entities"]} if it["entities"] else {}),
            },
            "basis_events": basis,
            "basis_seq": ev.get("seq"),
            "generated_by": "extraction",
            "generator": {"method": "llm", "model": self.model,
                          "version": EXTRACT_VERSION},
            "producer": "extraction",
        }


def _llm_model() -> str:
    try:
        from ..config import LLM_MODEL
        return LLM_MODEL
    except Exception:
        return "unknown"


def extractor_kind() -> str:
    """設定値: verbatim（既定・影運用互換）or llm。"""
    return os.getenv("CANONICAL_EXTRACTOR", "verbatim").strip().lower()


def default_extractor(writer):
    """CANONICAL_EXTRACTOR に応じた既定 extractor を返す。

    llm 指定でも writer が無ければ verbatim に倒す（pair 解決に events
    表が要るため）。"""
    if extractor_kind() == "llm" and writer is not None:
        return LLMExtractor(writer)
    from .dualrun import verbatim_extractor
    return verbatim_extractor
