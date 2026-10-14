"""kernel.envelope —— intake envelope 契約の純粋規則（FA §4.1・write-path §2）

Phase1（同期 1tx ack）の検査を担う純粋関数群:
  envelope schema 検証 → 同期 redaction（secret マスク＋敵対 Unicode Cf strip
  ＋NFC）→ declared-class 照合 → dedup 鍵の計算。

dedup 錨（SD-16/SD-65・transcript-ingress §4.1）:
  envelope_key = hash(adapter_id, stream_id, span_start, span_end,
                      payload_digest)          … envelope 保存の一意鍵
  ingress_key  = hash(adapter_id, stream_id, native_part_id,
                      transform_version)       … 正規化 event の冪等錨
envelope_id は producer 採番で再包装で変わりうるため dedup に使わない
（traceability のみ）。adapter_id が鍵成分なので adapter 跨ぎは衝突しない
（F-14: 同一論理ソースは1 adapter 系統の運用規約と合わせて担保）。

declared-class 照合: part.class が coverage.classes 宣言外なら
`undeclared_class` flag（隔離 fail-closed。adapter 申告を強制規則化）。
"""

import re
import unicodedata
from . import canon

ENVELOPE_FORMAT = "pcore.envelope.v1"

# ------------------------------------------------------------- dedup keys

def envelope_key(adapter_id: str, stream_id: str, span_start,
                 span_end, payload_digest: str) -> str:
    return canon.hash_tuple("pcore.envelope_key.v1",
                            adapter_id, stream_id, span_start, span_end,
                            payload_digest)


def ingress_key(adapter_id: str, stream_id: str, native_part_id,
                transform_version: str) -> str:
    """正規化 event の冪等錨。native_part_id 不在時は (span_start, ordinal)
    の座標合成文字列を呼び出し側が渡す。transform_version を含むため
    renormalize の再発行は別キーの正当な新 event になる（SD-46）。"""
    return canon.hash_tuple("pcore.ingress_key.v1",
                            adapter_id, stream_id, native_part_id,
                            transform_version)


def payload_digest(parts) -> str:
    """envelope の内容 digest（parts 配列の正準 hash）。"""
    return canon.hash_obj("pcore.envelope_payload.v1", parts)


# ------------------------------------------------------------- validation

REQUIRED_ENVELOPE_FIELDS = (
    "envelope_id", "adapter_id", "format_version", "stream_id", "parts",
    "span",
)

PAYLOAD_MODES = frozenset({"inline", "locator"})

# 既知の part.class 語彙（open-world・declared 照合は coverage.classes が担う）
PART_CLASSES = frozenset({
    "conversation", "tool", "job", "approval", "channel_meta", "observation",
})

# part の origin 語彙（echo 防御の補助信号。本線は content digest 照合）
PART_ORIGINS = frozenset({
    "user", "assistant", "tool", "system", "injected",
    "runtime_observation", "imported",
})


def validate_envelope(env) -> list:
    """envelope schema 検査。違反の文字列リスト（空=合格）。"""
    errs = []
    if not isinstance(env, dict):
        return ["envelope must be dict"]
    for f in REQUIRED_ENVELOPE_FIELDS:
        if not env.get(f):
            errs.append(f"envelope.{f} missing")
    if env.get("format_version") != ENVELOPE_FORMAT:
        errs.append(
            f"format_version must be {ENVELOPE_FORMAT}, got"
            f" {env.get('format_version')!r}")
    span = env.get("span")
    if span is not None:
        if not isinstance(span, dict) \
                or "start_cursor" not in span or "end_cursor" not in span:
            errs.append("span must be {start_cursor, end_cursor}")
    mode = env.get("payload_mode", "inline")
    if mode not in PAYLOAD_MODES:
        errs.append(f"payload_mode unknown: {mode!r}")
    pc = env.get("part_count")
    if pc is not None and isinstance(env.get("parts"), list) \
            and pc != len(env["parts"]):
        errs.append(
            f"part_count {pc} != len(parts) {len(env['parts'])}")
    if mode == "inline" and env.get("payload_inline") is None \
            and not env.get("parts"):
        errs.append("inline mode requires payload_inline or parts")
    if mode == "locator" and not isinstance(env.get("locator"), dict):
        errs.append("locator mode requires locator{uri,digest}")
    cov = env.get("coverage")
    if cov is not None:
        if not isinstance(cov, dict) or not isinstance(
                cov.get("classes", []), list):
            errs.append("coverage.classes must be a list")
    parts = env.get("parts")
    if parts is not None:
        if not isinstance(parts, list):
            errs.append("parts must be a list")
        else:
            for i, p in enumerate(parts):
                if not isinstance(p, dict):
                    errs.append(f"parts[{i}] must be dict")
                    continue
                if not p.get("part_id") and not p.get("body"):
                    errs.append(f"parts[{i}] has neither part_id nor body")
                o = p.get("origin")
                if o is not None and o not in PART_ORIGINS:
                    errs.append(f"parts[{i}].origin unknown: {o!r}")
    return errs


