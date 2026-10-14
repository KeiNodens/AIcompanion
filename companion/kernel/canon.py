"""kernel.canon —— canonical codec（直列化・正規化・hash の唯一入口。SD-01/SD-02）

canonical-schema §7.1 の契約をここに集約する。byte 同一性に依存する全機構
（row_hash・digest・content_hash・import_key・pack manifest）はこのモジュールの
規則に従う。規則を変更するときは SERIALIZER_VERSION を上げ、golden-byte
fixture（tests/test_kernel_canon.py）と policy_change 刻印を伴わせる。

受理値域（§7.1）:
  - None / bool / int / float（有限のみ）/ str / list / tuple→list / dict（str キー）
  - tz-aware datetime → UTC・マイクロ秒固定 ISO8601 文字列
  - NaN・±Infinity は拒否、-0.0 → 0.0 に正規化
  - 上記以外の型（bytes・set・オブジェクト等）は拒否
直列化: JSON・sort_keys・separators=(",",":")・ensure_ascii=False・UTF-8・NFC・LF
hash:   sha256(tag ‖ b"\\x00" ‖ canonical_bytes) の tag domain 分離を統一
複合キー: タプルは list として正準直列化して hash（区切り escaping は JSON が担う）

注: 直列化の正規化は NFC（表現の canon化のみで内容を変えない）。
照合用正規化 norm_text は NFKC+casefold+空白畳み（意味等価を畳む別層）。
"""

import hashlib
import json
import math
import unicodedata
from datetime import datetime, timezone

SERIALIZER_VERSION = "canon-json-v1"

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class CanonicalError(ValueError):
    """受理値域外の値・直列化不能な入力に対するエラー。"""


# ---------------------------------------------------------------- timestamps

def canon_ts(dt: datetime) -> str:
    """tz-aware datetime → "YYYY-MM-DDTHH:MM:SS.ffffffZ"（UTC・µs 固定）。"""
    if not isinstance(dt, datetime):
        raise CanonicalError(f"canon_ts expects datetime, got {type(dt).__name__}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise CanonicalError("naive datetime is not canonical (aware UTC required)")
    return dt.astimezone(timezone.utc).strftime(_TS_FORMAT)


def parse_ts(s: str) -> datetime:
    """canon_ts の逆。固定形式以外は ValueError。"""
    if not isinstance(s, str) or not s.endswith("Z"):
        raise CanonicalError(f"not a canonical timestamp: {s!r}")
    return datetime.strptime(s, _TS_FORMAT).replace(tzinfo=timezone.utc)


def is_ts(s) -> bool:
    try:
        parse_ts(s)
        return True
    except (CanonicalError, ValueError):
        return False


# ------------------------------------------------------------- normalization

def _normalize(v):
    """受理値域への正規化。受理できない型・値は CanonicalError。"""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            raise CanonicalError(f"non-finite float rejected: {v!r}")
        return 0.0 if v == 0.0 else v  # -0.0 → 0.0
    if isinstance(v, str):
        return unicodedata.normalize("NFC", v)
    if isinstance(v, datetime):
        return canon_ts(v)
    if isinstance(v, (list, tuple)):
        return [_normalize(x) for x in v]
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            if not isinstance(k, str):
                raise CanonicalError(f"non-str dict key rejected: {k!r}")
            nk = unicodedata.normalize("NFC", k)
            if nk in out:
                raise CanonicalError(f"dict key collides after NFC: {k!r}")
            out[nk] = _normalize(val)
        return out
    raise CanonicalError(f"type not in canonical domain: {type(v).__name__}")


# ---------------------------------------------------------- dumps / loads

def dumps(obj) -> str:
    """canonical JSON（決定的）。受理値域外は CanonicalError。"""
    norm = _normalize(obj)
    return json.dumps(
        norm,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def dumps_bytes(obj) -> bytes:
    return dumps(obj).encode("utf-8")


def loads(text):
    """canonical JSON の読み戻し。bytes も受ける。"""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8")
    return json.loads(text)


# ------------------------------------------------------------------ hashing

def hash_bytes(tag: str, data: bytes) -> str:
    """sha256(tag ‖ b"\\x00" ‖ data) の hex。tag による domain 分離。"""
    if not isinstance(tag, str) or not tag:
        raise CanonicalError("hash tag must be a non-empty str")
    h = hashlib.sha256()
    h.update(tag.encode("utf-8"))
    h.update(b"\x00")
    h.update(data)
    return h.hexdigest()


def hash_obj(tag: str, obj) -> str:
    """obj を canonical 直列化して hash。"""
    return hash_bytes(tag, dumps_bytes(obj))


def hash_tuple(tag: str, *parts) -> str:
    """複合キーの正準 hash。タプルを JSON list として直列化する。"""
    return hash_obj(tag, list(parts))


# ------------------------------------------------- matching normalization

def norm_text(s: str) -> str:
    """照合用正規化（Q-O35: NFKC+casefold で非 ASCII を残す）＋空白畳み。

    直列化（NFC）とは別層。同一性・dedup・tombstone 照合の正規化指紋に使う。
    """
    if not isinstance(s, str):
        raise CanonicalError(f"norm_text expects str, got {type(s).__name__}")
    t = unicodedata.normalize("NFKC", s).casefold()
    return " ".join(t.split())


def content_hash(text: str) -> str:
    """memory.content_hash: 正規化本文のハッシュ（同一性・dedup・import_key 材料）。"""
    return hash_bytes("pcore.content_hash.v1", norm_text(text).encode("utf-8"))


def pattern_hash(text: str) -> str:
    """tombstone.pattern_hash: 拒否対象 fingerprint（照合 domain は content と別）。"""
    return hash_bytes("pcore.tombstone.v1", norm_text(text).encode("utf-8"))


def fingerprint(text: str) -> dict:
    """tombstone.fingerprint_norm に刻む「照合に使った正規化指紋」（SD-64）。

    codec/正規化の版を一緒に残し、照合の再現性を保持する。
    """
    return {"algo": "nfkc-casefold-v1", "norm": norm_text(text)}
