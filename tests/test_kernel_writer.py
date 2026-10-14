"""kernel.writer のテスト（P8-1）

カバー: bootstrap・seq/hash鎖・(actor,ingress_key) 冪等・append-only
（authorizer＋trigger 二層）・scope fail-closed・record_version 連続性・
fold/replay 一致・改竄検出・起動 sanity・manifest pin。
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class WriterTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        cls.writer = cls._iso.import_module("companion.kernel.writer")
        cls.canon = cls._iso.import_module("companion.kernel.canon")
        cls.events = cls._iso.import_module("companion.kernel.events")
        cls.schema = cls._iso.import_module("companion.kernel.schema")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="coredb_test_"))
        self.t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        self._tick = {"i": 0}

        def clock():
            self._tick["i"] += 1
            return self.t0 + timedelta(seconds=self._tick["i"])

        self.clock = clock
        self.w = self.writer.open_core(self.tmp / "core.db", clock=clock)

    def tearDown(self):
        self.w.close()

    def _mem(self, rid="mem_1", ver=1, body="テスト記憶", **kw):
        st = {
            "record_id": rid, "record_family": "memory", "scope": "user",
            "record_version": ver, "status": "active", "memory_type": "fact",
            "body": {"text": body},
            "content_hash": self.canon.content_hash(body),
            "importance": 3, "retrieval_strength": 0.5,
            "epistemic_confidence": 0.9,
            "created_at": "2026-10-09T00:00:00.000000Z",
            "updated_at": "2026-10-09T00:00:00.000000Z",
            "provenance": {"generated_by": "manual",
                           "evidence_basis": "direct",
                           "source_events": []},
        }
        st.update(kw)
        return st


class TestBootstrap(WriterTestBase):
    def test_tables_and_seeds(self):
        conn = self.w._conn
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(self.schema.EXPECTED_TABLES <= names)
        scopes = {r[0] for r in conn.execute(
            "SELECT scope FROM scope_registry WHERE status='active'")}
        self.assertEqual(scopes, {"user", "agent"})
        mv = conn.execute(
            "SELECT value FROM core_meta WHERE key='schema_version'"
        ).fetchone()
        self.assertEqual(mv[0], str(self.schema.SCHEMA_VERSION))

    def test_reopen_idempotent(self):
        self.w.append_event("message", {"text": "x"}, actor="master")
        self.w.close()
        w2 = self.writer.open_core(self.tmp / "core.db", clock=self.clock)
        self.assertTrue(w2.sanity_check()["ok"])
        w2.close()
        self.w = self.writer.open_core(self.tmp / "core.db", clock=self.clock)


class TestAppend(WriterTestBase):
    def test_seq_and_chain(self):
        r1 = self.w.append_event("message", {"text": "a"}, actor="master")
        r2 = self.w.append_event("response", {"text": "b"}, actor="companion")
        self.assertEqual((r1["seq"], r2["seq"]), (1, 2))
        self.assertEqual(r2["prev_hash"], r1["row_hash"])
        v = self.w.verify_chain()
        self.assertTrue(v["ok"])
        self.assertEqual(v["verified"], 2)
        self.assertEqual(v["head"], r2["row_hash"])

    def test_genesis_prev_hash(self):
        r = self.w.append_event("message", {"text": "a"}, actor="master")
        row = self.w._conn.execute(
            "SELECT prev_hash FROM events WHERE seq=1").fetchone()
        self.assertEqual(row[0], self.events.GENESIS_HASH)

    def test_dedup_same_key_same_content(self):
        kw = dict(actor="master", ingress_key="k1",
                  session={"channel": "dm", "session_id": "s"})
        r1 = self.w.append_event("message", {"text": "hi"}, **kw)
        r2 = self.w.append_event("message", {"text": "hi"}, **kw)
        self.assertFalse(r1["dedup"])
        self.assertTrue(r2["dedup"])
        self.assertEqual(r2["event_id"], r1["event_id"])
        n = self.w._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(n, 1)

    def test_dedup_conflict_rejected(self):
        self.w.append_event("message", {"text": "hi"}, actor="master",
                            ingress_key="k1")
        with self.assertRaises(self.writer.IngressConflictError):
            self.w.append_event("message", {"text": "different"},
                                actor="master", ingress_key="k1")

    def test_dedup_scoped_by_actor(self):
        self.w.append_event("message", {"text": "hi"}, actor="master",
                            ingress_key="k1")
        r = self.w.append_event("message", {"text": "hi"}, actor="system",
                                ingress_key="k1")
        self.assertFalse(r["dedup"])

    def test_reserved_vocab_rejected(self):
        with self.assertRaises(Exception):
            self.w.append_event("@core/tampered", {}, actor="system")

    def test_actor_vocabulary(self):
        r = self.w.append_event("tool_result", {"x": 1}, actor="external:obs")
        self.assertTrue(r["accepted"])
        with self.assertRaises(Exception):
            self.w.append_event("message", {}, actor="nobody")


class TestAppendOnly(WriterTestBase):
    def test_update_delete_denied_on_writer_conn(self):
        self.w.append_event("message", {"text": "a"}, actor="master")
        with self.assertRaises(sqlite3.DatabaseError):
            self.w._conn.execute("UPDATE events SET actor='x' WHERE seq=1")
        with self.assertRaises(sqlite3.DatabaseError):
            self.w._conn.execute("DELETE FROM events WHERE seq=1")

    def test_append_only_triggers_stop_other_connections(self):
        self.w.append_event("message", {"text": "a"}, actor="master")
        raw = sqlite3.connect(str(self.tmp / "core.db"))
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM events WHERE seq=1")
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("UPDATE events SET actor='x' WHERE seq=1")
        raw.close()

    def test_ddl_denied(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.w._conn.execute("CREATE TABLE rogue(x)")
        with self.assertRaises(sqlite3.DatabaseError):
            self.w._conn.execute("DROP TABLE records")
        with self.assertRaises(sqlite3.DatabaseError):
            self.w._conn.execute("ATTACH ':memory:' AS m")

    def test_records_delete_denied(self):
        st = self._mem()
        self.w.apply_mutation(
            "memory_write", {"memory_id": "mem_1", "new_state": st},
            actor="system")
        with self.assertRaises(sqlite3.DatabaseError):
            self.w._conn.execute(
                "DELETE FROM records WHERE record_id='mem_1'")


class TestMutation(WriterTestBase):
    def test_write_and_update(self):
        st = self._mem()
        r = self.w.apply_mutation(
            "memory_write",
            {"memory_id": "mem_1", "new_state": st, "reason": "t"},
            actor="companion")
        self.assertTrue(r["accepted"])
        row = self.w._conn.execute(
            "SELECT * FROM records WHERE record_id='mem_1'").fetchone()
        self.assertEqual(row["record_version"], 1)
        self.assertEqual(row["last_mutation_seq"], r["seq"])
        self.assertEqual(self.canon.loads(row["state_json"]), st)

        st2 = dict(st, record_version=2, status="invalidated")
        self.w.apply_mutation(
            "memory_update",
            {"memory_id": "mem_1", "op": "invalidate",
             "new_state": st2, "reason": "忘れて"},
            actor="master")
        row = self.w._conn.execute(
            "SELECT status, record_version FROM records"
            " WHERE record_id='mem_1'").fetchone()
        self.assertEqual((row["status"], row["record_version"]),
                         ("invalidated", 2))

    def test_version_continuity(self):
        st = self._mem()
        self.w.apply_mutation(
            "memory_write",
            {"memory_id": "mem_1", "new_state": st}, actor="companion")
        with self.assertRaises(self.writer.MutationError):
            self.w.apply_mutation(
                "memory_update",
                {"memory_id": "mem_1", "op": "correct",
                 "new_state": dict(st, record_version=3)}, actor="companion")
        with self.assertRaises(self.writer.MutationError):
            self.w.apply_mutation(
                "memory_write",
                {"memory_id": "mem_2",
                 "new_state": self._mem("mem_2", ver=2)}, actor="companion")

    def test_scope_fail_closed(self):
        st = self._mem(scope="nobody")
        with self.assertRaises(self.writer.MutationError):
            self.w.apply_mutation(
                "memory_write",
                {"memory_id": "mem_1", "new_state": st}, actor="companion")

    def test_reserved_scope_person_rejected(self):
        """person:<id> は予約 scope（status=reserved）。活性化前は fail-closed。"""
        st = self._mem(scope="person:someone")
        with self.assertRaises(self.writer.MutationError):
            self.w.apply_mutation(
                "memory_write",
                {"memory_id": "mem_1", "new_state": st}, actor="companion")

    def test_missing_fields_rejected(self):
        st = self._mem()
        del st["status"]
        with self.assertRaises(self.writer.MutationError):
            self.w.apply_mutation(
                "memory_write",
                {"memory_id": "mem_1", "new_state": st}, actor="companion")

    def test_candidate_and_tombstone_routing(self):
        cand = {
            "record_id": "cand_1", "record_family": "candidate",
            "scope": "user", "record_version": 1, "status": "pending",
            "proposal": {"body": {"text": "x"}},
            "created_at": "2026-10-09T00:00:00.000000Z",
            "updated_at": "2026-10-09T00:00:00.000000Z",
        }
        self.w.apply_mutation(
            "record_write",
            {"record_family": "candidate", "record_id": "cand_1",
             "new_state": cand}, actor="system")
        n = self.w._conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE status='pending'"
        ).fetchone()[0]
        self.assertEqual(n, 1)

        tmb = {
            "record_id": "tmb_1", "record_family": "tombstone",
            "scope": "user", "record_version": 1, "status": "active",
            "pattern_hash": self.canon.pattern_hash("秘密の内容"),
            "pattern": "秘密の内容", "pattern_kind": "normalized_text",
            "reason": "forget",
            "created_at": "2026-10-09T00:00:00.000000Z",
            "updated_at": "2026-10-09T00:00:00.000000Z",
        }
        self.w.apply_mutation(
            "record_write",
            {"record_family": "tombstone", "record_id": "tmb_1",
             "new_state": tmb}, actor="master")
        n = self.w._conn.execute(
            "SELECT COUNT(*) FROM tombstones WHERE pattern_hash=?",
            (tmb["pattern_hash"],)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_record_source_junction(self):
        r = self.w.append_event("message", {"text": "m"}, actor="master")
        st = self._mem()
        st["provenance"]["source_events"] = [
            {"event_id": r["event_id"], "cite": "text"}]
        self.w.apply_mutation(
            "memory_write",
            {"memory_id": "mem_1", "new_state": st}, actor="companion")
        rows = self.w._conn.execute(
            "SELECT event_id, cite, ordinal FROM record_source"
            " WHERE record_id='mem_1'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_id"], r["event_id"])


class TestReplay(WriterTestBase):
    def test_fold_reproduces_records(self):
        st1 = self._mem("mem_1", body="りんご")
        st2 = self._mem("mem_2", body="みかん")
        self.w.apply_mutation(
            "memory_write", {"memory_id": "mem_1", "new_state": st1},
            actor="companion")
        self.w.apply_mutation(
            "memory_write", {"memory_id": "mem_2", "new_state": st2},
            actor="companion")
        self.w.apply_mutation(
            "memory_update",
            {"memory_id": "mem_1", "op": "correct",
             "new_state": dict(st1, record_version=2, status="active")},
            actor="companion")
        self.w.append_event("message", {"text": "non-mutation"},
                            actor="master")
        rc = self.w.replay_check()
        self.assertTrue(rc["ok"], rc["diffs"])
        self.assertEqual(rc["digest"], rc["stored_digest"])
        self.assertEqual(rc["counters"]["mutations"], 3)

    def test_unknown_event_type_counted(self):
        self.w.append_event("future_event", {"x": 1}, actor="system")
        rc = self.w.replay_check()
        self.assertTrue(rc["ok"])
        self.assertEqual(rc["unhandled_event_types"], {"future_event": 1})


class TestTamper(WriterTestBase):
    def _raw_tamper(self, sql, *args):
        raw = sqlite3.connect(str(self.tmp / "core.db"))
        raw.execute("DROP TRIGGER events_no_update")
        raw.execute("DROP TRIGGER events_no_delete")
        raw.execute(sql, args)
        raw.commit()
        raw.close()

    def test_payload_tamper_detected(self):
        self.w.append_event("message", {"text": "a"}, actor="master")
        self.w.append_event("message", {"text": "b"}, actor="master")
        self._raw_tamper(
            "UPDATE events SET payload_json='{\"text\":\"x\"}' WHERE seq=1")
        v = self.w.verify_chain()
        self.assertFalse(v["ok"])
        self.assertEqual(v["bad"]["seq"], 1)
        self.assertEqual(v["bad"]["issue"], "hash_mismatch")

    def test_row_removal_detected(self):
        for i in range(3):
            self.w.append_event("message", {"text": str(i)}, actor="master")
        self._raw_tamper("DELETE FROM events WHERE seq=2")
        v = self.w.verify_chain()
        self.assertFalse(v["ok"])
        self.assertEqual(v["bad"]["issue"], "gap")

    def test_row_reorder_detected(self):
        for i in range(3):
            self.w.append_event("message", {"text": str(i)}, actor="master")
        self._raw_tamper(
            "UPDATE events SET row_hash='x' WHERE seq=2")
        v = self.w.verify_chain()
        self.assertFalse(v["ok"])
        self.assertEqual(v["bad"]["seq"], 2)

    def test_sanity_fails_loud_on_tamper(self):
        self.w.append_event("message", {"text": "a"}, actor="master")
        self._raw_tamper(
            "UPDATE events SET payload_json='{\"text\":\"x\"}' WHERE seq=1")
        self.w.close()
        with self.assertRaises(self.writer.StoreCorruptError):
            self.writer.open_core(self.tmp / "core.db", clock=self.clock)
        w = self.writer.open_core(self.tmp / "core.db", clock=self.clock,
                                  sanity=False)
        report = w.sanity_check()
        self.assertFalse(report["ok"])
        self.w = w


class TestSanityAndManifest(WriterTestBase):
    def test_sanity_fresh_ok(self):
        report = self.w.sanity_check()
        self.assertTrue(report["ok"],
                        [c for c in report["checks"] if not c["ok"]])

    def test_manifest_pin_current(self):
        res = self.writer.verify_manifest()
        self.assertTrue(res["ok"], res["detail"])

    def test_manifest_detects_drift(self):
        kdir = self.tmp / "fake_kernel"
        kdir.mkdir()
        (kdir / "a.py").write_text("x = 1\n")
        res = self.writer.verify_manifest(kdir)
        self.assertFalse(res["ok"])
        manifest = self.writer.compute_manifest(kdir)
        (kdir / "manifest.json").write_text(
            __import__("json").dumps(manifest))
        self.assertTrue(self.writer.verify_manifest(kdir)["ok"])
        (kdir / "a.py").write_text("x = 2\n")
        self.assertFalse(self.writer.verify_manifest(kdir)["ok"])


if __name__ == "__main__":
    unittest.main()
