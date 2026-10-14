"""adapters.packio —— L-EXP pack export/import（core-api §6・migration-m5 §5.3）

export: consistent cut（宣言 seq で凍結）→ allowlist 表を jsonl＋manifest。
import: staged_* 検疫領域 → test-apply（鎖検査・fold 一致・needs_repair
集計）→ commit（verbatim 復元＋import_anchor 刻印）→ validate（往復照合）。

commit は「events の verbatim 復元」を行う: export 側で鎖が完結している
ため row_hash/prev_hash はそのまま入れ、test-apply で鎖完結性を機械検査
してから書き込む（検査なしの生 INSERT はしない）。staged → live の
書込みは writer.transaction() 配下で行う（canonical 書込み経路の単一性）。
"""

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..kernel import canon, fold, oracle, pack, ids


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


# commit 時に staged_registry → live へ写す表（registry の PK 単位 upsert。
# core_meta の schema_version 等は移行先 bootstrap 側が既に持つため、
# INSERT OR IGNORE で「未存在のキーのみ」取り込む）
_REGISTRY_PK = {
    "scope_registry": ("scope",),
    "vocab_registry": ("vocab_kind", "value"),
    "kernel_table": ("algo",),
    "tool_ledger": ("tool_name",),
    "policy_state": ("rule_id",),
    "schema_migrations": ("version",),
    "core_meta": ("key",),
}


class PackError(Exception):
    pass


