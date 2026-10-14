"""adapters.derived —— derived.{gen}.db の構築・世代管理（derived-rebuild.md）

canonical（core.db）から再生成できる写しを別 DB ファイルに建てる。
generation ポインタは core.db の `derived_generations` 表が権威（rename
ではなく is_active UPDATE で切替 —— SD-52）。

表（index_status で件数・digest・model_stamp を照合可能にする）:
  derived_meta   gen/frozen_read_seq/canonical_digest/embedding_space/vec0
  mem_search     record_id, scope, status, norm_text, content_hash,
                 tokenized_text（SV-B 強化経路の shadow 列）, source_seq
  mem_fts        FTS5 trigram（external-content → mem_search.rowid）
  mem_vec        vec0 float[N] cosine + scope PARTITION KEY + record_id aux
                 （vec0 不在時は mem_vec_blob + アプリ側 cosine の degraded）
  mem_vec_blob   degraded 経路用のベクトル実体
  tomb_emb       tombstone 類似照合用 embedding（照合専用・SD-43）
  edges          link 族の graph index
  entities       entity 腕の索引（scope, entity_norm, record_id —— C-4。
                 明示 entities フィールド＋決定的表面抽出の写し）
  index_status   索引名 → built_seq/rows_count/digest/model_stamp

frozen read point（SD-21）: rebuild は宣言した seq で読みを凍結し、
流入分は次世代が拾う。
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..kernel import canon

INDEX_NAMES = ("mem_search", "mem_fts", "mem_vec", "tomb_emb", "edges",
               "entities")

_DDL = """
CREATE TABLE IF NOT EXISTS derived_meta (
    key TEXT PRIMARY KEY, value TEXT) STRICT;

CREATE TABLE IF NOT EXISTS mem_search (
    record_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'hot',
    norm_text TEXT NOT NULL,
    content_hash TEXT,
    tokenized_text TEXT,
    source_seq INTEGER NOT NULL
) STRICT;

CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
    norm_text, content='mem_search', content_rowid='rowid',
    tokenize='trigram');

CREATE TABLE IF NOT EXISTS mem_vec_blob (
    scope TEXT NOT NULL,
    record_id TEXT NOT NULL,
    vec_json TEXT NOT NULL,
    dims INTEGER NOT NULL,
    PRIMARY KEY (scope, record_id)
) STRICT;

CREATE TABLE IF NOT EXISTS tomb_emb (
    tombstone_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    norm_text TEXT NOT NULL,
    vec_json TEXT,
    source_seq INTEGER NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS edges (
    scope TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    link_type TEXT NOT NULL,
    blocking INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, source_id, target_id, link_type)
) STRICT;

CREATE TABLE IF NOT EXISTS entities (
    scope TEXT NOT NULL,
    entity_norm TEXT NOT NULL,
    record_id TEXT NOT NULL,
    PRIMARY KEY (scope, entity_norm, record_id)
) STRICT;
CREATE INDEX IF NOT EXISTS entities_lookup ON entities(scope, entity_norm);
CREATE INDEX IF NOT EXISTS entities_record ON entities(record_id);

