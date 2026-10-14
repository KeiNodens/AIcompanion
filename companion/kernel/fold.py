"""kernel.fold —— events → records の決定的投影（REQ-X12・evolution §7）

再構築は「record_id ごとに最新の mutation イベントの new_state を収束する」
決定的手順。LLM・時計・乱数を介さない。

- 処理対象は mutation 語彙のみ（memory_write/memory_update/record_write/
  record_update）。それ以外の既知 event_type は投影対象外として読み飛ばす。
- 未知の event_type は keep-and-ignore: 読み飛ばして unhandled 計数に載せる
  （「両実装が同じものを落として green になる」盲点を塞ぐ SD-63）。
- 予約語彙値（@core/ 等）の未知値は FoldError で reject（SD-63）。
- 揮発フィールド（retrieval_strength/last_recalled_at）は P8-1 では
  スナップショット値をそのまま採る。receipt/sweep 駆動の規則再計算は
  P8-3/P8-4 で入る（phase8-impl.md に制限明記）。

row_for_* 系は「収束した new_state → 物理表の行」への写像で、
writer.apply_mutation の upsert と rebuild/verify の両方が共有する。
"""

from . import canon, events, schema, vocab

FOLD_VERSION = "fold-v1"


class FoldError(ValueError):
    """fold が reject する入力（予約語彙・壊れた mutation 行）。"""


def _check_reserved(value, field):
    if vocab.is_reserved_value(value):
        raise FoldError(f"reserved {field} rejected: {value}")


def fold_rows(rows) -> dict:
    """events 行の iterable → {(family, scope, record_id): (new_state, seq)}。

    rows は seq 昇順を前提（呼び出し側が ORDER BY seq）。行は events.parse_row
    済み（payload が dict）か生の行どちらでも受ける。
    """
    state = {}
    counters = {"events": 0, "mutations": 0}
    unhandled = {}
    for row in rows:
        r = dict(row)
        if "payload" not in r:
            r = events.parse_row(r)
        counters["events"] += 1
        et = r["event_type"]
        _check_reserved(et, "event_type")
        if et in ("memory_write", "memory_update"):
            p = r["payload"]
            st = p["new_state"]
            _check_reserved(p.get("op") or "", "op")
            _check_reserved(st.get("status", ""), "status")
            _check_reserved(st.get("memory_type", ""), "memory_type")
            key = ("memory", st["scope"], p["memory_id"])
            state[key] = (st, r["seq"])
            counters["mutations"] += 1
        elif et in ("record_write", "record_update"):
            p = r["payload"]
            st = p["new_state"]
            _check_reserved(p.get("record_family", ""), "record_family")
            _check_reserved(st.get("status", ""), "status")
            key = (p["record_family"], st["scope"], p["record_id"])
            state[key] = (st, r["seq"])
            counters["mutations"] += 1
        elif et not in vocab.EVENT_TYPES:
            unhandled[et] = unhandled.get(et, 0) + 1
    return {
        "state": state,
        "counters": counters,
        "unhandled_event_types": unhandled,
        "fold_version": FOLD_VERSION,
    }


def replay_digest(state: dict) -> str:
    """fold 結果（{(family,scope,rid): (state, seq)}）の正準 digest。

    digest は「全 record の確定値」の畳み込み（evolution §7）。揮発値の
    基準時刻再計算は P8-4 で差し込む。現時点はスナップショット完全写し。
    """
    items = [
        [fam, scope, rid, st, seq]
        for (fam, scope, rid), (st, seq) in sorted(state.items())
    ]
    return canon.hash_obj(events.TAG_REPLAY, items)


def states_digest(states) -> str:
    """new_state の iterable に対する digest（row_for なしで直接比較する用）。"""
    items = []
    for st in states:
        key = (st["record_family"], st["scope"], st["record_id"])
        items.append([key[0], key[1], key[2], st])
    items.sort()
    return canon.hash_obj(events.TAG_REPLAY, items)


# ------------------------------------------------- new_state → 物理表の行
# 共通ヘッダを列に展開し、完全スナップショットを state_json に保持する。
# state_json が replay/validate の一致比較対象（列は照会・索引の補助）。

def _common_cols(st: dict) -> dict:
    temporal = st.get("temporal") or {}
    return {
        "scope": st["scope"],
        "record_id": st["record_id"],
        "record_family": st["record_family"],
        "status": st["status"],
        "content_hash": st.get("content_hash"),
        "record_version": st["record_version"],
        "schema_version": st.get("schema_version", 1),
        "importance": st.get("importance"),
        "retrieval_strength": st.get("retrieval_strength"),
        "last_recalled_at": st.get("last_recalled_at"),
        "epistemic_confidence": st.get("epistemic_confidence"),
        "param_policy_ref": st.get("param_policy_ref"),
        "valid_from": temporal.get("valid_from") or st.get("valid_from"),
        "valid_to": temporal.get("valid_to") or st.get("valid_to"),
        "sensitivity": st.get("sensitivity", "normal"),
        "pinned": 1 if st.get("pinned") else 0,
        "tier": st.get("tier", "hot"),
        "subject_json": canon.dumps(st["subject"]) if st.get("subject") else None,
        "provenance_json": (canon.dumps(st["provenance"])
                            if st.get("provenance") else None),
        "attachments_json": (canon.dumps(st["attachments"])
                             if st.get("attachments") else None),
        "migration_json": (canon.dumps(st["migration"])
                           if st.get("migration") else None),
        "external_ref_json": (canon.dumps(st["external_ref"])
                              if st.get("external_ref") else None),
        "created_at": st["created_at"],
        "updated_at": st["updated_at"],
    }


