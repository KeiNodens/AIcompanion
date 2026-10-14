"""kernel.events —— Event Log の行構造と hash 鎖の純粋規則（canonical-schema §4）

event は生の出来事記録であり scope を持たない（共通ヘッダ外）。全順序の担い手は
seq、権威時刻は recorded_at（occurred_at は送信者の申告値）。

hash 鎖規約（SD-04 / MD-1 決裁 (b)）:
  payload_hash = sha256("pcore.payload.v1" ‖ canon(payload))
  row_hash     = sha256("pcore.event.v1"   ‖ canon([seq, event_type,
                                                   recorded_at, prev_hash,
                                                   payload_hash]))
commit 対象は payload 本体ではなく payload_hash（M-14: payload 本文への直接 commit
は消去要求の余地と preimage 攻撃面を生むため）。
先頭イベントの prev_hash は GENESIS_HASH。
"""

from . import canon, vocab

TAG_PAYLOAD = "pcore.payload.v1"
TAG_EVENT = "pcore.event.v1"
TAG_REPLAY = "pcore.replay.v1"
TAG_IMPORT_KEY = "pcore.import_key.v1"

# 先頭イベントの prev_hash に使う固定値（空鎖の起点を一意に定義する）
GENESIS_HASH = canon.hash_bytes("pcore.genesis.v1", b"")


class EventError(ValueError):
    """イベント受理値域・必須フィールドの違反。"""


def payload_hash(payload) -> str:
    return canon.hash_bytes(TAG_PAYLOAD, canon.dumps_bytes(payload))


def row_hash(seq: int, event_type: str, recorded_at: str,
             prev_hash: str, phash: str) -> str:
    return canon.hash_obj(
        TAG_EVENT, [seq, event_type, recorded_at, prev_hash, phash])


def build_event(*, event_id: str, seq: int, event_type: str, actor: str,
                recorded_at: str, payload, prev_hash: str = GENESIS_HASH,
                occurred_at=None, session=None, attachments=None,
                redacted=None, correlation=None, source_meta=None,
                ingress_key=None, migration=None) -> dict:
    """events 行（dict）を組み立てる。時計・ID・seq は呼び出し側（writer）が注入。"""
    phash = payload_hash(payload)
    rhash = row_hash(seq, event_type, recorded_at, prev_hash, phash)
    sess = dict(session) if session else {}
    corr = dict(correlation) if correlation else {}
    smeta = dict(source_meta) if source_meta else {}
    if ingress_key is not None:
        smeta["ingress_key"] = ingress_key
    return {
        "seq": seq,
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "actor": actor,
        "session_json": canon.dumps(sess) if sess else None,
        "session_channel": sess.get("channel"),
        "session_id": sess.get("session_id"),
        "payload_json": canon.dumps(payload),
        "attachments_json": canon.dumps(attachments) if attachments else None,
        "redacted_json": canon.dumps(redacted) if redacted else None,
        "correlation_json": canon.dumps(corr) if corr else None,
        "turn_id": corr.get("turn_id"),
        "source_meta_json": canon.dumps(smeta) if smeta else None,
        "ingress_key": ingress_key,
        "prev_hash": prev_hash,
        "row_hash": rhash,
        "canon_algo": canon.SERIALIZER_VERSION,
        "migration_json": canon.dumps(migration) if migration else None,
    }


def check_event(e: dict) -> list:
    """受理値域の検査。違反の文字列リストを返す（空=合格）。

    語彙は open-world: 未知 event_type は受理するが、予約値（@core/ 等）の
    未登録値は reject（SD-63）。
    """
    errs = []
    et = e.get("event_type")
    if not isinstance(et, str) or not et:
        errs.append("event_type missing")
    elif vocab.is_reserved_value(et):
        errs.append(f"reserved event_type rejected: {et}")
    actor = e.get("actor")
    if not vocab.is_known_actor(actor):
        errs.append(f"actor not in vocabulary: {actor!r}")
    if not isinstance(e.get("seq"), int) or e["seq"] < 1:
        errs.append("seq must be a positive int")
    if not isinstance(e.get("event_id"), str) or not e["event_id"]:
        errs.append("event_id missing")
    if not canon.is_ts(e.get("recorded_at")):
        errs.append("recorded_at must be canonical UTC µs timestamp")
    if e.get("occurred_at") is not None and not isinstance(e["occurred_at"], str):
        errs.append("occurred_at must be str or null")
    if not isinstance(e.get("payload_json"), str):
        errs.append("payload_json missing")
    if not isinstance(e.get("row_hash"), str) or len(e["row_hash"]) != 64:
        errs.append("row_hash malformed")
    return errs


def parse_row(row) -> dict:
    """events 行（sqlite Row/dict）を fold 用に payload を parse した dict にする。"""
    r = dict(row)
    r["payload"] = canon.loads(r["payload_json"])
    return r


def import_key(source_table: str, source_id: str) -> str:
    """migration の冪等キー: hash(移行元表, 移行元ID)（canonical-schema §3）。"""
    return canon.hash_tuple(TAG_IMPORT_KEY, source_table, source_id)


def check_mutation_payload(event_type: str, payload: dict) -> list:
    """memory_write/memory_update/record_write/record_update の構造検査。"""
    errs = []
    if not isinstance(payload, dict):
        return ["mutation payload must be dict"]
    st = payload.get("new_state")
    if not isinstance(st, dict):
        errs.append("new_state missing (mutation events carry full snapshots)")
        return errs
    if event_type in ("memory_write", "memory_update"):
        if payload.get("memory_id") != st.get("record_id"):
            errs.append("memory_id != new_state.record_id")
        if st.get("record_family") != "memory":
            errs.append("memory_* payload must carry record_family='memory'")
        if event_type == "memory_update" and "op" not in payload:
            errs.append("memory_update requires op")
        elif vocab.is_reserved_value(payload.get("op") or ""):
            errs.append(f"reserved op rejected: {payload.get('op')}")
    else:
        fam = payload.get("record_family")
        if not isinstance(fam, str) or not fam:
            errs.append("record_family missing")
        elif vocab.is_reserved_value(fam):
            errs.append(f"reserved record_family rejected: {fam}")
        elif payload.get("record_id") != st.get("record_id"):
            errs.append("record_id != new_state.record_id")
        elif st.get("record_family") != fam:
            errs.append("payload.record_family != new_state.record_family")
    # 共通ヘッダの最低限（完全スナップショットは replay 照合の対象本体）
    for f in ("scope", "record_version", "status", "created_at", "updated_at"):
        if f not in st:
            errs.append(f"new_state.{f} missing")
    if not isinstance(st.get("record_version"), int) or st["record_version"] < 1:
        errs.append("record_version must be a positive int")
    if vocab.is_reserved_value(st.get("status", "")):
        errs.append(f"reserved status rejected: {st.get('status')}")
    be = payload.get("basis_events")
    if be is not None and not isinstance(be, list):
        errs.append("basis_events must be a list")
    return errs
