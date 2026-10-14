"""kernel.writer —— canonical (data/core.db) への唯一の書込み接続を所有する不純端

kernel パッケージ内で sqlite3・時計・乱数・ファイル IO を使えるのはこの
モジュールのみ（tests/test_kernel_purity.py の AST lint が他 kernel ファイルの
同種 import を機械拒否する）。第二書込み路を作らないための構造:

- この接続は bootstrap 時に PRAGMA journal_mode=WAL・synchronous=FULL・
  foreign_keys=ON を張り、最後に set_authorizer で DDL/ATTACH/保護表への
  UPDATE・DELETE を接続レベルで拒否する（authorizer は接続ごとの防御）。
- append-only は BEFORE UPDATE/DELETE トリガーでも強制（他接続からの
  素通し UPDATE/DELETE も塞ぐ）。
- seq は writer が tx 内で MAX+1 を発行（全順序の担い手は seq）。
- row_hash は prev_hash 連鎖の hash 鎖（SD-04）。payload 本体ではなく
  payload_hash に commit する（M-14）。
- 時計・乱数はこのモジュールが注入する。テスト用に clock/rand を差し替え可能。

sanity_check は起動 sanity（integrity_check・manifest pin・鎖検査・
gap-free 検査・期待表集合）を担い、validate の検査項にも載る（SD-61）。
"""

import contextlib
import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import canon, events, fold, ids, schema, vocab

KERNEL_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = KERNEL_DIR / "manifest.json"


class StoreCorruptError(RuntimeError):
    """起動 sanity・鎖検査での破損/改竄検出（fail-loud）。"""


class IngressConflictError(ValueError):
    """同 (actor, ingress_key) で異内容の再送（SD-65: reject＋運用通知）。"""


class MutationError(ValueError):
    """mutation payload・record_version 連続性・scope 登録の違反。"""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------- authorizer
# 接続レベルの防御。bootstrap 完了後に install する（DDL は install 前に流す）。

_DENY_PRAGMAS = {"writable_schema", "journal_mode", "synchronous",
                 "foreign_keys", "defer_foreign_keys", "trusted_schema"}

_DENY_ACTIONS = {
    sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH, sqlite3.SQLITE_REINDEX, sqlite3.SQLITE_ANALYZE,
    sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE,
}

_FAMILY_TABLE = {
    "candidate": ("candidates", "candidate_id"),
    "tombstone": ("tombstones", "tombstone_id"),
    "link": ("links", "link_id"),
    "blob": ("blobs", "hash"),
}


def _authorizer(action, arg1, arg2, _db, _src):
    if action in _DENY_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_UPDATE:
        if arg1 in schema.AUTHORIZER_NO_UPDATE:
            return sqlite3.SQLITE_DENY
    elif action == sqlite3.SQLITE_DELETE:
        if arg1 in schema.AUTHORIZER_NO_DELETE:
            return sqlite3.SQLITE_DENY
    elif action == sqlite3.SQLITE_PRAGMA:
        if arg1 and arg1.lower() in _DENY_PRAGMAS and arg2 is not None:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


# --------------------------------------------------------------- bootstrap

_SEED_SCOPES = [
    ("user", "active", "初期 scope（canonical-schema §7）"),
    ("agent", "active", "初期 scope（canonical-schema §7）"),
    ("shared", "reserved", "予約 scope（未使用・登録制）"),
    ("person:<id>", "reserved", "予約 namespace（person:<id> 接頭辞）"),
]


def _apply_ddl(conn):
    conn.executescript(schema.DDL)
    conn.executescript(schema.trigger_ddl())


