"""kernel.schema —— data/core.db の中核スキーマ（DDL はデータ、適用は writer）

P8-1 の中核: events / records / candidates / tombstones / links / blobs /
record_source＋registry（core_meta・scope_registry・vocab_registry・
kernel_table・policy_state・tool_ledger・migration_map・archive_segments・
schema_migrations）＋operational（watermarks・extraction_attempts・
derived_generations）。
P8-2 で追加: L-ING 群（ingress_envelopes・ingress_receipts・
ingress_watermarks・normalize_queue・claims・claim_events）。
deliver_state・staged_* は P8-7 で schema_migrations 経由で追加する。

- 全 canonical レコードは「削除しない」。DELETE 禁止は BEFORE トリガー＋
  writer の set_authorizer の二層で強制する（triggers は接続横断で効く）。
- events は UPDATE も禁止（append-only）。records 等の現在値表は UPDATE を
  writer の mutation apply が使うので許可、DELETE のみ禁止。
- L-ING 群は operational 区分（FA §3.1）。envelope 生保全と receipt 監査の
  ため ingress_envelopes/ingress_receipts/claim_events も DELETE 禁止にする
  （spec の要求より強い側に倒す）。
"""

SCHEMA_VERSION = 5

# ---------------------------------------------------------------- DDL

DDL = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    occurred_at TEXT,
    recorded_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    session_json TEXT,
    session_channel TEXT,
    session_id TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    attachments_json TEXT,
    redacted_json TEXT,
    correlation_json TEXT,
    turn_id TEXT,
    source_meta_json TEXT,
    ingress_key TEXT,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL,
    canon_algo TEXT NOT NULL,
    migration_json TEXT,
    UNIQUE(actor, ingress_key)
) STRICT;

CREATE INDEX IF NOT EXISTS events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS events_turn ON events(turn_id);
CREATE INDEX IF NOT EXISTS events_recorded ON events(recorded_at);

-- 全 record_family 共通ヘッダ＋完全スナップショット（state_json が権威写し）
CREATE TABLE IF NOT EXISTS records (
    scope TEXT NOT NULL,
    record_id TEXT NOT NULL,
    record_family TEXT NOT NULL,
    kind TEXT,
    status TEXT NOT NULL,
    content_hash TEXT,
    record_version INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    importance INTEGER,
    retrieval_strength REAL,
    last_recalled_at TEXT,
    epistemic_confidence REAL,
    param_policy_ref TEXT,
    valid_from TEXT,
    valid_to TEXT,
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    pinned INTEGER NOT NULL DEFAULT 0,
    tier TEXT NOT NULL DEFAULT 'hot',
    subject_json TEXT,
    provenance_json TEXT,
    attachments_json TEXT,
    migration_json TEXT,
    external_ref_json TEXT,
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_mutation_seq INTEGER NOT NULL,
    PRIMARY KEY (scope, record_id)
) STRICT;

CREATE INDEX IF NOT EXISTS records_family_status ON records(record_family, scope, status);
CREATE INDEX IF NOT EXISTS records_content_hash ON records(content_hash);
CREATE INDEX IF NOT EXISTS records_kind ON records(scope, record_family, kind);

CREATE TABLE IF NOT EXISTS candidates (
    scope TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    status TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    verdicts_json TEXT,
    review_json TEXT,
    suppression_key TEXT,
    suppression_until TEXT,
    suppression_lifted_by TEXT,
    producer TEXT,
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_mutation_seq INTEGER NOT NULL,
    PRIMARY KEY (scope, candidate_id)
) STRICT;

CREATE INDEX IF NOT EXISTS candidates_status ON candidates(scope, status);

CREATE TABLE IF NOT EXISTS tombstones (
    scope TEXT NOT NULL,
    tombstone_id TEXT NOT NULL,
    pattern_hash TEXT NOT NULL,
    pattern TEXT,
    pattern_kind TEXT,
    fingerprint_norm TEXT,
    embedding_space TEXT,
    derived_refs_json TEXT,
    reason TEXT,
    source_memory_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_mutation_seq INTEGER NOT NULL,
    PRIMARY KEY (scope, tombstone_id),
    UNIQUE (scope, pattern_hash)
) STRICT;

CREATE TABLE IF NOT EXISTS links (
    scope TEXT NOT NULL,
    link_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    link_type TEXT NOT NULL,
    blocking INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_mutation_seq INTEGER NOT NULL,
    PRIMARY KEY (scope, link_id)
) STRICT;

CREATE INDEX IF NOT EXISTS links_source ON links(scope, source_id);
CREATE INDEX IF NOT EXISTS links_target ON links(scope, target_id);

