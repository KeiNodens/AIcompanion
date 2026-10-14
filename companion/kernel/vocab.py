"""kernel.vocab —— open-world 語彙の初期集合と予約語彙の判定（canonical-schema §7）

全語彙は open-world: 初期集合を定義するが閉じた enum にしない。
未知の語彙値は keep-and-ignore（保持するが意味解釈しない）で受け入れる。
ただし予約値（`@core/` namespace・予約 record_family 等）のうち未登録のものは
fold/受理で MUST reject（SD-63）—— 「知らない」と「将来のために取ってある」は別物。
"""

# ------------------------------------------------------------- event_type
# canonical-schema §4.2 の初期語彙＋write-path §3 の poison/claim 系。
EVENT_TYPES = frozenset({
    # conversation
    "message", "response", "inner",
    # operation
    "tool_call", "tool_result", "notification", "job_transition",
    "runtime_observation",
    # memory
    "memory_write", "memory_update", "write_proposal", "candidate_review",
    "context_assembly", "extraction_scanned", "reflection_trigger",
    # mutation（非 memory 族）
    "record_write", "record_update",
    # governance
    "approval", "policy_change",
    # lifecycle
    "state_change", "reflection",
    # system
    "ingest", "redaction", "schema_transform", "import_anchor", "migration",
    "authority_transition", "blob_register", "archive",
    # L-ING（P8-2: transcript-ingress §4.2 の marker 語彙）
    "skipped_part", "ingress_gap",
    # poison/claim 系（write-path §3・SD-18: candidate.quarantined とは別物）
    "extraction_poisoned", "claim_stalled", "poison_release", "quarantine_release",
    # 規則値刻印（canonical-schema §4.2 注記: param_update 同型イベント）
    "param_update",
    # tombstone 系の影響記録（write-path §7.1: 根拠イベントを指す新規イベント）
    "memory_learn_stop",
})

MUTATION_EVENT_TYPES = frozenset({
    "memory_write", "memory_update", "record_write", "record_update",
})

# memory_update の op 語彙（canonical-schema §4.3）。open-world。
MEMORY_OPS = frozenset({
    "supersede", "invalidate", "restore", "correct", "demote",
    "strengthen", "merge", "split", "param_update",
})

# ---------------------------------------------------------- record_family
# canonical-schema §3 共通ヘッダ＋§7 語彙表。'event' は records に載らないので除く。
RECORD_FAMILIES = frozenset({
    "memory", "identity", "usermodel", "state", "approval", "candidate",
    "tombstone", "task", "list", "reminder", "job", "link", "blob",
    "prediction",
})
# 後続フェーズの予約（canonical-schema §3）。登録前に使われたら fold は keep-and-ignore。
RESERVED_FAMILIES = frozenset({"goal", "project", "plan", "policy", "decision"})

MEMORY_TYPES = frozenset({
    "fact", "episode", "preference", "procedure",
    "relationship", "intention", "insight",
})

MEMORY_STATUS = frozenset({
    "active", "demoted", "superseded", "invalidated", "merged",
})

CANDIDATE_STATUS = frozenset({
    "pending", "quarantined", "approved", "rejected", "expired",
})

LINK_TYPES = frozenset({
    "depends_on", "blocks", "conditional_blocks", "waits_for",
    "related", "caused_by", "discovered_from", "validates",
    "duplicates", "approved_by", "tracks", "authored_by",
})

SENSITIVITY = frozenset({"normal", "sensitive", "hidden"})
SUBJECT_KINDS = frozenset({"self", "master", "person", "topic", "entity"})
EVIDENCE_BASIS = frozenset({"direct", "inferred", "self_derived", "migrated_unknown"})
RETENTION_CLASSES = frozenset({"permanent", "ephemeral", "operational"})
TIERS = frozenset({"hot", "warm", "cold", "archive"})

PRODUCERS = frozenset({
    "extraction", "reflection", "manual", "media_log", "ingest",
    "migration", "explicit", "tier_cycle",
})

ACTORS = frozenset({"master", "companion", "system"})

# --------------------------------------------------------------- scope 系
SCOPES_INITIAL = frozenset({"user", "agent"})
# 予約名前空間（登録のみ・未使用）。person:<id> は接頭辞予約。
SCOPE_RESERVED = ("shared",)
SCOPE_RESERVED_PREFIXES = ("person:",)

# ------------------------------------------------------------- 予約語彙
# fold/受理で MUST reject する予約 namespace（SD-63）。
RESERVED_PREFIXES = ("@core/",)

# session namespace の予約（core-api §2.2: Body 分離時の入口識別）。
SESSION_RESERVED_PREFIXES = ("<body>:",)


def is_reserved_value(v) -> bool:
    """予約 namespace 語彙の判定。未登録の予約値は reject 対象。"""
    return isinstance(v, str) and any(v.startswith(p) for p in RESERVED_PREFIXES)


def is_known_actor(v) -> bool:
    return isinstance(v, str) and (v in ACTORS or v.startswith("external:"))


def is_reserved_scope(v) -> bool:
    return isinstance(v, str) and (
        v in SCOPE_RESERVED or any(v.startswith(p) for p in SCOPE_RESERVED_PREFIXES)
    )


def vocab_hash() -> str:
    """語彙集合の pin hash（fold/oracle 両実装の「kept-but-ineffective」
    計数同期・derived manifest の event_vocab_version に使う。SD-19/SD-63）。"""
    from . import canon
    return canon.hash_obj("pcore.vocab.v1", {
        "event_types": sorted(EVENT_TYPES),
        "memory_ops": sorted(MEMORY_OPS),
        "record_families": sorted(RECORD_FAMILIES),
        "memory_types": sorted(MEMORY_TYPES),
        "memory_status": sorted(MEMORY_STATUS),
        "candidate_status": sorted(CANDIDATE_STATUS),
        "link_types": sorted(LINK_TYPES),
        "sensitivity": sorted(SENSITIVITY),
        "subject_kinds": sorted(SUBJECT_KINDS),
        "evidence_basis": sorted(EVIDENCE_BASIS),
        "retention_classes": sorted(RETENTION_CLASSES),
        "tiers": sorted(TIERS),
        "producers": sorted(PRODUCERS),
        "actors": sorted(ACTORS),
    })
