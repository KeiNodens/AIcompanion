"""kernel.migration —— L-MIG の純粋規則（migration-m5 §2 写像表・
import_key・mem0 値写像）。

I/O・時計は持たない。adapter（transcribe）が source 行を渡し、ここで
target 記述子と record state を決定的に組み立てる。
"""

from . import canon

IMPORT_KEY_TAG = "pcore.import_key.v1"
SEED_REASON = "migration_seed"          # 揮発値 seed の刻印理由（SD-33）
PRODUCER = "migration"                  # provenance.generated_by


def import_key(source_table, source_id) -> str:
    """hash(移行元の表名, 移行元の行ID) —— 再実行で二重化しない冪等錨。"""
    return canon.hash_tuple(IMPORT_KEY_TAG, source_table, str(source_id))


def migrated_record_id(import_key_str: str, prefix: str) -> str:
    """import_key から決定的な record_id を発番する。"""
    return canon.hash_tuple("pcore.migrated_id.v1", prefix, import_key_str)


# ------------------------------------------------------------- 写像表
# migration-m5 §2 の機械化。
# kind: record（canonical record 化）| event（Event Log 行化）|
#       blob（blob 参照化）| derived（再生成・移行しない）|
#       body（Body 側に残す）| skip（対象外）
SOURCE_MAP = {
    "mem0:user":   {"kind": "record", "family": "memory", "scope": "user"},
    "mem0:agent":  {"kind": "record", "family": "memory", "scope": "agent"},
    "history":     {"kind": "event", "event_type": "migration",
                    "note": "transform_history の種"},
    "messages":    {"kind": "event", "event_type": "migration",
                    "note": "mem0 側会話写し —— turns 正本と重複するため"
                            " 監査写しとして保持（Q-O17 決着）"},
    "turns":       {"kind": "event"},
    "file:persona.md": {"kind": "record", "family": "identity",
                        "scope": "agent", "identity_kind": "persona_core"},
    "file:persona_notes.md": {"kind": "record", "family": "identity",
                              "scope": "agent",
                              "identity_kind": "persona_note"},
    "approvals":   {"kind": "record", "family": "approval", "scope": "user"},
    "delegate_jobs": {"kind": "record+event", "family": "job",
                      "scope": "agent"},
    "state:inner": {"kind": "record+event", "family": "state",
                    "scope": "agent"},
    "state:llm":   {"kind": "record", "family": "memory", "scope": "user",
                    "memory_type": "preference"},
    "state:music_route": {"kind": "record", "family": "memory",
                          "scope": "user", "memory_type": "procedure"},
    "docs":        {"kind": "blob", "blob_kind": "pdf"},
    "delegate_logs": {"kind": "blob"},
    "tasks":       {"kind": "record", "family": "task", "scope": "user"},
    "lists":       {"kind": "record", "family": "list", "scope": "user"},
    "reminders":   {"kind": "record", "family": "reminder", "scope": "user"},
    "notify_outbox": {"kind": "body"},
    "job_runs":    {"kind": "body"},
    "reflection_state": {"kind": "body"},
    "recent_context_state": {"kind": "derived"},
    "pageindex":   {"kind": "derived"},
    "user_profile": {"kind": "derived"},
    "turns_state": {"kind": "skip"},
}


def source_kind(source_table: str) -> dict:
    """写像表の行を返す。未知 source は skip 扱い（安全側）。"""
    return SOURCE_MAP.get(source_table, {"kind": "skip"})


# ------------------------------------------------------- mem0 値写像
def map_importance(meta: dict) -> int:
    """mem0 metadata.importance → 1..5 int（欠損は 3 既定）。"""
    try:
        v = int(float((meta or {}).get("importance") or 3))
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, v))


def map_strength(meta: dict, *, default=0.5) -> float:
    """mem0 metadata.confidence → retrieval_strength seed。

    旧 confidence は「想起で上がる・時間で下がる」の単一値 —— 意味の
    詳細は失われる（損失宣言）。seed は decay の床 0.05..1.0 に clamp。
    """
    try:
        v = float((meta or {}).get("confidence"))
    except (TypeError, ValueError):
        v = default
    return round(max(0.05, min(1.0, v)), 3)


def mem0_scope(item: dict) -> str:
    """mem0 item の scope 判定（metadata/user_id/agent_id 優先順）。"""
    md = item.get("metadata") or {}
    if item.get("agent_id") or md.get("agent_id"):
        return "agent"
    return "user"