def _kind_of(st: dict):
    """family 副キー: memory_type / identity.kind / usermodel.attribute /
    state.key / job.kind / link.link_type 等を統一列に収める。"""
    fam = st["record_family"]
    for f in ("memory_type", "kind", "attribute", "key", "link_type"):
        if st.get(f):
            return st[f]
    return None


def record_row(st: dict, seq: int) -> dict:
    row = _common_cols(st)
    row["kind"] = _kind_of(st)
    row["state_json"] = canon.dumps(st)
    row["last_mutation_seq"] = seq
    return row


def candidate_row(st: dict, seq: int) -> dict:
    return {
        "scope": st["scope"],
        "candidate_id": st["record_id"],
        "status": st["status"],
        "proposal_json": canon.dumps(st.get("proposal") or {}),
        "verdicts_json": (canon.dumps(st["verdicts"])
                          if st.get("verdicts") is not None else None),
        "review_json": (canon.dumps(st["review"])
                        if st.get("review") is not None else None),
        "suppression_key": (st.get("suppression") or {}).get("key"),
        "suppression_until": (st.get("suppression") or {}).get("until"),
        "suppression_lifted_by": (st.get("suppression") or {}).get("lifted_by"),
        "producer": (st.get("provenance") or {}).get("generated_by"),
        "state_json": canon.dumps(st),
        "created_at": st["created_at"],
        "updated_at": st["updated_at"],
        "last_mutation_seq": seq,
    }


def tombstone_row(st: dict, seq: int) -> dict:
    return {
        "scope": st["scope"],
        "tombstone_id": st["record_id"],
        "pattern_hash": st["pattern_hash"],
        "pattern": st.get("pattern"),
        "pattern_kind": st.get("pattern_kind"),
        "fingerprint_norm": (canon.dumps(st["fingerprint_norm"])
                             if isinstance(st.get("fingerprint_norm"), (dict, list))
                             else st.get("fingerprint_norm")),
        "embedding_space": st.get("embedding_space"),
        "derived_refs_json": (canon.dumps(st["derived_refs"])
                              if st.get("derived_refs") is not None else None),
        "reason": st.get("reason"),
        "source_memory_id": st.get("source_memory_id"),
        "status": st["status"],
        "state_json": canon.dumps(st),
        "created_at": st["created_at"],
        "updated_at": st["updated_at"],
        "last_mutation_seq": seq,
    }


def link_row(st: dict, seq: int) -> dict:
    return {
        "scope": st["scope"],
        "link_id": st["record_id"],
        "source_id": st["source_id"],
        "target_id": st["target_id"],
        "link_type": st["link_type"],
        "blocking": 1 if st.get("blocking") else 0,
        "status": st["status"],
        "state_json": canon.dumps(st),
        "created_at": st["created_at"],
        "updated_at": st["updated_at"],
        "last_mutation_seq": seq,
    }


def blob_row(st: dict, seq: int) -> dict:
    return {
        "scope": st["scope"],
        "hash": st["record_id"],
        "kind": st.get("kind"),
        "size": st.get("size"),
        "media_meta_json": (canon.dumps(st["media_meta"])
                            if st.get("media_meta") is not None else None),
        "retention_class": st.get("retention_class", "permanent"),
        "ref": st.get("ref"),
        "status": st["status"],
        "state_json": canon.dumps(st),
        "created_at": st["created_at"],
        "updated_at": st["updated_at"],
        "last_mutation_seq": seq,
    }


ROW_BUILDERS = {
    "candidate": ("candidates", candidate_row),
    "tombstone": ("tombstones", tombstone_row),
    "link": ("links", link_row),
    "blob": ("blobs", blob_row),
}


def row_for(family: str, st: dict, seq: int):
    """(table_name, row_dict)。未知 family は records に載せる（keep-and-ignore）。"""
    if family in ROW_BUILDERS:
        table, builder = ROW_BUILDERS[family]
        return table, builder(st, seq)
    return "records", record_row(st, seq)


def diff_states(folded: dict, stored: dict) -> list:
    """fold 結果と格納済み state（{key: (state, seq)} 同型）の差分を列挙。

    確定値フィールドは canon 直列化の byte 等価で比較する（揮発値は現段階で
    スナップショット値をそのまま格納・比較する。P8-4 で規則再計算に移行）。
    """
    diffs = []
    for key in sorted(set(folded) | set(stored)):
        if key not in folded:
            diffs.append({"key": key, "issue": "stored_only"})
            continue
        if key not in stored:
            diffs.append({"key": key, "issue": "replay_only"})
            continue
        fst, fseq = folded[key]
        sst, sseq = stored[key]
        if fseq != sseq:
            diffs.append({"key": key, "issue": "seq_mismatch",
                          "replay_seq": fseq, "stored_seq": sseq})
        elif canon.dumps(fst) != canon.dumps(sst):
            diffs.append({"key": key, "issue": "state_mismatch"})
    return diffs
