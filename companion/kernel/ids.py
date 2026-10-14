"""kernel.ids —— 発番 ID の生成規則（純粋。時計・乱数は引数で注入）

canonical-schema §4.1: event_id は `evt_<uuid>` 形の識別子で順序の担い手ではない
（全順序は seq）。D-A3: 発番 ID＋content_hash 併記。DEV-020: 内容由来にしない。

event_id は ULID 形（48bit 時刻 ms + 80bit 乱数・Crockford base32・26 文字）で、
単調増加が目視できる一方、順序保証は持たせない（seq が全順序）。
"""

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# record_family → record_id の接頭辞（<family>_<uuid> 形の不透明 urn。D-A3）
FAMILY_PREFIX = {
    "memory": "mem",
    "identity": "idn",
    "usermodel": "umd",
    "state": "stt",
    "approval": "apr",
    "candidate": "cand",
    "tombstone": "tmb",
    "task": "tsk",
    "list": "lst",
    "reminder": "rmd",
    "job": "job",
    "link": "lnk",
    "blob": "blob",
    "prediction": "prd",
}


def _b32(value: int, width: int) -> str:
    if value < 0:
        raise ValueError("negative value cannot be base32-encoded")
    out = []
    for _ in range(width):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    if value:
        raise ValueError("value does not fit width")
    return "".join(reversed(out))


def ulid(ts_ms: int, rand80: int) -> str:
    """ULID 形 26 文字。ts_ms は ms 時刻、rand80 は 80bit 乱数（注入値）。"""
    return _b32(ts_ms, 10) + _b32(rand80, 16)


def event_id(ts_ms: int, rand80: int) -> str:
    return "evt_" + ulid(ts_ms, rand80)


def record_id(family: str, uuid_hex: str) -> str:
    """`<prefix>_<uuid>` 形の不透明 record_id。未知 family も受理（open-world）。"""
    prefix = FAMILY_PREFIX.get(family)
    if prefix is None:
        prefix = "".join(c for c in family if c.isalnum())[:4] or "rec"
    clean = uuid_hex.lower().replace("-", "")
    return f"{prefix}_{clean}"