# ------------------------------------------------------------- redaction
# ingress 同期 redaction（write-path §2.1）:
#   - secret パターンを `***` にマスクし redacted{fields,reason} を刻む
#     （マスク前の値はどこにも書かない）
#   - 敵対 Unicode（bidi override・zero-width・Tag block・Hangul filler 等の
#     Cf 系）は strip＋flag（FA §4.1 の "Cf strip" 側を採用。write-path の
#     「拒否」表記との揺れは phase8-impl.md に記録）
#   - NFC 正規化を ingress 検査語彙として適用

_SECRET_PATTERNS = [
    (re.compile(
        r"(?i)\b([A-Za-z_][A-Za-z0-9_]*"
        r"(?:KEY|TOKEN|SECRET|PASS|PASSWORD|PWD|CREDENTIAL|AUTH)"
        r"[A-Za-z0-9_]*)\s*[=:]\s*(\"[^\"\n]*\"|'[^'\n]*'|[^\s&|;]+)"),
     "kv_secret"),
    (re.compile(r"(?i)\bBearer\s+\S+"), "bearer"),
    (re.compile(r"(://)[^/@\s:]+:[^/@\s]+@"), "url_userinfo"),
    (re.compile(r"[A-Za-z0-9_\-][A-Za-z0-9._\-+/=]{23,}"), "long_token"),
]

# 敵対 Unicode（Cf 系のうち意味を歪めうるもの）
_HOSTILE_RANGES = [
    (0x202A, 0x202E),   # bidi embedding/override
    (0x2066, 0x2069),   # bidi isolate
    (0x200B, 0x200F),   # zero-width space/joiner/non-joiner・LRM/RLM
    (0xFEFF,),          # BOM / zero-width no-break
    (0xE0000, 0xE007F), # Tag block
    (0x115F, 0x1160),   # Hangul jamo filler
    (0xFFA0,),          # Hangul halfwidth filler
    (0x3164,),          # Hangul compatibility filler
]


def _is_hostile(ch: str) -> bool:
    cp = ord(ch)
    return any(a <= cp <= b for r in _HOSTILE_RANGES
               for a, b in [(r[0], r[-1])])


def sanitize_text(text: str):
    """(cleaned_text, flags)。Cf strip＋NFC。hostile 検出は flag に残る。"""
    flags = []
    hostile = [ch for ch in text if _is_hostile(ch)]
    if hostile:
        flags.append("hostile_unicode")
        text = "".join(ch for ch in text if not _is_hostile(ch))
    nfc = unicodedata.normalize("NFC", text)
    if nfc != text:
        flags.append("nfc_normalized")
    return nfc, flags


def redact_part(part: dict):
    """part の body 等テキストフィールドに redaction＋sanitize を適用。

    戻り値: (new_part, redaction_record_or_None, flags)。
    マスク前の値は返さない・残さない。"""
    if not isinstance(part, dict):
        return part, None, []
    p = dict(part)
    flags = []
    redacted_fields = []
    for field in ("body", "text", "content"):
        v = p.get(field)
        if not isinstance(v, str):
            continue
        v, fl = sanitize_text(v)
        flags.extend(fl)
        for rx, kind in _SECRET_PATTERNS:
            if kind == "kv_secret":
                v, n = rx.subn(lambda m: f"{m.group(1)}=***", v)
            elif kind == "bearer":
                v, n = rx.subn("Bearer ***", v)
            elif kind == "url_userinfo":
                v, n = rx.subn(r"\1***:***@", v)
            else:
                v, n = rx.subn("***", v)
            if n:
                redacted_fields.append({"field": field, "reason": kind})
        p[field] = v
    rec = {"fields": redacted_fields} if redacted_fields else None
    return p, rec, flags


def check_declared_classes(parts, coverage_classes) -> list:
    """declared-class 照合。宣言外 class の part index リストを返す。"""
    declared = set(coverage_classes or [])
    out = []
    for i, p in enumerate(parts or []):
        cls = (p or {}).get("class") or "conversation"
        if cls not in declared:
            out.append(i)
    return out