CREATE TABLE IF NOT EXISTS index_status (
    name TEXT PRIMARY KEY,
    built_seq INTEGER NOT NULL,
    rows_count INTEGER NOT NULL,
    digest TEXT NOT NULL,
    model_stamp TEXT,
    built_at TEXT NOT NULL
) STRICT;
"""


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


def vec0_available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
        return True
    except ImportError:
        return False


def _trigram_text(s: str) -> str:
    """trigram FTS5 は tokenizer が分解を担うので、索引側は norm_text を
    そのまま入れる。クエリ側の OR 分解は supply._fts_terms が担当。"""
    return canon.norm_text(s or "")


def embed_source_text(st: dict) -> str:
    """embedding 入力文。人間可読の日付を本文の前に置き、時間認識を
    ベクトルに混ぜる（Hindsight の temporal awareness 相当）。

    日付源は occurred_at（出来事の時刻）> valid_from（有効開始）>
    created_at（記録時刻）の優先。日付が取れなければ本文のみ。
    CANONICAL_EMBED_DATES=0/off/false で無効化。
    """
    base = st.get("content") or st.get("text") or ""
    if os.getenv("CANONICAL_EMBED_DATES", "1").strip() in \
            ("0", "off", "false"):
        return base
    when = (st.get("occurred_at") or st.get("valid_from")
            or st.get("created_at") or "")
    day = when[:10] if len(when) >= 10 else ""
    return f"{day} {base}" if day else base


class DerivedStore:
    """derived.{gen}.db の構築・世代ポインタ・増分索引を司る adapter。"""

    def __init__(self, writer, dir=None, *, clock=None):
        self.writer = writer
        self.dir = Path(dir) if dir else writer.path.parent
        self._clock = clock or _now
        self.vec0 = vec0_available()

    # --------------------------------------------------------- 接続
    def _connect(self, path: Path) -> sqlite3.Connection:
        con = sqlite3.connect(str(path))
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        if self.vec0:
            import sqlite_vec
            con.enable_load_extension(True)
            sqlite_vec.load(con)
            con.enable_load_extension(False)
        return con

    def active(self):
        """(gen, path) —— active な derived 世代。無ければ (None, None)。"""
        row = self.writer._conn.execute(
            "SELECT gen, db_path FROM derived_generations"
            " WHERE is_active=1").fetchone()
        if row is None:
            return None, None
        return row["gen"], Path(row["db_path"])

    def open_active(self):
        """(conn, gen) —— active derived への読み取り接続。無ければ None。"""
        gen, path = self.active()
        if gen is None or not path.exists():
            return None, gen
        return self._connect(path), gen

    # --------------------------------------------------------- 構築
    FAMILIES = ("memories", "tombstones", "links")

    def rebuild(self, *, embedder=None, targets=None) -> dict:
        """canonical 全走査 → 新世代 derived を shadow build → 検証 → 切替。

        読み取りは frozen_read_seq（build 開始時点の head）で凍結。
        canonical 本体は一切触らない（書込みは derived_generations 表と
        `migration` marker イベントのみ）。

        targets: FAMILIES の部分集合を渡すと family 単位の部分 rebuild
        になる（Q-O48）。部分 build では対象外 index は空のまま残り、
        verify も対象分のみ行う。世代は部分でも is_active=1 に切替できる
        （未対象 index は callers が「空」を見て扱う）。
        """
        targets = tuple(targets or self.FAMILIES)
        unknown = set(targets) - set(self.FAMILIES)
        if unknown:
            raise ValueError(f"unknown rebuild targets: {sorted(unknown)}")
        now = self._clock()
        with self.writer.transaction() as conn:
            head = conn.execute(
                "SELECT MAX(seq) FROM events").fetchone()[0] or 0
            gen = (conn.execute(
                "SELECT MAX(gen) FROM derived_generations"
            ).fetchone()[0] or 0) + 1
            path = self.dir / f"derived.{gen}.db"
            conn.execute(
                "INSERT INTO derived_generations(gen, db_path, built_at,"
                " frozen_read_seq, status, is_active) VALUES"
                " (?,?,?,?,'building',0)",
                (gen, str(path), now, head))

        rows = self._scan_canonical(head, targets)
        dcon = self._connect(path)
        try:
            dcon.executescript(_DDL)
            counts = {}
            if "memories" in targets:
                counts.update(self._build_mem(dcon, rows["memories"],
                                              embedder, head))
                counts.update(
                    self._build_entities(dcon, rows["memories"], head))
            if "tombstones" in targets:
                counts.update(self._build_tomb(dcon, rows["tombstones"],
                                               embedder, head))
            if "links" in targets:
                counts.update(self._build_edges(dcon, rows["links"], head))
            stamp = json.dumps(
                embedder.model_stamp(), ensure_ascii=False,
                sort_keys=True) if embedder else None
            meta = {
                "gen": gen, "frozen_read_seq": head,
                "built_at": now,
                "canonical_digest": self._canonical_digest(head),
                "embedding_space": embedder.space if embedder else None,
                "vec0": "1" if self.vec0 else "0",
                "targets": json.dumps(list(targets)),
            }
            dcon.executemany(
                "INSERT INTO derived_meta(key, value) VALUES (?,?)",
                meta.items())
            for name, cnt in counts.items():
                dcon.execute(
                    "INSERT INTO index_status(name, built_seq, rows_count,"
                    " digest, model_stamp, built_at) VALUES (?,?,?,?,?,?)",
                    (name, head, cnt["rows"], cnt["digest"], stamp, now))
            dcon.commit()
            verify = self._verify(dcon, rows, targets)
        finally:
            dcon.close()

        with self.writer.transaction() as conn:
            status = "active" if verify["ok"] else "failed"
            conn.execute(
                "UPDATE derived_generations SET is_active=0"
                " WHERE is_active=1")
            conn.execute(
                "UPDATE derived_generations SET status=?, is_active=?"
                " WHERE gen=?",
                (status, 1 if verify["ok"] else 0, gen))
            self.writer._append_locked(
                "migration",
                {"action": "rebuild_indexes", "gen": gen,
                 "frozen_read_seq": head, "verify": verify,
                 "targets": list(targets),
                 "db_path": str(path)},
                actor="system")
        return {"gen": gen, "path": str(path), "frozen_read_seq": head,
                "targets": list(targets),
                "verify": verify, "active": verify["ok"]}

    def _scan_canonical(self, upto_seq: int,
                        targets=None) -> dict:
        """frozen_read_seq 時点の canonical 読み取り（targets で絞込み）。"""
        targets = set(targets or self.FAMILIES)
        conn = self.writer._conn
        memories = [canon.loads(r["state_json"]) for r in conn.execute(
            "SELECT state_json FROM records WHERE record_family='memory'"
            " AND last_mutation_seq<=?", (upto_seq,))] \
            if "memories" in targets else []
        tombs = [canon.loads(r["state_json"]) for r in conn.execute(
            "SELECT state_json FROM tombstones"
            " WHERE last_mutation_seq<=?", (upto_seq,))] \
            if "tombstones" in targets else []
        links = [canon.loads(r["state_json"]) for r in conn.execute(
            "SELECT state_json FROM links WHERE last_mutation_seq<=?",
            (upto_seq,))] if "links" in targets else []
        return {"memories": memories, "tombstones": tombs, "links": links}

    def _canonical_digest(self, upto_seq: int) -> str:
        conn = self.writer._conn
        parts = []
        for table, keycol in (("records", "record_id"),
                              ("candidates", "candidate_id"),
                              ("tombstones", "tombstone_id"),
                              ("links", "link_id"), ("blobs", "hash")):
            for r in conn.execute(
                    f"SELECT scope, {keycol} k, state_json FROM {table}"
                    " WHERE last_mutation_seq<=? ORDER BY scope, k",
                    (upto_seq,)):
                parts.append([table, r["scope"], r["k"], r["state_json"]])
        return canon.hash_obj("pcore.derived_base.v1", parts)

    def _build_mem(self, dcon, memories, embedder, head) -> dict:
        """mem_search + mem_fts + mem_vec（or blob 退避）。

        index membership（FA §6.2）: cue 索引（mem_search/mem_fts）は
        全 tier を載せる（deep_recall の発見面）が、vec0 メンバーシップは
        hot/warm のみ —— cold/archive は既定 recall の空間を汚さない。"""
        texts, metas, vec_metas = [], [], []
        for st in memories:
            rid, scope = st["record_id"], st["scope"]
            tr = st.get("tier") or "hot"
            text = st.get("content") or st.get("text") or ""
            dcon.execute(
                "INSERT INTO mem_search(record_id, scope, status, tier,"
                " norm_text, content_hash, tokenized_text, source_seq)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (rid, scope, st.get("status", "active"), tr,
                 _trigram_text(text), st.get("content_hash"),
                 _trigram_text(text), head))
            metas.append((rid, scope))
            texts.append(text)
            if tr in ("hot", "warm"):
                vec_metas.append(((rid, scope), embed_source_text(st)))
        dcon.execute("INSERT INTO mem_fts(mem_fts) VALUES('rebuild')")
        vecs = []
        if embedder and vec_metas:
            vecs = embedder.embed([t for _, t in vec_metas])
            if self.vec0:
                dcon.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS mem_vec USING vec0("
                    f"embedding float[{embedder.dims}]"
                    " distance_metric=cosine, scope TEXT PARTITION KEY,"
                    " +record_id TEXT)")
                for (rid, scope), v in zip(
                        [m for m, _ in vec_metas], vecs):
                    dcon.execute(
                        "INSERT INTO mem_vec(embedding, scope, record_id)"
                        " VALUES (?,?,?)",
                        (json.dumps(v), scope, rid))
            else:
                for (rid, scope), v in zip(
                        [m for m, _ in vec_metas], vecs):
                    dcon.execute(
                        "INSERT INTO mem_vec_blob(scope, record_id,"
                        " vec_json, dims) VALUES (?,?,?,?)",
                        (scope, rid, json.dumps(v), len(v)))
        vec_ids = sorted(m for m, _ in vec_metas)
        return {
            "mem_search": {"rows": len(metas),
                           "digest": canon.hash_obj(
                               "pcore.idx.v1",
                               sorted(m for m in metas))},
            "mem_fts": {"rows": len(metas),
                        "digest": canon.hash_obj(
                            "pcore.idx.v1", sorted(m for m in metas))},
            "mem_vec": {"rows": len(vecs) if embedder and vec_metas else 0,
                        "digest": canon.hash_obj(
                            "pcore.idx.v1", vec_ids)
                        if embedder and vec_metas else
                        canon.hash_obj("pcore.idx.v1", [])},
        }

    def _build_entities(self, dcon, memories, head) -> dict:
        """entities 索引（C-4 entity 腕）。record の明示 entities＋
        決定的表面抽出を (scope, entity_norm, record_id) に展開する。"""
        from .entities import record_entities
        metas = []
        for st in memories:
            rid, scope = st["record_id"], st["scope"]
            for e in record_entities(st):
                dcon.execute(
                    "INSERT OR IGNORE INTO entities(scope, entity_norm,"
                    " record_id) VALUES (?,?,?)", (scope, e, rid))
                metas.append((scope, e, rid))
        return {"entities": {"rows": len(metas),
                             "digest": canon.hash_obj(
                                 "pcore.idx.v1", sorted(metas))}}

    def _build_tomb(self, dcon, tombs, embedder, head) -> dict:
        metas = []
        texts = []
        for st in tombs:
            tid, scope = st["record_id"], st["scope"]
            pat = st.get("pattern") or ""
            metas.append((tid, scope))
            texts.append(pat)
            dcon.execute(
                "INSERT INTO tomb_emb(tombstone_id, scope, norm_text,"
                " vec_json, source_seq) VALUES (?,?,?,NULL,?)",
                (tid, scope, _trigram_text(pat), head))
        if embedder and texts:
            for (tid, _s), v in zip(metas, embedder.embed(texts)):
                dcon.execute(
                    "UPDATE tomb_emb SET vec_json=? WHERE tombstone_id=?",
                    (json.dumps(v), tid))
        return {"tomb_emb": {"rows": len(metas),
                             "digest": canon.hash_obj(
                                 "pcore.idx.v1", sorted(metas))}}

    def _build_edges(self, dcon, links, head) -> dict:
        metas = []
        for st in links:
            dcon.execute(
                "INSERT INTO edges(scope, source_id, target_id, link_type,"
                " blocking) VALUES (?,?,?,?,?)",
                (st["scope"], st.get("source_id"), st.get("target_id"),
                 st.get("link_type", "related"),
                 1 if st.get("blocking") else 0))
            metas.append((st["scope"], st.get("source_id"),
                          st.get("target_id"), st.get("link_type")))
        return {"edges": {"rows": len(metas),
                          "digest": canon.hash_obj("pcore.idx.v1",
                                                   sorted(metas))}}

    def _verify(self, dcon, rows, targets=None) -> dict:
        """検証基準（derived-rebuild §7）: 索引件数が対象行数と一致。
        部分 rebuild（Q-O48）では対象 family の index のみ照合する。"""
        targets = set(targets or self.FAMILIES)
        checks = []
        if "memories" in targets:
            expected = len(rows["memories"])
            got = dcon.execute(
                "SELECT COUNT(*) FROM mem_search").fetchone()[0]
            checks.append({"index": "mem_search", "ok": got == expected,
                           "rows": got, "expected": expected})
            if self.vec0 and dcon.execute(
                    "SELECT name FROM sqlite_master WHERE name='mem_vec'"
            ).fetchone():
                gv = dcon.execute(
                    "SELECT COUNT(*) FROM mem_vec").fetchone()[0]
                expected_vec = sum(
                    1 for st in rows["memories"]
                    if (st.get("tier") or "hot") in ("hot", "warm"))
                checks.append({"index": "mem_vec",
                               "ok": gv == expected_vec,
                               "rows": gv, "expected": expected_vec})
            ft = dcon.execute(
                "SELECT COUNT(*) FROM mem_fts").fetchone()[0]
            checks.append({"index": "mem_fts", "ok": ft == expected,
                           "rows": ft, "expected": expected})
        if "tombstones" in targets:
            te = dcon.execute("SELECT COUNT(*) FROM tomb_emb").fetchone()[0]
            checks.append({"index": "tomb_emb",
                           "ok": te == len(rows["tombstones"]),
                           "rows": te, "expected": len(rows["tombstones"])})
        if "links" in targets:
            eg = dcon.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            checks.append({"index": "edges", "ok": eg == len(rows["links"]),
                           "rows": eg, "expected": len(rows["links"])})
        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    # --------------------------------------------------------- 増分
    def index_pending(self, *, embedder=None, limit: int = 500) -> dict:
        """SD-22: last_indexed_seq 以後に mutation した memory/tombstone を
        併合走査して active derived に追記する。返り値は追記件数。"""
        acon, gen = self.open_active()
        if acon is None:
            return {"ok": False, "reason": "no active derived"}
        try:
            row = acon.execute(
                "SELECT value FROM derived_meta"
                " WHERE key='last_indexed_seq'").fetchone()
            last = int(row[0]) if row else int(
                acon.execute("SELECT value FROM derived_meta"
                             " WHERE key='frozen_read_seq'"
                             ).fetchone()[0])
            head = self.writer._conn.execute(
                "SELECT MAX(seq) FROM events").fetchone()[0] or 0
            changed = [canon.loads(r["state_json"]) for r in
                       self.writer._conn.execute(
                           "SELECT state_json FROM records"
                           " WHERE record_family='memory'"
                           " AND last_mutation_seq>? LIMIT ?",
                           (last, limit))]
            n = 0
            texts, metas, vec_metas = [], [], []
            from .entities import record_entities
            for st in changed:
                rid, scope = st["record_id"], st["scope"]
                tr = st.get("tier") or "hot"
                text = st.get("content") or st.get("text") or ""
                # 既存行を消してから upsert（status/本文/tier の変更を反映）。
                # FTS 側は行 delete/insert で追従（external-content のため
                # rowid 指定を必要とする）
                old = acon.execute(
                    "SELECT rowid FROM mem_search WHERE record_id=?",
                    (rid,)).fetchone()
                if old:
                    acon.execute(
                        "DELETE FROM mem_fts WHERE rowid=?", (old[0],))
                acon.execute(
                    "DELETE FROM mem_search WHERE record_id=?", (rid,))
                cur = acon.execute(
                    "INSERT INTO mem_search(record_id, scope, status, tier,"
                    " norm_text, content_hash, tokenized_text, source_seq)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (rid, scope, st.get("status", "active"), tr,
                     _trigram_text(text), st.get("content_hash"),
                     _trigram_text(text), head))
                acon.execute(
                    "INSERT INTO mem_fts(rowid, norm_text) VALUES (?,?)",
                    (cur.lastrowid, _trigram_text(text)))
                # entities 索引の追従（C-4: delete+reinsert）
                acon.execute(
                    "DELETE FROM entities WHERE record_id=?", (rid,))
                for e in record_entities(st):
                    acon.execute(
                        "INSERT OR IGNORE INTO entities(scope,"
                        " entity_norm, record_id) VALUES (?,?,?)",
                        (scope, e, rid))
                # embed 入力は rebuild 経路と同一の日付前置済み文面
                # （増分で素文を埋めると新旧でベクトルが系統的にずれる）
                texts.append(embed_source_text(st))
                metas.append((rid, scope))
                if tr in ("hot", "warm"):
                    vec_metas.append((rid, scope))
                n += 1
            if embedder and texts:
                vecs = embedder.embed(texts)
                # vec0 membership は hot/warm のみ。cold 落ちした行は
                # mem_vec から除く（index membership・FA §6.2）
                vmeta = set(vec_metas)
                if self.vec0:
                    for (rid, scope), v in zip(metas, vecs):
                        acon.execute(
                            "DELETE FROM mem_vec WHERE record_id=?", (rid,))
                        if (rid, scope) in vmeta:
                            acon.execute(
                                "INSERT INTO mem_vec(embedding, scope,"
                                " record_id) VALUES (?,?,?)",
                                (json.dumps(v), scope, rid))
                else:
                    for (rid, scope), v in zip(metas, vecs):
                        acon.execute(
                            "DELETE FROM mem_vec_blob WHERE scope=?"
                            " AND record_id=?", (scope, rid))
                        if (rid, scope) in vmeta:
                            acon.execute(
                                "INSERT INTO mem_vec_blob(scope,"
                                " record_id, vec_json, dims)"
                                " VALUES (?,?,?,?)",
                                (scope, rid, json.dumps(v), len(v)))
            acon.execute(
                "INSERT OR REPLACE INTO derived_meta(key, value) VALUES"
                " ('last_indexed_seq', ?)", (str(head),))
            acon.commit()
            return {"ok": True, "gen": gen, "indexed": n,
                    "last_indexed_seq": head}
        finally:
            acon.close()

    # ------------------------------------------------------- 検索 helper
    def fts_terms(self, query: str) -> list:
        """クエリを trigram OR 分解した MATCH 式項列（read-path §7.1）。
        3文字未満は LIKE fallback に回すため None を返す。"""
        q = canon.norm_text(query or "")
        if len(q) < 3:
            return None
        terms = []
        for piece in q.split():
            for i in range(len(piece) - 2):
                terms.append(piece[i:i + 3])
        return sorted(set(terms))
