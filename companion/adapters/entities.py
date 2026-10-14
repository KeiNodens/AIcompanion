"""adapters.entities —— entity 腕の索引材料（C-4、read-path §4 補強）

record から「実体（entity）」候補を集め、derived の entities 索引に
載せる。供給時はクエリ中に現れた entity → その entity を持つ記憶を
候補化し、edges 索引で1ホップ先まで展開する。

entity の二系統:

- 明示: new_state["entities"]（LLM 抽出器が付ける。漢字の人名等も
  拾える本線）
- 決定的表面抽出: LLM を通らない記憶（verbatim episode 等）からの
  補助索引。カタカナ連続・「」引用・大文字 ASCII 語を拾う
  （索引は再生成可能な写しなので、精度は粗くてよい）
"""

import re

from ..kernel import canon

# 表面抽出パターン（粗くてよい —— 索引・再生成可能な写し用）
_KATAKANA = re.compile(r"[ァ-ヶー]{2,}")          # アリス・ラーメン等
_QUOTED = re.compile(r"「([^」]{1,24})」")         # 「作品名」「呼称」
_ASCII = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9]{1,}")

# 索引に載せない語（機能語・一般語の表面形を弾く最低限のフィルタ）
_STOP = frozenset({
    "これ", "それ", "あれ", "どれ", "ここ", "そこ", "あそこ",
    "なに", "だれ", "いつ", "いる", "ある", "なる", "する",
})

_MAX_PER_RECORD = 12


def surface_entities(text: str) -> list:
    """本文から決定的に拾える entity 表層候補（norm_text 済み・dedup）。"""
    t = text or ""
    found = list(_KATAKANA.findall(t)) + _QUOTED.findall(t) \
        + _ASCII.findall(t)
    out, seen = [], set()
    for e in found:
        n = canon.norm_text(e)
        if not n or n in _STOP or len(n) < 2 or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out[:_MAX_PER_RECORD]


def record_entities(st: dict) -> list:
    """record の entity 集合: 明示 entities フィールド + 表面抽出。

    明示フィールド（LLM 抽出由来）を優先し、表面抽出で補完する。
    戻り値は norm_text 済みの dedup リスト。"""
    out, seen = [], set()

    def add(name):
        n = canon.norm_text(str(name or ""))
        if n and n not in _STOP and len(n) >= 2 and n not in seen:
            seen.add(n)
            out.append(n)

    for e in st.get("entities") or []:
        add(e)
    for e in surface_entities(st.get("content") or st.get("text") or ""):
        add(e)
    # subject（{kind,name} の主語）があれば entity としても索引に乗せる
    subj = st.get("subject")
    if isinstance(subj, dict) and subj.get("name"):
        add(subj["name"])
    return out[:_MAX_PER_RECORD]


def query_entities(query: str, known: list) -> list:
    """クエリ中に現れる entity_norm を返す（部分一致・長い順）。"""
    q = canon.norm_text(query or "")
    hits = [e for e in known if e and e in q]
    return sorted(set(hits), key=len, reverse=True)