def mem0_item_to_state(item: dict, scope: str, ikey: str, *,
                       created_now: str) -> dict:
    """mem0 記憶行 → memory record の完全スナップショット。

    provenance_gap=structural（根拠イベントが存在しない旨を明示）＋
    evidence_basis=migrated_unknown。揮発値は seed 値として埋め、
    由来は param_update 同型の刻印で追う（SD-33）。
    """
    meta = dict(item.get("metadata") or {})
    content = item.get("memory") or item.get("content") or ""
    created = item.get("created_at") or created_now
    return {
        "record_family": "memory",
        "record_id": migrated_record_id(ikey, "mem"),
        "scope": scope,
        "status": "active",
        "record_version": 1,
        "memory_type": meta.get("type") or "fact",
        "content": content,
        "content_hash": canon.content_hash(content),
        "sensitivity": meta.get("sensitivity") or "normal",
        "importance": map_importance(meta),
        "retrieval_strength": map_strength(meta),
        "epistemic_confidence": meta.get("epistemic_confidence"),
        "last_recalled_at": meta.get("last_recalled_at") or created,
        "pinned": bool(meta.get("pinned")),
        "tier": "hot",
        "import_key": ikey,
        "provenance": {
            "evidence_basis": "migrated_unknown",
            "provenance_gap": "structural",
            "generated_by": PRODUCER,
            "source": {"system": "mem0", "memory_id": item.get("id"),
                       "scope": scope},
        },
        "created_at": created,
        "updated_at": created_now,
    }


def turn_row_to_events(row: dict, ikey: str) -> list:
    """turns 行 → message/response/inner の event 記述子。

    戻り値の各要素: {event_type, payload, occurred_at, actor, part}。
    inner_text は別 event（第三列、存在時のみ）。
    """
    out = []
    base = {"source": "turns", "turn_id": row.get("id"),
            "import_key": ikey}
    out.append({
        "event_type": "message",
        "payload": {"body": row.get("user_text") or "",
                    "origin": "user", "part_class": "conversation",
                    "migrated": True},
        "occurred_at": row.get("ts"),
        "actor": "master",
        "part": f"{ikey}:u",
        "base": base,
    })
    out.append({
        "event_type": "response",
        "payload": {"body": row.get("assistant_text") or "",
                    "origin": "assistant", "part_class": "conversation",
                    "migrated": True},
        "occurred_at": row.get("ts"),
        "actor": "companion",
        "part": f"{ikey}:a",
        "base": base,
    })
    if row.get("inner_text"):
        out.append({
            "event_type": "inner",
            "payload": {"body": row["inner_text"],
                        "origin": "assistant", "migrated": True},
            "occurred_at": row.get("ts"),
            "actor": "companion",
            "part": f"{ikey}:i",
            "base": base,
        })
    return out


def history_row_to_event(row: dict, ikey: str) -> dict:
    """mem0 history.db 行 → migration event 記述子（transform_history の種）。"""
    return {
        "event_type": "migration",
        "payload": {
            "action": "transform_history",
            "source": "history",
            "memory_id": row.get("memory_id"),
            "old_memory": row.get("old_memory"),
            "new_memory": row.get("new_memory"),
            "event": row.get("event"),
            "import_key": ikey,
        },
        "occurred_at": row.get("created_at"),
        "actor": "system",
        "part": ikey,
    }


# ------------------------------------------------------- 同等性判定（G1）
def norm_text(text: str) -> str:
    """意味的同等性比較用の正規化（空白・大小を畳む。NFC は canon 側の約束）。"""
    return " ".join(str(text or "").split()).casefold()


def equiv_match(old_item: dict, new_item: dict, *,
                sim_threshold=0.8) -> bool:
    """旧系 item と新系 record の構造的等価判定。

    優先: import_key/由来 id の一致（転記物なら確実）→ content 正規化一致 →
    trigram Jaccard ≥ 閾値。
    """
    new_prov = (new_item.get("provenance") or {}).get("source") or {}
    old_id = old_item.get("id")
    if old_id and new_prov.get("memory_id") == old_id:
        return True
    old_txt = norm_text(old_item.get("memory") or old_item.get("content"))
    new_txt = norm_text(new_item.get("content"))
    if old_txt and old_txt == new_txt:
        return True
    return trigram_sim(old_txt, new_txt) >= sim_threshold


def trigram_sim(a: str, b: str) -> float:
    """3-gram Jaccard。どちらか空なら 0。"""
    if not a or not b:
        return 0.0
    ta = {a[i:i + 3] for i in range(max(1, len(a) - 2))}
    tb = {b[i:i + 3] for i in range(max(1, len(b) - 2))}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def equiv_coverage(old_items: list, new_items: list, *,
                   sim_threshold=0.8) -> dict:
    """旧系集合が新系集合でどれだけ被覆されたか（G1 判定の機械部）。"""
    covered, missed = [], []
    for oi in old_items:
        if any(equiv_match(oi, ni, sim_threshold=sim_threshold)
               for ni in new_items):
            covered.append(oi.get("id") or oi.get("memory") or "?")
        else:
            missed.append(oi.get("id") or oi.get("memory") or "?")
    total = len(old_items)
    return {"total": total, "covered": len(covered),
            "coverage": (len(covered) / total) if total else 1.0,
            "missed": missed}
