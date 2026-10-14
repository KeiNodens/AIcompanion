"""kernel.oracle —— fold/decide の意味層独立実装（SD-41・L-VER）

fold.py とは**別の計算構造**で同じ意味結果を出す第2実装:
- fold: events を1回走査して record ごとに last-wins 上書き
- oracle: record キーで群集約 → 各群の最大 seq を取り出す

両者が同じ合成 corpus で一致しない場合、意味層に潜むバグがある
（SD-63 の「kept-but-ineffective」計数も両実装で同期して可視化）。

codec/hashing は canon/events の共有契約を使う（独立するのは
「アルゴリズム構造」であって直列化規則そのものではない）。
"""

from . import canon, events, fold, vocab

ORACLE_VERSION = "oracle-v1"


class OracleError(ValueError):
    """oracle 側の reject（fold の FoldError と別型 = 独立実装の証）。"""


def oracle_fold(rows) -> dict:
    """record 群集約方式の fold。戻り値は fold.fold_rows と同型。

    rows は seq 昇順前提。rows は events.parse_row 済みでも生行でも可。
    """
    # 1) 群集約: {(family,scope,rid): {seq: state}} —— 上書きではなく蓄積
    groups = {}
    counters = {"events": 0, "mutations": 0}
    unhandled = {}
    for row in rows:
        r = dict(row)
        if "payload" not in r:
            r = events.parse_row(r)
        counters["events"] += 1
        et = r["event_type"]
        if vocab.is_reserved_value(et):
            raise OracleError(f"reserved event_type rejected: {et}")
        if et in ("memory_write", "memory_update"):
            p = r["payload"]
            st = p["new_state"]
            key = ("memory", st["scope"], p["memory_id"])
            for v, f in ((p.get("op") or "", "op"),
                         (st.get("status", ""), "status"),
                         (st.get("memory_type", ""), "memory_type")):
                if vocab.is_reserved_value(v):
                    raise OracleError(f"reserved {f} rejected: {v}")
            groups.setdefault(key, {})[r["seq"]] = st
            counters["mutations"] += 1
        elif et in ("record_write", "record_update"):
            p = r["payload"]
            st = p["new_state"]
            for v, f in ((p.get("record_family", ""), "record_family"),
                         (st.get("status", ""), "status")):
                if vocab.is_reserved_value(v):
                    raise OracleError(f"reserved {f} rejected: {v}")
            key = (p["record_family"], st["scope"], p["record_id"])
            groups.setdefault(key, {})[r["seq"]] = st
            counters["mutations"] += 1
        elif et not in vocab.EVENT_TYPES:
            unhandled[et] = unhandled.get(et, 0) + 1
    # 2) 各群の最大 seq を収束（fold の last-wins と意味同値だが手順が違う）
    state = {}
    for key, seqs in groups.items():
        seq = max(seqs)
        state[key] = (seqs[seq], seq)
    return {"state": state, "counters": counters,
            "unhandled_event_types": unhandled,
            "fold_version": fold.FOLD_VERSION,
            "oracle_version": ORACLE_VERSION}


def oracle_verify_chain(rows) -> dict:
    """hash 鎖の独立検査。末尾から prev_hash を遡る（fold 側 verify_chain
    は先頭から積むので走査方向が違う = 共通モードずれを拾える）。"""
    parsed = []
    for row in rows:
        r = dict(row)
        if "payload" not in r:
            r = events.parse_row(r)
        parsed.append(r)
    by_seq = {r["seq"]: r for r in parsed}
    out = {"ok": True, "events": len(parsed), "errors": []}
    if not parsed:
        return out
    # 末尾から遡る（鎖方向を逆向きに検査）
    seq = parsed[-1]["seq"]
    while seq >= 1:
        r = by_seq.get(seq)
        if r is None:
            out["errors"].append({"seq": seq, "issue": "seq_gap"})
            out["ok"] = False
            seq -= 1
            continue
        phash = events.payload_hash(r["payload"])
        rh = events.row_hash(r["seq"], r["event_type"], r["recorded_at"],
                             r["prev_hash"], phash)
        if rh != r["row_hash"]:
            out["errors"].append({"seq": seq, "issue": "row_hash"})
            out["ok"] = False
        prev = by_seq.get(seq - 1)
        expect_prev = events.GENESIS_HASH if seq == 1 \
            else (prev["row_hash"] if prev else None)
        if r["prev_hash"] != expect_prev:
            out["errors"].append({"seq": seq, "issue": "prev_hash_link"})
            out["ok"] = False
        seq -= 1
    return out


def compare(fold_result: dict, oracle_result: dict) -> dict:
    """fold vs oracle の意味層一致検査。state/counters/unhandled の3面。

    戻り値 {"ok": bool, "diffs": [...]}。diffs は record 単位の
    issue 列挙（missing_one_side/state_mismatch/counter_mismatch）。
    """
    diffs = []
    fs, os_ = fold_result["state"], oracle_result["state"]
    for key in sorted(set(fs) | set(os_)):
        if key not in os_:
            diffs.append({"key": key, "issue": "fold_only"})
        elif key not in fs:
            diffs.append({"key": key, "issue": "oracle_only"})
        else:
            fst, fseq = fs[key]
            ost, oseq = os_[key]
            if fseq != oseq:
                diffs.append({"key": key, "issue": "seq_mismatch",
                              "fold_seq": fseq, "oracle_seq": oseq})
            elif canon.dumps(fst) != canon.dumps(ost):
                diffs.append({"key": key, "issue": "state_mismatch"})
    if fold_result["counters"] != oracle_result["counters"]:
        diffs.append({"issue": "counter_mismatch",
                      "fold": fold_result["counters"],
                      "oracle": oracle_result["counters"]})
    if (fold_result.get("unhandled_event_types")
            != oracle_result.get("unhandled_event_types")):
        diffs.append({"issue": "unhandled_mismatch",
                      "fold": fold_result.get("unhandled_event_types"),
                      "oracle": oracle_result.get("unhandled_event_types")})
    return {"ok": not diffs, "diffs": diffs}


def semantic_outcome(verdict: str) -> str:
    """decide の意味層分類（verdict → 書込みが確定するか）。

    独立実装として「受理系 = 書込み確定」の2値に畳む。rederive_verdicts
    側は検査列16項目の完全再現で照合する（意味は同じ・粒度が違う）。
    """
    if verdict in ("accept", "noop_duplicate"):
        return "writes"
    return "held"