class PackIO:
    """core.db ↔ pack ディレクトリの export/import。"""

    def __init__(self, writer, *, clock=None):
        self.writer = writer
        self._clock = clock or _now

    # ============================================================ export
    def export_pack(self, out_dir, *, cut_seq=None,
                    watermark_hint=True) -> dict:
        """canonical 全量を pack として出力。戻り値は manifest。

        consistent cut: cut_seq（既定=head）で読みを凍結し、
        その時点の events と表内容を吐く（流入分は切り捨て）。
        """
        out = Path(out_dir)
        if out.exists() and any(out.iterdir()):
            raise PackError(f"output dir not empty: {out}")
        out.mkdir(parents=True, exist_ok=True)
        (out / "tables").mkdir(exist_ok=True)
        conn = self.writer._conn

        head = conn.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0
        cut = head if cut_seq is None else int(cut_seq)
        if cut > head:
            raise PackError(f"cut_seq {cut} > head {head}")

        counts, digests = {}, {}
        # events（完全行写し・seq 順）
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE seq<=? ORDER BY seq", (cut,))]
        self._write_jsonl(out / "events.jsonl", rows)
        counts["events"] = len(rows)
        digests["events"] = pack.table_digest("events", rows)

        # canonical/log/registry 表（last_mutation_seq で cut を効かせる
        # 表と、全行写しの表に分かれる）
        for t in pack.CANONICAL_TABLES:
            r2 = [dict(r) for r in conn.execute(
                f"SELECT * FROM {t} WHERE last_mutation_seq<=?", (cut,))]
            self._write_jsonl(out / "tables" / f"{t}.jsonl", r2)
            counts[t] = len(r2)
            digests[t] = pack.table_digest(t, r2)
        for t in pack.LOG_TABLES:
            if t == "events":
                continue
            r2 = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
            self._write_jsonl(out / "tables" / f"{t}.jsonl", r2)
            counts[t] = len(r2)
            digests[t] = pack.table_digest(t, r2)
        for t in pack.REGISTRY_TABLES:
            r2 = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
            self._write_jsonl(out / "tables" / f"{t}.jsonl", r2)
            counts[t] = len(r2)
            digests[t] = pack.table_digest(t, r2)

        # permanent blob 実体（ref がファイルとして解決できるもののみ。
        # 解決不能は blob_files に "missing" で刻む）
        blob_files = []
        blob_dir = out / "blobs"
        for r in conn.execute(
                "SELECT hash, ref, size FROM blobs"
                " WHERE retention_class='permanent'"
                " AND last_mutation_seq<=?", (cut,)):
            ref = r["ref"]
            entry = {"hash": r["hash"], "size": r["size"]}
            src = Path(ref) if ref else None
            if src and src.exists() and src.is_file():
                blob_dir.mkdir(exist_ok=True)
                dst = blob_dir / r["hash"]
                shutil.copy2(src, dst)
                entry["path"] = f"blobs/{r['hash']}"
                entry["status"] = "bundled"
            else:
                entry["status"] = "missing_ref"
            blob_files.append(entry)

        # replay_digest（fold 畳み込みの権威値 —— round-trip の照合基準）
        fr = fold.fold_rows([dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE seq<=? ORDER BY seq", (cut,))])
        rdigest = fold.replay_digest(fr["state"])

        if watermark_hint:
            hint = [dict(r) for r in conn.execute(
                "SELECT * FROM watermarks")]
            (out / "watermark_hint.json").write_text(
                canon.dumps({"watermarks": hint}), encoding="utf-8")

        manifest = pack.build_manifest(
            exported_seq=cut, exported_at=self._clock(),
            counts=counts, digests=digests, replay_digest=rdigest,
            blob_files=blob_files, watermark_hint=watermark_hint)
        (out / "manifest.json").write_text(
            canon.dumps(manifest), encoding="utf-8")
        return manifest

    @staticmethod
    def _write_jsonl(path: Path, rows):
        with open(path, "wb") as f:
            for r in rows:
                f.write(pack.canon_line(r))

    # ============================================================ import
    def stage(self, pack_dir, *, run_id=None) -> dict:
        """pack → staged_* 検疫領域へ読み込み（import_key=event_id 冪等）。

        manifest の構造検査と各ファイルの digest 照合を行い、
        中身を staged_* 表へ入れて run を返す。
        """
        pack_dir = Path(pack_dir)
        manifest = json.loads(
            (pack_dir / "manifest.json").read_text(encoding="utf-8"))
        errs = pack.verify_manifest(manifest)
        if errs:
            raise PackError("manifest invalid: " + "; ".join(errs))
        run_id = run_id or ids.record_id(
            "run", canon.hash_bytes(
                "pcore.run_id.v1",
                canon.dumps_bytes(manifest))[:24])
        now = self._clock()

        conn = self.writer._conn
        with self.writer.transaction():
            # run_id は pack 内容由来 —— 同 pack の再 stage は冪等で
            # 既存 run をそのまま返す（中断後の再開・重複投入の両方を
            # 安全に扱う。別 pack は別 run_id になる）
            dup = conn.execute(
                "SELECT status FROM import_runs WHERE run_id=?",
                (run_id,)).fetchone()
            if dup:
                return {"run_id": run_id, "status": dup["status"],
                        "counts": canon.loads(conn.execute(
                            "SELECT counts_json FROM import_runs"
                            " WHERE run_id=?", (run_id,)
                        ).fetchone()["counts_json"]),
                        "resumed": True}
            conn.execute(
                "INSERT INTO import_runs(run_id, mode, pack_digest,"
                " source_seq, status, counts_json, manifest_json,"
                " created_at, updated_at) VALUES (?,?,?,?,'staged',?,?,?,?)",
                (run_id, manifest["mode"], manifest["pack_digest"],
                 manifest["exported_seq"], canon.dumps(manifest["counts"]),
                 canon.dumps(manifest), now, now))

            counts = {"events": 0, "records": 0, "registry": 0,
                      "blobs": 0, "digest_errors": []}

            # events.jsonl → staged_events（digest 再照合）
            ev_rows = [pack.parse_line(l) for l in
                       (pack_dir / "events.jsonl").read_bytes().splitlines()
                       if l.strip()]
            if pack.table_digest("events", ev_rows) \
                    != manifest["digests"].get("events"):
                counts["digest_errors"].append("events")
            for r in ev_rows:
                conn.execute(
                    "INSERT INTO staged_events(run_id, seq, event_id,"
                    " event_type, row_json, status) VALUES (?,?,?,?,?,"
                    " 'staged')",
                    (run_id, r["seq"], r["event_id"], r["event_type"],
                     canon.dumps(r)))
            counts["events"] = len(ev_rows)

            # tables/*.jsonl → staged_records / staged_registry
            for t, dest in [(t, "records")
                            for t in pack.CANONICAL_TABLES
                            + pack.LOG_TABLES] + \
                           [(t, "registry") for t in pack.REGISTRY_TABLES]:
                p = pack_dir / "tables" / f"{t}.jsonl"
                if not p.exists():
                    continue
                rows = [pack.parse_line(l) for l in
                        p.read_bytes().splitlines() if l.strip()]
                if pack.table_digest(t, rows) \
                        != manifest["digests"].get(t):
                    counts["digest_errors"].append(t)
                for r in rows:
                    if dest == "records":
                        keycol = {"records": "record_id",
                                  "candidates": "candidate_id",
                                  "tombstones": "tombstone_id",
                                  "links": "link_id",
                                  "blobs": "hash",
                                  "migration_map": "import_key",
                                  "archive_segments": "segment_id"}[t]
                        st = canon.loads(r["state_json"]) \
                            if "state_json" in r else {}
                        rid = r.get(keycol) or st.get("record_id") \
                            or r.get("import_key") or "?"
                        conn.execute(
                            "INSERT INTO staged_records(run_id, table_name,"
                            " scope, record_id, row_json, import_key,"
                            " status) VALUES (?,?,?,?,?,?,'staged')",
                            (run_id, t, r.get("scope") or
                             st.get("scope") or "?",
                             rid, canon.dumps(r),
                             r.get("import_key")))
                        counts["records"] += 1
                    else:
                        conn.execute(
                            "INSERT INTO staged_registry(run_id, table_name,"
                            " row_json, status) VALUES (?,?,?,'staged')",
                            (run_id, t, canon.dumps(r)))
                        counts["registry"] += 1

            # blobs/ → staged_blobs
            for bf in manifest.get("blob_files") or []:
                bp = pack_dir / (bf.get("path") or "")
                status = "staged" if bf.get("status") == "bundled" \
                    and bp.exists() else "missing"
                conn.execute(
                    "INSERT INTO staged_blobs(run_id, hash, pack_path,"
                    " size, status) VALUES (?,?,?,?,?)",
                    (run_id, bf["hash"], bf.get("path") or "",
                     bf.get("size") or 0, status))
                counts["blobs"] += 1

            conn.execute(
                "UPDATE import_runs SET counts_json=? WHERE run_id=?",
                (canon.dumps(counts), run_id))
        return {"run_id": run_id, "status": "staged", "counts": counts,
                "pack_digest": manifest["pack_digest"]}

    def test_apply(self, run_id: str) -> dict:
        """staged 内容を機械検査して verified/failed に遷移させる。

        検査列（migration-m5 §5 のうち pack 自給分）:
        - staged events の鎖完結（oracle_verify_chain・独立実装）
        - fold(staged_events) → 物理行写像 == staged_records 内容
        - manifest replay_digest との一致
        - scope_registry 被覆（staged records の scope が registry にあるか）
        - blob 実体の同梱状況
        戻り値は needs_repair 集計付きの判定レポート。
        """
        conn = self.writer._conn
        with self.writer.transaction():
            run = conn.execute(
                "SELECT * FROM import_runs WHERE run_id=?",
                (run_id,)).fetchone()
            if not run:
                raise PackError(f"run not found: {run_id}")
            ev_rows = [canon.loads(r["row_json"]) for r in conn.execute(
                "SELECT row_json FROM staged_events WHERE run_id=?"
                " ORDER BY seq", (run_id,))]

            needs, checks = [], []

            # 1) 鎖完結（独立実装で検査 —— 同一コード経路の盲点を防ぐ）
            ch = oracle.oracle_verify_chain(ev_rows)
            checks.append({"check": "chain", "ok": ch["ok"],
                           "errors": ch["errors"][:5]})
            if not ch["ok"]:
                needs.append({"what": "events", "reason": "chain broken"})

            # 2) fold(staged_events) → staged_records との一致
            # fold が導出するのは5つの record family 表のみ ——
            # migration_map/archive_segments は commit で verbatim 復元される
            # 写しなので比較対象外（fold 由来でない行をここで要求しない）
            folded = fold.fold_rows(ev_rows)["state"]
            expect = {}
            for (fam, scope, rid), (st, seq) in folded.items():
                table, row = fold.row_for(fam, st, seq)
                expect[(table, scope, rid)] = row["state_json"]
            got = {}
            for r in conn.execute(
                    "SELECT table_name, scope, record_id, row_json"
                    " FROM staged_records WHERE run_id=? AND table_name IN"
                    " ('records','candidates','tombstones','links','blobs')",
                    (run_id,)):
                row = canon.loads(r["row_json"])
                got[(r["table_name"], r["scope"], r["record_id"])] = \
                    row.get("state_json")
            diff = sorted(set(expect) ^ set(got))
            content_diff = [k for k in set(expect) & set(got)
                            if expect[k] != got[k]]
            ok2 = not diff and not content_diff
            checks.append({"check": "fold_vs_staged_records", "ok": ok2,
                           "key_diffs": diff[:5],
                           "content_diffs": content_diff[:5]})
            if not ok2:
                needs.append({"what": "records",
                              "reason": "staged rows diverge from fold"})

            # 3) manifest replay_digest 一致
            rd = fold.replay_digest(folded)
            man = canon.loads(run["manifest_json"])
            ok3 = rd == man["replay_digest"]
            checks.append({"check": "replay_digest", "ok": ok3,
                           "got": rd, "expected": man["replay_digest"]})
            if not ok3:
                needs.append({"what": "replay_digest",
                              "reason": "digest mismatch vs manifest"})

            # 4) scope_registry 被覆
            reg_scopes = set()
            for r in conn.execute(
                    "SELECT row_json FROM staged_registry WHERE run_id=?"
                    " AND table_name='scope_registry'", (run_id,)):
                reg_scopes.add(canon.loads(r["row_json"]).get("scope"))
            missing_scope = sorted(
                {k[1] for k in expect} - reg_scopes - {""})
            ok4 = not missing_scope
            checks.append({"check": "scope_coverage", "ok": ok4,
                           "missing": missing_scope})
            if not ok4:
                needs.append({"what": "scope_registry",
                              "reason": f"scopes not in pack: "
                                        f"{missing_scope}"})

            # 5) permanent blob 実体の同梱
            missing_b = [r["hash"] for r in conn.execute(
                "SELECT hash FROM staged_blobs WHERE run_id=?"
                " AND status='missing'", (run_id,))]
            checks.append({"check": "blob_entities", "ok": not missing_b,
                           "missing": missing_b})
            if missing_b:
                needs.append({"what": "blobs",
                              "reason": "permanent blob entities missing"})

            ok = all(c["ok"] for c in checks)
            status = "verified" if ok else "failed"
            conn.execute(
                "UPDATE import_runs SET status=?, needs_repair_json=?,"
                " updated_at=? WHERE run_id=?",
                (status, canon.dumps(needs), self._clock(), run_id))
            conn.execute(
                "UPDATE staged_events SET status=? WHERE run_id=?",
                ("applied" if ok else "failed", run_id))
            conn.execute(
                "UPDATE staged_records SET status=? WHERE run_id=?",
                ("applied" if ok else "failed", run_id))
            return {"run_id": run_id, "ok": ok, "status": status,
                    "checks": checks, "needs_repair": needs}

    def commit(self, run_id: str, pack_dir) -> dict:
        """verified run を canonical へ確定（events verbatim 復元）。

        - staged events を seq 順に INSERT（event_id 冪等: 既存 event_id は
          skip、seq が別 event_id で占有されていたら失敗）
        - staged_records/registry を各表へ upsert/INSERT OR IGNORE
        - blob 実体を <db>/pack-blobs/<hash> へコピー
        - `import_anchor` marker event を刻印
        - commit 後に writer.replay_check を走らせ、不一致なら rollback
        """
        conn = self.writer._conn
        pack_dir = Path(pack_dir)
        with self.writer.transaction():
            run = conn.execute(
                "SELECT status FROM import_runs WHERE run_id=?",
                (run_id,)).fetchone()
            if not run:
                raise PackError(f"run not found: {run_id}")
            if run["status"] != "verified":
                raise PackError(
                    f"run must be verified (got {run['status']})")

            # 1) events verbatim 復元
            inserted, skipped = 0, 0
            for r in conn.execute(
                    "SELECT seq, event_id, row_json FROM staged_events"
                    " WHERE run_id=? ORDER BY seq", (run_id,)):
                row = canon.loads(r["row_json"])
                ex = conn.execute(
                    "SELECT event_id, row_hash FROM events WHERE seq=?",
                    (row["seq"],)).fetchone()
                if ex:
                    if ex["event_id"] == row["event_id"] \
                            and ex["row_hash"] == row["row_hash"]:
                        skipped += 1
                        continue
                    raise PackError(
                        f"seq {row['seq']} occupied by different event")
                conn.execute(
                    "INSERT INTO events(seq, event_id, event_type,"
                    " occurred_at, recorded_at, actor, session_json,"
                    " session_channel, session_id, payload_json,"
                    " attachments_json, redacted_json, correlation_json,"
                    " turn_id, source_meta_json, ingress_key, prev_hash,"
                    " row_hash, canon_algo, migration_json) VALUES"
                    " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["seq"], row["event_id"], row["event_type"],
                     row.get("occurred_at"), row["recorded_at"],
                     row["actor"], row.get("session_json"),
                     row.get("session_channel"), row.get("session_id"),
                     row["payload_json"], row.get("attachments_json"),
                     row.get("redacted_json"), row.get("correlation_json"),
                     row.get("turn_id"), row.get("source_meta_json"),
                     row.get("ingress_key"), row["prev_hash"],
                     row["row_hash"], row["canon_algo"],
                     row.get("migration_json")))
                inserted += 1

            # 2) staged_records → canonical 表へ upsert（state_json 権威）
            n_rec = 0
            for r in conn.execute(
                    "SELECT table_name, row_json FROM staged_records"
                    " WHERE run_id=? AND table_name IN"
                    " ('records','candidates','tombstones','links','blobs')",
                    (run_id,)):
                row = canon.loads(r["row_json"])
                self._upsert_row(conn, r["table_name"], row)
                n_rec += 1
            # canonical_log 表（migration_map/archive_segments）は
            # append-only: INSERT OR IGNORE
            for r in conn.execute(
                    "SELECT table_name, row_json FROM staged_records"
                    " WHERE run_id=? AND table_name IN"
                    " ('migration_map','archive_segments')", (run_id,)):
                row = canon.loads(r["row_json"])
                self._insert_ignore(conn, r["table_name"], row)
                n_rec += 1

            # 3) staged_registry → registry 表（INSERT OR IGNORE）
            n_reg = 0
            for r in conn.execute(
                    "SELECT table_name, row_json FROM staged_registry"
                    " WHERE run_id=?", (run_id,)):
                row = canon.loads(r["row_json"])
                if self._insert_ignore(conn, r["table_name"], row):
                    n_reg += 1

            # 4) blob 実体 → <db>/pack-blobs/<hash>
            dst_dir = self.writer.path.parent / "pack-blobs"
            n_blob = 0
            for r in conn.execute(
                    "SELECT hash, pack_path, status FROM staged_blobs"
                    " WHERE run_id=?", (run_id,)):
                if r["status"] != "staged":
                    continue
                src = pack_dir / r["pack_path"]
                if src.exists():
                    dst_dir.mkdir(exist_ok=True)
                    shutil.copy2(src, dst_dir / r["hash"])
                    conn.execute(
                        "UPDATE staged_blobs SET status='committed'"
                        " WHERE run_id=? AND hash=?", (run_id, r["hash"]))
                    n_blob += 1

            # 5) 復元後の機械検査: replay が pack 内容と一致するか
            rep = self.writer.replay_check()
            if not rep["ok"]:
                raise PackError(
                    f"post-commit replay diverges: {rep['diffs'][:3]}")

            # 6) import_anchor 刻印（run 完了の監査）
            self.writer._append_locked(
                "import_anchor",
                {"run_id": run_id, "counts": {"events": inserted,
                                              "events_skipped": skipped,
                                              "records": n_rec,
                                              "registry": n_reg,
                                              "blobs": n_blob},
                 "replay_digest": rep["digest"]},
                actor="system")

            now = self._clock()
            conn.execute(
                "UPDATE import_runs SET status='committed', updated_at=?"
                " WHERE run_id=?", (now, run_id))
            conn.execute(
                "UPDATE staged_events SET status='committed'"
                " WHERE run_id=?", (run_id,))
            conn.execute(
                "UPDATE staged_records SET status='committed'"
                " WHERE run_id=?", (run_id,))
            return {"run_id": run_id, "inserted_events": inserted,
                    "skipped_events": skipped, "records": n_rec,
                    "registry": n_reg, "blobs": n_blob,
                    "replay_digest": rep["digest"]}

    def validate_pack(self, pack_dir, *, expect_replay_digest=None) -> dict:
        """往復照合: 現在の DB 状態が pack の manifest と一致するか
        （replay digest・record 数・content_hash 集合 —— §5.3）。"""
        man = json.loads(
            (Path(pack_dir) / "manifest.json").read_text(encoding="utf-8"))
        conn = self.writer._conn
        rep = self.writer.replay_check()
        checks = []
        rd = rep["digest"]
        exp = expect_replay_digest or man["replay_digest"]
        checks.append({"check": "replay_digest", "ok": rd == exp,
                       "got": rd, "expected": exp})
        n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        checks.append({"check": "record_count",
                       "ok": n == man["counts"].get("records"),
                       "got": n, "expected": man["counts"].get("records")})
        hashes = {r[0] for r in conn.execute(
            "SELECT content_hash FROM records WHERE content_hash"
            " IS NOT NULL")}
        # pack 側の content_hash 集合を events.jsonl から再構成
        pack_hashes = set()
        for line in (Path(pack_dir) / "tables" / "records.jsonl"
                     ).read_bytes().splitlines():
            if line.strip():
                pack_hashes.add(
                    pack.parse_line(line).get("content_hash"))
        pack_hashes.discard(None)
        checks.append({"check": "content_hash_set",
                       "ok": hashes == pack_hashes,
                       "missing": sorted(pack_hashes - hashes)[:5],
                       "extra": sorted(hashes - pack_hashes)[:5]})
        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    # ------------------------------------------------------------- util
    def _upsert_row(self, conn, table: str, row: dict):
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        pks = {"records": "scope, record_id",
               "candidates": "scope, candidate_id",
               "tombstones": "scope, tombstone_id",
               "links": "scope, link_id",
               "blobs": "scope, hash"}[table]
        sets = ", ".join(f"{c}=excluded.{c}" for c in row
                         if c not in {k.strip() for k in pks.split(",")})
        conn.execute(
            f"INSERT INTO {table}({cols}) VALUES({marks})"
            f" ON CONFLICT({pks}) DO UPDATE SET {sets}",
            tuple(row.values()))

    @staticmethod
    def _insert_ignore(conn, table: str, row: dict) -> bool:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {table}({cols}) VALUES({marks})",
            tuple(row.values()))
        return cur.rowcount > 0