CREATE TABLE IF NOT EXISTS blobs (
    hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    kind TEXT,
    size INTEGER,
    media_meta_json TEXT,
    retention_class TEXT NOT NULL DEFAULT 'permanent',
    ref TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_mutation_seq INTEGER NOT NULL,
    PRIMARY KEY (scope, hash)
) STRICT;

-- provenance 逆引き junction（records.state_json.provenance.source_events の
-- 物化写し。browse_events 逆引き用。rebuild 可能な operational 区分）
CREATE TABLE IF NOT EXISTS record_source (
    scope TEXT NOT NULL,
    record_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    cite TEXT,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (scope, record_id, ordinal)
) STRICT;

CREATE INDEX IF NOT EXISTS record_source_event ON record_source(event_id);

-- ------------------------------------------------------------ registries

CREATE TABLE IF NOT EXISTS core_meta (
    key TEXT PRIMARY KEY,
    value TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS scope_registry (
    scope TEXT PRIMARY KEY,
    status TEXT NOT NULL,          -- active | reserved
    registered_seq INTEGER,
    registered_at TEXT NOT NULL,
    note TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS vocab_registry (
    vocab TEXT NOT NULL,
    value TEXT NOT NULL,
    content_digest TEXT,
    introduced_seq INTEGER,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (vocab, value)
) STRICT;

CREATE TABLE IF NOT EXISTS kernel_table (
    algo TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    params_json TEXT,
    registered_seq INTEGER,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (algo, version)
) STRICT;

CREATE TABLE IF NOT EXISTS policy_state (
    rule_id TEXT PRIMARY KEY,
    version TEXT,
    params_json TEXT,
    content_digest TEXT,
    activated_seq INTEGER,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS tool_ledger (
    tool_name TEXT PRIMARY KEY,
    returns_memory INTEGER NOT NULL DEFAULT 0,
    registered_at TEXT NOT NULL,
    note TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS migration_map (
    import_key TEXT PRIMARY KEY,
    source_table TEXT,
    source_id TEXT,
    record_id TEXT,
    scope TEXT,
    seq INTEGER,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS archive_segments (
    segment_id TEXT PRIMARY KEY,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    segment_hash TEXT NOT NULL,
    prev_segment_hash TEXT,
    location TEXT NOT NULL DEFAULT 'local',
    sealed_seq INTEGER NOT NULL,
    sealed_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    note TEXT
) STRICT;

-- ------------------------------------------------------------ operational
-- operational 区分（SD-32）: append-only 義務なし・export 除外既定・
-- rebuild 時の扱いは項目ごと宣言（いずれも rebuild 対象外/再生成可能）

CREATE TABLE IF NOT EXISTS watermarks (
    processor TEXT PRIMARY KEY,
    last_done_seq INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS extraction_attempts (
    processor TEXT NOT NULL,
    seq INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (processor, seq)
) STRICT;

CREATE TABLE IF NOT EXISTS derived_generations (
    gen INTEGER PRIMARY KEY,
    db_path TEXT,
    built_at TEXT,
    frozen_read_seq INTEGER,
    status TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0
) STRICT;

-- ------------------------------------------------------------ L-ING（P8-2）
-- 2相 ingress（write-path §2・transcript-ingress §4.1）。全て operational 区分。
-- Phase1（同期 1tx）: envelope 生保存→receipt→watermark 前進→queue enqueue。
-- Phase2（非同期）: normalize_queue → normalizer → writer.append_event。

-- 生 envelope の原本（再正規化 renormalize の源）。envelope_key が dedup 錨:
-- hash(adapter_id, stream_id, span_start, span_end, payload_digest)。
-- adapter_id が鍵成分に入るため adapter 跨ぎでは衝突しない（F-14）。
CREATE TABLE IF NOT EXISTS ingress_envelopes (
    envelope_key TEXT PRIMARY KEY,
    envelope_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version TEXT,
    format_version TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    span_json TEXT,
    coverage_json TEXT,
    payload_mode TEXT NOT NULL DEFAULT 'inline',
    payload_inline TEXT,
    locator_json TEXT,
    payload_digest TEXT NOT NULL,
    occurred_range_json TEXT,
    parts_json TEXT,
    received_at TEXT,
    recorded_at TEXT NOT NULL,
    seq INTEGER,
    status TEXT NOT NULL DEFAULT 'stored',  -- stored | quarantined | normalized
    detail_json TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS ingress_env_stream ON ingress_envelopes(adapter_id, stream_id);
CREATE INDEX IF NOT EXISTS ingress_env_status ON ingress_envelopes(status);

-- admission_report 型の append-only 監査（受理/dedup/quarantined/gap 宣言）
CREATE TABLE IF NOT EXISTS ingress_receipts (
    receipt_id TEXT PRIMARY KEY,
    envelope_key TEXT NOT NULL,
    envelope_id TEXT,
    batch_id TEXT,
    verdict TEXT NOT NULL,     -- received | dedup | quarantined | gap_declared
    detail_json TEXT,
    seq INTEGER,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS ingress_rcpt_env ON ingress_receipts(envelope_key);

-- Core 所有の消費位置（adapter×stream 単位）。可変行。
CREATE TABLE IF NOT EXISTS ingress_watermarks (
    adapter_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    cursor_kind TEXT NOT NULL DEFAULT 'seq',
    cursor_value TEXT,
    last_envelope_key TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (adapter_id, stream_id)
) STRICT;

-- Phase2 正規化キュー（QueueFS 型。state 遷移は更新、行は消さない）
CREATE TABLE IF NOT EXISTS normalize_queue (
    queue_id INTEGER PRIMARY KEY,
    envelope_key TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    state TEXT NOT NULL DEFAULT 'pending',  -- pending | leased | done | failed
    claim_token TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS nq_state ON normalize_queue(state, next_attempt_at);

-- 正規化 event ごとの下流行先（sonder 型 claim）。遷移は claim_events が権威。
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    processor TEXT NOT NULL,
    status TEXT NOT NULL,      -- claimed | stored | no_lesson | cancelled
    claim_token TEXT,
    leased_until TEXT,
    result_ref TEXT,
    detail TEXT,
    seq INTEGER,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS claims_status ON claims(status, leased_until);
CREATE INDEX IF NOT EXISTS claims_event ON claims(event_id);

-- claims 遷移の append-only 監査（「届いたが記憶にならなかった内訳」の権威）
CREATE TABLE IF NOT EXISTS claim_events (
    id INTEGER PRIMARY KEY,
    claim_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    ts TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS claim_events_claim ON claim_events(claim_id);

-- ------------------------------------------------------------ L-EXP（P8-6）
-- staged_* は ops/import の恒久設備（SD-50）。pack import の「staged →
-- test-apply → commit」の検疫領域。operational 区分（export 対象外）。
-- 行は status 遷移で管理し、rollback は run ごと status='rolled_back'
-- にする（物理削除しない）。

-- pack から読んだ event 行の検疫領域（row_json は canonical イベント行の
-- 完全写し —— seq/prev_hash/row_hash 込み。鎖は export 側で既に完結）
CREATE TABLE IF NOT EXISTS staged_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    row_json TEXT NOT NULL,
    status TEXT NOT NULL,      -- staged | applied | committed | failed
    PRIMARY KEY (run_id, seq)
) STRICT;

-- canonical 現在値表の検疫領域（table_name ∈ records/candidates/tombstones/
-- links/blobs/migration_map/archive_segments）
CREATE TABLE IF NOT EXISTS staged_records (
    run_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    scope TEXT NOT NULL,
    record_id TEXT NOT NULL,
    row_json TEXT NOT NULL,
    import_key TEXT,
    status TEXT NOT NULL,      -- staged | applied | committed | failed
    PRIMARY KEY (run_id, table_name, scope, record_id)
) STRICT;

-- registry 群の検疫領域（scope_registry/vocab_registry/kernel_table/
-- tool_ledger/policy_state/schema_migrations/core_meta）
CREATE TABLE IF NOT EXISTS staged_registry (
    run_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_json TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (run_id, table_name, row_json)
) STRICT;

-- permanent blob 実体の検疫領域（pack 内の相対 path を保持）
CREATE TABLE IF NOT EXISTS staged_blobs (
    run_id TEXT NOT NULL,
    hash TEXT NOT NULL,
    pack_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    status TEXT NOT NULL,      -- staged | committed | missing
    PRIMARY KEY (run_id, hash)
) STRICT;

-- import run の台帳（needs_repair の集計を receipt として露出 —— SD-68）
CREATE TABLE IF NOT EXISTS import_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,        -- full
    pack_digest TEXT,
    source_seq INTEGER,
    status TEXT NOT NULL,      -- staged | verified | committed | failed | rolled_back
    counts_json TEXT,
    manifest_json TEXT,
    needs_repair_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS staged_records_run ON staged_records(run_id, status);
CREATE INDEX IF NOT EXISTS staged_events_run ON staged_events(run_id, status);

-- L-MIG（P8-7）: 移行元の行ごとの配送台帳。staged_* と同じく検疫・工程の
-- 恒久設備だが、L-MIG 固有の工程表（SD-50）。import_key で冪等追跡し、
-- 「もう移したか」を run を跨いで判定できる。
CREATE TABLE IF NOT EXISTS deliver_state (
    import_key TEXT PRIMARY KEY,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target TEXT NOT NULL,        -- events | records | blob | skip | quarantine
    target_id TEXT,
    status TEXT NOT NULL,        -- pending | staged | delivered | quarantined
                                 -- | skipped | failed | rolled_back
    detail_json TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS deliver_state_run ON deliver_state(run_id, status);
CREATE INDEX IF NOT EXISTS deliver_state_source
    ON deliver_state(source_table, status);

-- L-TIER（P8-8）: scope 別 tier policy（registry）と cycle 評価の滞留値
-- （operational）。tier_eval は hysteresis の「連続 N cycle」計数を担う
-- —— 揮発値ではなく判定材料なので record state には載せない。
CREATE TABLE IF NOT EXISTS tier_policies (
    scope TEXT PRIMARY KEY,
    policy_json TEXT NOT NULL CHECK (json_valid(policy_json)),
    policy_digest TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS tier_eval (
    scope TEXT NOT NULL,
    record_id TEXT NOT NULL,
    desired_tier TEXT NOT NULL,
    streak INTEGER NOT NULL DEFAULT 0,
    last_cycle TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, record_id)
) STRICT;

CREATE TABLE IF NOT EXISTS tier_cycles (
    cycle_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    ran_at TEXT NOT NULL,
    counts_json TEXT,
    PRIMARY KEY (cycle_id, scope)
) STRICT;
"""

# ------------------------------------------------------- append-only 強制

# UPDATE/DELETE 両方を禁止する表（純粋 append-only: ログ・封印台帳・移行写像・
# ingress 監査 receipt・claim 遷移履歴）
NO_UPDATE_DELETE = (
    "events", "archive_segments", "migration_map",
    "ingress_receipts", "claim_events",
)
# DELETE のみ禁止する表（canonical 現在値・registry・L-ING 作業面。
# UPDATE は mutation apply / 水位前進 / queue・claim 遷移が使う）
NO_DELETE = (
    "records", "candidates", "tombstones", "links", "blobs",
    "scope_registry", "vocab_registry", "kernel_table",
    "tool_ledger", "policy_state", "schema_migrations", "core_meta",
    "ingress_envelopes", "ingress_watermarks", "normalize_queue", "claims",
    "deliver_state",
)


def trigger_ddl() -> str:
    out = []
    for t in NO_UPDATE_DELETE:
        out.append(
            f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}\n"
            f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;\n"
            f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}\n"
            f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
        )
    for t in NO_DELETE:
        out.append(
            f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}\n"
            f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
        )
    return "\n".join(out)


# ----------------------------------------------------------- 表の格納区分
# export allowlist（core-api §6・SD-29/37）は canonical*＋registry。
# operational は除外（watermark_hint の任意同梱は区別名で宣言）。

TABLE_CLASS = {
    "events": "canonical_log",
    "archive_segments": "canonical_log",
    "migration_map": "canonical_log",
    "records": "canonical",
    "candidates": "canonical",
    "tombstones": "canonical",
    "links": "canonical",
    "blobs": "canonical",
    "scope_registry": "registry",
    "vocab_registry": "registry",
    "kernel_table": "registry",
    "tool_ledger": "registry",
    "policy_state": "registry",
    "schema_migrations": "registry",
    "core_meta": "registry",
    "record_source": "operational",
    "watermarks": "operational",
    "extraction_attempts": "operational",
    "derived_generations": "operational",
    # L-ING（FA §3.1: operational 区分 —— export 除外既定・可変行許容）
    "ingress_envelopes": "operational",
    "ingress_receipts": "operational",
    "ingress_watermarks": "operational",
    "normalize_queue": "operational",
    "claims": "operational",
    "claim_events": "operational",
    # L-EXP（P8-6: staged_* は ops/import の検疫領域 —— export 対象外）
    "staged_events": "operational",
    "staged_records": "operational",
    "staged_registry": "operational",
    "staged_blobs": "operational",
    "import_runs": "operational",
    # L-MIG（P8-7: 移行工程の台帳 —— export 対象外。恒久的な provenance は
    # migration_map と record の import_key が担う）
    "deliver_state": "operational",
    # L-TIER（P8-8: policy は registry＝export 対象。eval/cycle 滞留値は
    # operational で export 対象外 —— 再評価で再導出可能）
    "tier_policies": "registry",
    "tier_eval": "operational",
    "tier_cycles": "operational",
}

# authorizer が UPDATE を許す区分/表:
#   canonical 現在値表（records 等）は mutation apply の upsert が UPDATE を使う。
#   events/archive_segments/migration_map は UPDATE 不可（trigger と二重に防御）。
AUTHORIZER_NO_UPDATE = frozenset(NO_UPDATE_DELETE)
AUTHORIZER_NO_DELETE = frozenset(NO_UPDATE_DELETE + NO_DELETE)

EXPECTED_TABLES = frozenset(TABLE_CLASS)