def _seed(conn, now_ts: str):
    meta = [
        ("serializer_version", canon.SERIALIZER_VERSION),
        ("fold_version", fold.FOLD_VERSION),
        ("genesis_hash", events.GENESIS_HASH),
        ("created_at", now_ts),
        ("event_vocab_hash", vocab.vocab_hash()),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO core_meta(key, value) VALUES (?,?)", meta)
    # schema_version は最新版へ前進させる（版更新の刻印は schema_migrations）
    conn.execute(
        "INSERT INTO core_meta(key, value) VALUES('schema_version', ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(schema.SCHEMA_VERSION),))
    for scope, status, note in _SEED_SCOPES:
        conn.execute(
            "INSERT OR IGNORE INTO scope_registry(scope, status,"
            " registered_seq, registered_at, note) VALUES (?,?,?,?,?)",
            (scope, status, 0, now_ts, note))
    for algo, ver in (("canon", canon.SERIALIZER_VERSION),
                      ("fold", fold.FOLD_VERSION)):
        conn.execute(
            "INSERT OR IGNORE INTO kernel_table(algo, version, digest,"
            " params_json, registered_seq, registered_at)"
            " VALUES (?,?,?,?,?,?)",
            (algo, ver, canon.hash_obj("pcore.kernel_table.v1",
                                       [algo, ver]), None, 0, now_ts))
    for ver, note in ((1, "P8-1 中核スキーマ初期適用"),
                      (2, "P8-2 L-ING 群追加（ingress_*/normalize_queue/claims)"),
                      (3, "P8-6 L-EXP 群追加（staged_*/import_runs)"),
                      (4, "P8-7 L-MIG 群追加（deliver_state)"),
                      (5, "P8-8 L-TIER 群追加（tier_*)")):
        if ver <= schema.SCHEMA_VERSION:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations("
                "version, applied_at, note) VALUES (?,?,?)",
                (ver, now_ts, note))


def scope_allowed(conn, scope: str) -> bool:
    """scope_registry 登録制・未登録は fail-closed（canonical-schema §7）。

    status='active' の登録 scope のみ受理。reserved（shared・person:<id>
    名前空間）は将来の予約で、現段階では書込みを受理しない。
    """
    if not isinstance(scope, str):
        return False
    row = conn.execute(
        "SELECT status FROM scope_registry WHERE scope=?", (scope,)
    ).fetchone()
    return bool(row and row[0] == "active")


class CoreWriter:
    """core.db の唯一の rw 接続所有者。append/apply_mutation は lock+tx で
    直列化し、seq 発行と hash 鎖を単一主体で進める。"""

    def __init__(self, path, *, clock=None, rand=None, sanity=True,
                 maintenance=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or _now_utc
        self._rand = rand or secrets.randbits
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._bootstrap()
        if maintenance:
            return
        report = self.sanity_check()
        if sanity and not report["ok"]:
            self._conn.close()
            raise StoreCorruptError(
                "core.db sanity failed: " + "; ".join(
                    f"{c['name']}: {c['detail']}"
                    for c in report["checks"] if not c["ok"]))
        self._conn.set_authorizer(_authorizer)

    @contextlib.contextmanager
    def _tx(self):
        """入子対応 tx。最外層のみ commit/rollback を担う（内側は裸 conn を
        返すだけ）。`with self._conn` は入子で途中 commit するため使わない。
        呼び出し側が self._lock を保持している前提。"""
        outer = self._tx_depth == 0
        self._tx_depth += 1
        try:
            yield self._conn
            self._tx_depth -= 1
            if outer:
                self._conn.commit()
        except BaseException:
            self._tx_depth -= 1
            if outer:
                self._conn.rollback()
            raise

    # ---------------------------------------------------------- bootstrap
    def _bootstrap(self):
        with self._tx():
            _apply_ddl(self._conn)
            _seed(self._conn, canon.canon_ts(self._clock()))

    # ------------------------------------------------------------ helpers
    def _tail(self):
        return self._conn.execute(
            "SELECT seq, row_hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()

    def _peek_next_seq(self) -> int:
        """次に発行される events.seq の予告値（receipt 等の位置刻印用。
        seq を消費しない）。"""
        tail = self._tail()
        return (tail["seq"] + 1) if tail else 1

    def _insert_event(self, ev: dict):
        self._conn.execute(
            "INSERT INTO events(seq, event_id, event_type, occurred_at,"
            " recorded_at, actor, session_json, session_channel, session_id,"
            " payload_json, attachments_json, redacted_json, correlation_json,"
            " turn_id, source_meta_json, ingress_key, prev_hash, row_hash,"
            " canon_algo, migration_json) VALUES"
            " (:seq,:event_id,:event_type,:occurred_at,:recorded_at,:actor,"
            " :session_json,:session_channel,:session_id,:payload_json,"
            " :attachments_json,:redacted_json,:correlation_json,:turn_id,"
            " :source_meta_json,:ingress_key,:prev_hash,:row_hash,:canon_algo,"
            " :migration_json)",
            ev)

    def _dedup_check(self, actor, ingress_key, event_type,
                     payload_json, occurred_at):
        """(actor, ingress_key) の既受理行を照合（SD-16/SD-65）。

        同内容なら既受理行の dict、異内容なら IngressConflictError、
        未受理なら None。"""
        if ingress_key is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM events WHERE actor=? AND ingress_key=?",
            (actor, ingress_key)).fetchone()
        if row is None:
            return None
        same = (row["event_type"] == event_type
                and row["payload_json"] == payload_json
                and row["occurred_at"] == occurred_at)
        if not same:
            raise IngressConflictError(
                f"ingress_key conflict: actor={actor} key={ingress_key}"
                " already accepted with different content")
        return dict(row)

    def _append_locked(self, event_type, payload, *, actor, occurred_at=None,
                       session=None, correlation=None, source_meta=None,
                       ingress_key=None, attachments=None, redacted=None,
                       migration=None) -> dict:
        """append の実体。呼び出し側が lock と tx を保持している前提。"""
        payload_json = canon.dumps(payload)
        dup = self._dedup_check(actor, ingress_key, event_type,
                                payload_json, occurred_at)
        if dup is not None:
            return {"accepted": True, "dedup": True,
                    "event_id": dup["event_id"], "seq": dup["seq"]}

        now = self._clock()
        recorded_at = canon.canon_ts(now)
        tail = self._tail()
        seq = (tail["seq"] + 1) if tail else 1
        prev = tail["row_hash"] if tail else events.GENESIS_HASH
        ev = events.build_event(
            event_id=ids.event_id(int(now.timestamp() * 1000),
                                  self._rand(80)),
            seq=seq, event_type=event_type, actor=actor,
            recorded_at=recorded_at, payload=payload,
            prev_hash=prev, occurred_at=occurred_at,
            session=session, attachments=attachments,
            redacted=redacted, correlation=correlation,
            source_meta=source_meta, ingress_key=ingress_key,
            migration=migration)
        errs = events.check_event(ev)
        if errs:
            raise events.EventError("invalid event: " + "; ".join(errs))
        self._insert_event(ev)
        return {"accepted": True, "dedup": False, "event_id": ev["event_id"],
                "seq": seq, "recorded_at": recorded_at,
                "row_hash": ev["row_hash"], "prev_hash": prev}

    def _stored_version(self, family, scope, rid):
        """格納済み record の現在 record_version（無ければ None）。

        全表とも state_json（完全スナップショット）の値が権威。"""
        table, keycol = _FAMILY_TABLE.get(family, ("records", "record_id"))
        row = self._conn.execute(
            f"SELECT state_json FROM {table} WHERE scope=? AND {keycol}=?",
            (scope, rid)).fetchone()
        if row is None:
            return None
        return canon.loads(row["state_json"]).get("record_version")

    def _upsert(self, table: str, row: dict):
        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        pks = {
            "records": "scope, record_id",
            "candidates": "scope, candidate_id",
            "tombstones": "scope, tombstone_id",
            "links": "scope, link_id",
            "blobs": "scope, hash",
        }[table]
        pkcols = {c.strip() for c in pks.split(",")}
        sets = ", ".join(f"{c}=excluded.{c}" for c in row
                         if c not in pkcols)
        self._conn.execute(
            f"INSERT INTO {table}({cols}) VALUES({marks})"
            f" ON CONFLICT({pks}) DO UPDATE SET {sets}", row)

    def _refresh_record_source(self, st: dict):
        """record_source junction の物化更新（operational 区分・再生成可能）。"""
        self._conn.execute(
            "DELETE FROM record_source WHERE scope=? AND record_id=?",
            (st["scope"], st["record_id"]))
        src = (st.get("provenance") or {}).get("source_events") or []
        for i, e in enumerate(src):
            if not isinstance(e, dict) or "event_id" not in e:
                continue
            self._conn.execute(
                "INSERT INTO record_source(scope, record_id, event_id,"
                " cite, ordinal) VALUES (?,?,?,?,?)",
                (st["scope"], st["record_id"], e["event_id"],
                 e.get("cite"), i))

    # ------------------------------------------------------------- public
    @contextlib.contextmanager
    def transaction(self):
        """複数操作を同一 tx・lock 配下で束ねる adapter 向け口（RLock 入子可）。

        canonical の書込み接続はこの writer が唯一の所有者であり続ける
        （第二接続を作らせないための口）。中で append_event/apply_mutation
        等の公開 API を呼んでもよい（_tx が入子で最外層のみ commit する）。
        """
        with self._lock:
            with self._tx() as conn:
                yield conn

    def append_event(self, event_type: str, payload, *, actor: str,
                     occurred_at=None, session=None, correlation=None,
                     source_meta=None, ingress_key=None, attachments=None,
                     redacted=None, migration=None) -> dict:
        """Event Log への append。receipt を返す。

        同 (actor, ingress_key) で同内容の再送は冪等受理（dedup=True）。
        異内容の再送は IngressConflictError（SD-65）。
        """
        with self._lock:
            with self._tx():
                return self._append_locked(
                    event_type, payload, actor=actor,
                    occurred_at=occurred_at, session=session,
                    correlation=correlation, source_meta=source_meta,
                    ingress_key=ingress_key, attachments=attachments,
                    redacted=redacted, migration=migration)

    def apply_mutation(self, event_type: str, payload: dict, *, actor: str,
                       occurred_at=None, session=None, correlation=None,
                       source_meta=None, ingress_key=None) -> dict:
        """mutation イベントの append＋表への upsert を1txで行う。

        record_version の連続性（新規=1・既存=prev+1）と scope_registry
        登録（fail-closed）を writer 側で機械担保する。
        """
        if event_type not in vocab.MUTATION_EVENT_TYPES:
            raise MutationError(f"not a mutation type: {event_type}")
        errs = events.check_mutation_payload(event_type, payload)
        if errs:
            raise MutationError("; ".join(errs))
        st = payload["new_state"]
        family = ("memory" if event_type in ("memory_write", "memory_update")
                  else payload["record_family"])
        rid = st["record_id"]
        scope = st["scope"]

        with self._lock:
            with self._tx():
                # 冪等再送は検査列より先に判定する（同一効果の再適用は不要）
                dup = self._dedup_check(
                    actor, ingress_key, event_type,
                    canon.dumps(payload), occurred_at)
                if dup is not None:
                    return {"accepted": True, "dedup": True,
                            "event_id": dup["event_id"], "seq": dup["seq"]}
                if not scope_allowed(self._conn, scope):
                    raise MutationError(
                        f"scope not registered (fail-closed): {scope}")
                expect = (v + 1) if (v := self._stored_version(
                    family, scope, rid)) else 1
                if st["record_version"] != expect:
                    raise MutationError(
                        f"record_version must be {expect}, got"
                        f" {st['record_version']}")

                receipt = self._append_locked(
                    event_type, payload, actor=actor,
                    occurred_at=occurred_at, session=session,
                    correlation=correlation, source_meta=source_meta,
                    ingress_key=ingress_key)
                table, row = fold.row_for(family, st, receipt["seq"])
                self._upsert(table, row)
                self._refresh_record_source(st)
                return receipt

    # ------------------------------------------------------------- verify
    def verify_chain(self, upto_seq=None) -> dict:
        """hash 鎖の全走査検証（挿入・抜き取り・書換えを検出）。O(n)。"""
        rows = self._conn.execute(
            "SELECT seq, event_type, recorded_at, payload_json, prev_hash,"
            " row_hash FROM events ORDER BY seq").fetchall()
        prev = events.GENESIS_HASH
        bad = None
        n = 0
        for r in rows:
            if upto_seq is not None and r["seq"] > upto_seq:
                break
            if r["seq"] != n + 1:
                bad = {"seq": r["seq"], "issue": "gap"}
                break
            phash = events.payload_hash(canon.loads(r["payload_json"]))
            rh = events.row_hash(r["seq"], r["event_type"],
                                 r["recorded_at"], prev, phash)
            if rh != r["row_hash"] or r["prev_hash"] != prev:
                bad = {"seq": r["seq"], "issue": "hash_mismatch"}
                break
            prev = r["row_hash"]
            n += 1
        return {"ok": bad is None, "verified": n, "bad": bad,
                "head": prev if n else None}

    def replay_check(self, upto_seq=None) -> dict:
        """events → fold → 格納表との照合（evolution §7 の replay 検証）。"""
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY seq").fetchall()
        if upto_seq is not None:
            rows = [r for r in rows if r["seq"] <= upto_seq]
        res = fold.fold_rows(rows)
        folded = res["state"]

        stored = {}
        for t, keycol in (("records", "record_id"),
                          ("candidates", "candidate_id"),
                          ("tombstones", "tombstone_id"),
                          ("links", "link_id"),
                          ("blobs", "hash")):
            for r in self._conn.execute(
                    f"SELECT scope, {keycol} AS rid, state_json,"
                    f" last_mutation_seq FROM {t}"):
                st = canon.loads(r["state_json"])
                stored[(st["record_family"], r["scope"], r["rid"])] = (
                    st, r["last_mutation_seq"])

        diffs = fold.diff_states(folded, stored)
        return {
            "ok": not diffs,
            "digest": fold.replay_digest(folded),
            "stored_digest": fold.replay_digest(stored),
            "diffs": diffs[:50],
            "counters": res["counters"],
            "unhandled_event_types": res["unhandled_event_types"],
        }

    def sanity_check(self) -> dict:
        """起動 sanity（SD-61 の validate 検査項のうち DB 面）。

        integrity_check・期待表集合・manifest pin・鎖の整合・
        gap-free 連番検査を行い report dict を返す（raise は呼び出し側）。"""
        checks = []

        def add(name, ok, detail=""):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        try:
            ic = self._conn.execute("PRAGMA integrity_check").fetchone()
            add("integrity_check", ic and ic[0] == "ok", ic[0] if ic else "")
        except sqlite3.Error as e:
            add("integrity_check", False, str(e))

        tables = {r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = schema.EXPECTED_TABLES - tables
        extra = tables - schema.EXPECTED_TABLES - {"sqlite_sequence"}
        add("tables_present", not missing, f"missing={sorted(missing)}")
        add("no_extra_tables", not extra, f"extra={sorted(extra)}")

        mv = self._conn.execute(
            "SELECT value FROM core_meta WHERE key='schema_version'"
        ).fetchone()
        add("schema_version", mv and mv[0] == str(schema.SCHEMA_VERSION),
            mv[0] if mv else "missing")

        m = verify_manifest()
        add("manifest_pin", m["ok"], m.get("detail", ""))

        v = self.verify_chain()
        add("hash_chain", v["ok"], v.get("bad") or f"verified={v['verified']}")

        return {"ok": all(c["ok"] for c in checks), "checks": checks,
                "head": v.get("head")}

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ------------------------------------------------------- read-only & 起点

def connect_ro(path) -> sqlite3.Connection:
    """canonical の読み専用接続（照会・fold 検証用）。書込みは writer のみ。"""
    p = Path(path).resolve()
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def open_core(path, **kw) -> CoreWriter:
    return CoreWriter(path, **kw)


def compute_manifest(kernel_dir=None) -> dict:
    """kernel/*.py の sha256 pin 集合を生成（manifest.json の材料）。"""
    kdir = Path(kernel_dir) if kernel_dir else KERNEL_DIR
    files = {}
    for f in sorted(kdir.glob("*.py")):
        files[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    return {"version": 1, "algo": "sha256",
            "serializer": canon.SERIALIZER_VERSION, "files": files}


def verify_manifest(kernel_dir=None) -> dict:
    """kernel/*.py の sha256 pin 照合（コード完全性 pin）。"""
    kdir = Path(kernel_dir) if kernel_dir else KERNEL_DIR
    mpath = kdir / "manifest.json"
    if not mpath.exists():
        return {"ok": False, "detail": "manifest.json missing"}
    try:
        manifest = canon.loads(mpath.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "detail": f"manifest unreadable: {e}"}
    expected = manifest.get("files", {})
    actual = compute_manifest(kdir)["files"]
    if set(expected) != set(actual):
        return {"ok": False,
                "detail": f"file set drift: manifest={sorted(expected)}"
                          f" actual={sorted(actual)}"}
    bad = [k for k in expected if expected[k] != actual[k]]
    if bad:
        return {"ok": False, "detail": f"content drift: {bad}"}
    return {"ok": True, "detail": f"{len(actual)} files pinned"}
