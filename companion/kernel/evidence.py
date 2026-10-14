"""kernel.evidence —— 証拠資格の決定的分類（write-path §4.2・L-GOV）

判定は「basis イベントの actor」ではなく「basis イベント内容の起源」で行う
（echo 防御の判定側）。basis payload が companion 由来の生成物（注入文脈・
memory 本文・receipt・mutation スナップショット・記憶を返す tool_result）を
含む場合、actor に関わらず companion 由来として扱う（汚染伝播）。

evidence_basis の格順（権限単調性・SD-57）:
  direct > inferred > self_derived > migrated_unknown
派生レコードの evidence_basis は祖先の格を超えない（laundering 拒否）。
同一起源の basis は dedupe（同一 cause/turn 由来は1票）。provenance 不明は
1バケツ扱い（不明同士を独立証拠と数えない）。

basis_infos の要素形（adapter が events 表から作る描述子）:
  {event_id, actor, event_type, origin?, cite?, sanity_flag?: bool,
   companion_artifact?: bool, cause_event_id?, turn_id?}
    - origin: normalizer が付けた値（injected/system/tool/...）
    - companion_artifact: payload が companion 生成物を含むと判定済みの印
      （context_assembly receipt との digest 突合結果等を adapter が反映）
"""

from . import canon

ORIGINS = frozenset({
    "user_stated", "tool_derived", "self_derived", "migrated_unknown",
})

EVIDENCE_BASIS = frozenset({
    "direct", "inferred", "self_derived", "migrated_unknown",
})

# 権限単調性の格順（大きいほど強い証拠）
BASIS_RANK = {
    "direct": 3,
    "inferred": 2,
    "self_derived": 1,
    "migrated_unknown": 0,
}

# cite 必須の basis 型（tool/system 由来は引用箇所の実在を要求する）
_CITE_REQUIRED_TYPES = frozenset({"tool_result", "tool_call", "ingest"})
_CITE_REQUIRED_ORIGINS = frozenset({"tool", "system", "imported"})

# companion 由来とみなす origin（内容起源判定）
_COMPANION_ORIGINS = frozenset({"assistant", "injected", "system"})


def basis_origin(info: dict) -> str:
    """basis イベント1件の内容起源を分類する。

    優先順: companion_artifact 印 > origin 申告 > actor 帰属。
    """
    if info.get("companion_artifact"):
        return "self_derived"
    o = info.get("origin")
    if o == "imported":
        return "migrated_unknown"
    if o in _COMPANION_ORIGINS:
        return "self_derived"
    if o == "tool":
        return "tool_derived"
    if info.get("actor") == "master":
        return "user_stated"
    if info.get("actor") == "companion":
        return "self_derived"
    if info.get("actor") == "system" or str(info.get("actor", "")).startswith(
            "external:"):
        return "tool_derived"
    return "migrated_unknown"


def cite_required(info: dict) -> bool:
    return (info.get("event_type") in _CITE_REQUIRED_TYPES
            or info.get("origin") in _CITE_REQUIRED_ORIGINS)


def cap_basis(child_basis: str, ancestor_bases) -> str:
    """権限単調性: 派生の evidence_basis は祖先の最小格を超えない。

    ancestor_bases は祖先 record の evidence_basis 値列。空なら制限なし。"""
    if not ancestor_bases:
        return child_basis
    floor = min(BASIS_RANK.get(b, 0) for b in ancestor_bases)
    if BASIS_RANK.get(child_basis, 0) > floor:
        inv = {v: k for k, v in BASIS_RANK.items()}
        return inv[floor]
    return child_basis


def evaluate_basis(proposal: dict, basis_infos, scope: str) -> dict:
    """write-path §4.2 の判定表。

    戻り値:
      {evidence_basis, disposition: "accept"|"quarantine",
       cite_missing: [event_id], sanity_basis: [event_id],
       origins: {event_id: origin}, reasons: [...]}

    disposition=quarantine は admission 側で quarantine verdict へ写像する。
    """
    producer = proposal.get("producer")
    gap = (proposal.get("provenance_gap")
           or (proposal.get("provenance") or {}).get("provenance_gap"))
    reasons = []

    # producer=migration かつ provenance_gap=structural は basis 空でも受理
    if not basis_infos:
        if producer == "migration" and gap == "structural":
            return {"evidence_basis": "migrated_unknown",
                    "disposition": "accept", "cite_missing": [],
                    "sanity_basis": [], "origins": {},
                    "reasons": ["migration lane: empty basis allowed"]}
        return {"evidence_basis": None, "disposition": "quarantine",
                "cite_missing": [], "sanity_basis": [], "origins": {},
                "reasons": ["empty basis without migration lane"]}

    origins = {i["event_id"]: basis_origin(i) for i in basis_infos}
    cite_missing = [i["event_id"] for i in basis_infos
                    if cite_required(i) and not i.get("cite")]
    sanity_basis = [i["event_id"] for i in basis_infos
                    if i.get("sanity_flag")]

    # 同一起源 dedupe（同一 cause/turn 由来は1票、provenance 不明は1バケツ）
    seen_sources = set()
    distinct = []
    for i in basis_infos:
        src = i.get("cause_event_id") or i.get("turn_id") or "unknown"
        if src in seen_sources:
            continue
        seen_sources.add(src)
        distinct.append(i)
    _ = distinct  # 計数情報は呼び出し側の verdicts detail 用に残す

    if sanity_basis:
        reasons.append(f"basis on sanity-flagged events: {sanity_basis}")
        return {"evidence_basis": None, "disposition": "quarantine",
                "cite_missing": cite_missing, "sanity_basis": sanity_basis,
                "origins": origins, "reasons": reasons}

    oset = set(origins.values())

    if producer == "explicit" or oset <= {"user_stated"}:
        basis = "direct"
    elif scope == "user" and oset <= {"self_derived"}:
        # scope=user の事実を companion 由来だけで裏付けようとしている
        reasons.append("scope=user with companion-only basis")
        return {"evidence_basis": None, "disposition": "quarantine",
                "cite_missing": cite_missing, "sanity_basis": sanity_basis,
                "origins": origins, "reasons": reasons}
    elif scope == "user" and "user_stated" in oset:
        basis = "inferred"  # master 由来と companion 由来の混在
    elif scope == "agent" and oset <= {"self_derived", "tool_derived"}:
        basis = "self_derived"  # 経験記憶は正当
    elif oset <= {"tool_derived", "user_stated"}:
        basis = "inferred"
    elif oset <= {"migrated_unknown"}:
        basis = "migrated_unknown"
    else:
        basis = "inferred"

    return {"evidence_basis": basis, "disposition": "accept",
            "cite_missing": cite_missing, "sanity_basis": sanity_basis,
            "origins": origins, "reasons": reasons}


def origin_digest(text: str) -> str:
    """注入内容 digest 照合用の part 本文 digest（context_assembly receipt
    との突合に使う —— normalizer が origin=injected を機械付与する本線）。"""
    return canon.hash_bytes("pcore.part_digest.v1",
                            canon.norm_text(text).encode("utf-8"))
