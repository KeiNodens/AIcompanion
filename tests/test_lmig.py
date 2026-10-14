"""test_lmig —— L-MIG（P8-7）の kernel 規則・アダプタ工程・cutover のテスト

カバー:
- import_key 冪等錨・写像表・mem0 値写像（origin=imported 相当の刻印）
- turns/history → event 記述子
- G1 同等性: equiv_match/equiv_coverage/EquivGate
- DeliverState・Reconciler
- Transcriber stage→commit（冪等再実行・migration_map・deliver_state）
- MigVerifier 機械検証
- DualRun verbatim_extractor・g1_report
- Cutover perform/rollback（authority_transition 刻印＋旗往復）
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class MigBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        im = cls._iso.import_module
        cls.canon = im("companion.kernel.canon")
        cls.writer = im("companion.kernel.writer")
        cls.mig = im("companion.kernel.migration")
        cls.migration = im("companion.adapters.migration")
        cls.transcribe = im("companion.adapters.transcribe")
        cls.migverify = im("companion.adapters.migverify")
        cls.dualrun = im("companion.adapters.dualrun")
        cls.cutover = im("companion.adapters.cutover")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def _mk(self, name="mig"):
        tmp = Path(tempfile.mkdtemp(prefix=f"lmig_{name}_"))
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        tick = {"i": 0}

        def clock():
            tick["i"] += 1
            return t0 + timedelta(seconds=tick["i"])

        def sclock():
            return self.canon.canon_ts(t0 + timedelta(seconds=tick["i"]))

        w = self.writer.open_core(tmp / "core.db", clock=clock)
        return tmp, w, sclock


# ------------------------------------------------------------ kernel 規則
class TestKernelMapping(MigBase):
    def test_import_key_deterministic(self):
        a = self.mig.import_key("mem0:user", "abc-1")
        b = self.mig.import_key("mem0:user", "abc-1")
        self.assertEqual(a, b)
        self.assertNotEqual(a, self.mig.import_key("mem0:user", "abc-2"))
        self.assertNotEqual(a, self.mig.import_key("turns", "abc-1"))

    def test_source_map_expected_kinds(self):
        self.assertEqual(self.mig.source_kind("mem0:user")["kind"],
                         "record")
        self.assertEqual(self.mig.source_kind("turns")["kind"], "event")
        self.assertEqual(self.mig.source_kind("docs")["kind"], "blob")
        self.assertEqual(self.mig.source_kind("user_profile")["kind"],
                         "derived")
        self.assertEqual(self.mig.source_kind("未知テーブル")["kind"],
                         "skip")

    def test_mem0_scope(self):
        self.assertEqual(self.mig.mem0_scope({"id": "1"}), "user")
        self.assertEqual(
            self.mig.mem0_scope({"id": "1", "agent_id": "c"}), "agent")
        self.assertEqual(
            self.mig.mem0_scope({"id": "1",
                                 "metadata": {"agent_id": "c"}}), "agent")

    def test_mem0_item_to_state_stamps(self):
        st = self.mig.mem0_item_to_state(
            {"id": "m1", "memory": "牛乳が好き",
             "metadata": {"importance": 5, "confidence": 0.9},
             "created_at": "2026-01-01T00:00:00Z"},
            "user", "k1", created_now="2026-10-10T00:00:00Z")
        self.assertEqual(st["record_family"], "memory")
        self.assertEqual(st["scope"], "user")
        self.assertEqual(st["content"], "牛乳が好き")
        self.assertEqual(st["importance"], 5)
        self.assertEqual(st["retrieval_strength"], 0.9)
        self.assertEqual(st["tier"], "hot")
        self.assertEqual(st["import_key"], "k1")
        prov = st["provenance"]
        self.assertEqual(prov["generated_by"], "migration")
        self.assertEqual(prov["evidence_basis"], "migrated_unknown")
        self.assertEqual(prov["source"]["memory_id"], "m1")
        self.assertEqual(st["record_id"],
                         self.mig.migrated_record_id("k1", "mem"))

    def test_map_strength_clamps(self):
        self.assertEqual(self.mig.map_strength({"confidence": 2.0}), 1.0)
        self.assertEqual(self.mig.map_strength({"confidence": 0.0}), 0.05)
        self.assertEqual(self.mig.map_strength({}), 0.5)

    def test_turn_row_to_events(self):
        evs = self.mig.turn_row_to_events(
            {"id": 7, "user_text": "u", "assistant_text": "a",
             "inner_text": "i", "ts": "2026-09-01T10:00:00"}, "k")
        self.assertEqual([e["event_type"] for e in evs],
                         ["message", "response", "inner"])
        self.assertEqual(evs[0]["actor"], "master")
        self.assertEqual(evs[1]["actor"], "companion")
        self.assertEqual(evs[0]["occurred_at"], "2026-09-01T10:00:00")
        no_inner = self.mig.turn_row_to_events(
            {"id": 8, "user_text": "u", "assistant_text": "a",
             "inner_text": None, "ts": "x"}, "k")
        self.assertEqual(len(no_inner), 2)

    def test_equiv_match_by_source_id(self):
        old = {"id": "m1", "memory": "なにかの記憶"}
        new = {"content": "別文",
               "provenance": {"source": {"memory_id": "m1"}}}
        self.assertTrue(self.mig.equiv_match(old, new))
        new2 = {"content": "なにかの記憶"}
        self.assertTrue(self.mig.equiv_match(old, new2))

    def test_equiv_coverage(self):
        old = [{"id": "a", "memory": "牛乳が好き"},
               {"id": "b", "memory": "週末は映画を見た"}]
        new = [{"content": "牛乳が好き"},
               {"content": "全く違う内容の記憶"}]
        cov = self.mig.equiv_coverage(old, new)
        self.assertEqual(cov["total"], 2)
        self.assertEqual(cov["covered"], 1)
        self.assertEqual(cov["coverage"], 0.5)
        self.assertEqual(cov["missed"], ["b"])


# ------------------------------------------------------------ EquivGate
class TestEquivGate(MigBase):
    def test_gate_passes_on_full_coverage(self):
        gate = self.migration.EquivGate()
        queries = [{"id": "q1", "text": "好きなもの",
                    "must_include": [{"id": "a", "memory": "牛乳が好き"}]}]
        old = lambda q: [{"id": "a", "memory": "牛乳が好き"}]
        new = lambda q: [{"content": "牛乳が好き",
                          "provenance": {"source": {"memory_id": "a"}}}]
        res = gate.evaluate(queries, old, new)
        self.assertTrue(res["ok"])

    def test_gate_fails_on_missing(self):
        gate = self.migration.EquivGate()
        queries = [{"id": "q1", "text": "x"}]
        res = gate.evaluate(queries,
                            lambda q: [{"id": "a", "memory": "牛乳"}],
                            lambda q: [{"content": "別物"}])
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed"], 1)
        self.assertEqual(res["results"][0]["must_missing"], [])

    def test_gate_must_include_missing_fails(self):
        gate = self.migration.EquivGate(coverage_floor=0.0)
        queries = [{"id": "q1", "text": "x",
                    "must_include": [{"id": "a", "memory": "牛乳"}]}]
        res = gate.evaluate(queries, lambda q: [], lambda q: [])
        self.assertFalse(res["ok"])
        self.assertEqual(res["results"][0]["must_missing"], ["a"])


# ------------------------------------------------ DeliverState/Reconciler
class TestDeliverReconcile(MigBase):
    def _inventory(self, tr):
        return tr.build_inventory(
            mem0_items=[{"id": "m1", "memory": "牛乳が好き",
                         "metadata": {"importance": 4}},
                        {"id": "m2", "memory": "映画が好き",
                         "metadata": {"agent_id": "companion"}}],
            turn_rows=[{"id": 1, "user_text": "u", "assistant_text": "a",
                        "inner_text": None, "ts": "2026-09-01"}],
            db_rows={}, state_values={}, blobs=[])

    def test_deliver_state_marks(self):
        _, w, sclock = self._mk()
        ds = self.migration.DeliverState(w, clock=sclock)
        with w.transaction() as conn:
            ds.mark(conn, "k1", "mem0:user", "m1", "records",
                    status="staged", run_id="r1")
            self.assertEqual(ds.get(conn, "k1")["status"], "staged")
            ds.mark(conn, "k1", "mem0:user", "m1", "records",
                    target_id="rid1", status="delivered", run_id="r1")
            self.assertEqual(ds.get(conn, "k1")["status"], "delivered")
            self.assertEqual(ds.counts(conn, "r1"), {"delivered": 1})

    def test_reconciler_reports_states(self):
        _, w, sclock = self._mk()
        tr = self.transcribe.Transcriber(w, clock=sclock)
        inv = self._inventory(tr)
        rec = self.migration.Reconciler(w)
        res0 = rec.reconcile(inv)
        self.assertEqual(res0["counts"]["missing"], len(inv))
        tr.stage(inv, "r1")
        res1 = rec.reconcile(inv)
        self.assertGreater(res1["counts"]["staged"], 0)
        tr.commit("r1")
        res2 = rec.reconcile(inv)
        self.assertTrue(res2["ok"], res2["counts"])
        self.assertGreater(res2["counts"]["delivered"], 0)


# ---------------------------------------------------------------- stage/commit
class TestTranscribe(MigBase):
    def test_stage_and_commit_idempotent(self):
        _, w, sclock = self._mk()
        tr = self.transcribe.Transcriber(w, clock=sclock)
        inv = tr.build_inventory(
            mem0_items=[{"id": "m1", "memory": "牛乳が好き",
                         "metadata": {"importance": 4}},
                        {"id": "m2", "memory": "映画が好き",
                         "metadata": {"agent_id": "companion"}}],
            turn_rows=[{"id": 1, "user_text": "こんにちは",
                        "assistant_text": "こんにちは！",
                        "inner_text": "うれしい",
                        "ts": "2026-09-01T00:00:00"}],
            files=[], db_rows={}, state_values={}, blobs=[])
        c1 = tr.stage(inv, "r1")
        self.assertEqual(c1["staged_records"], 2)
        self.assertEqual(c1["staged_events"], 3)   # u/a/inner
        res1 = tr.commit("r1")
        self.assertEqual(res1["records"], 2)
        self.assertEqual(res1["events"], 3)
        self.assertFalse(res1["failed"], res1["failed"])

        # 再 commit は冪等（deliver 済みの stage 残留は staged のまま）
        res2 = tr.commit("r1")
        self.assertEqual(res2["records"], 0)
        self.assertEqual(res2["events"], 0)

        with w.transaction() as conn:
            n_mem = conn.execute(
                "SELECT COUNT(*) c FROM records"
                " WHERE record_family='memory'").fetchone()["c"]
            self.assertEqual(n_mem, 2)
            st = self.canon.loads(conn.execute(
                "SELECT state_json FROM records WHERE scope='user'"
                " AND record_family='memory'").fetchone()["state_json"])
            self.assertEqual(st["content"], "牛乳が好き")
            self.assertEqual(st["provenance"]["generated_by"],
                             "migration")
            n_mm = conn.execute(
                "SELECT COUNT(*) c FROM migration_map").fetchone()["c"]
            self.assertEqual(n_mm, 2)
            n_msg = conn.execute(
                "SELECT COUNT(*) c FROM events WHERE event_type"
                " IN ('message','response','inner')").fetchone()["c"]
            self.assertEqual(n_msg, 3)

    def test_imported_origin_in_state(self):
        _, w, sclock = self._mk("origin")
        tr = self.transcribe.Transcriber(w, clock=sclock)
        inv = tr.build_inventory(
            mem0_items=[{"id": "m1", "memory": "x", "metadata": {}}],
            turn_rows=[], files=[], db_rows={}, state_values={}, blobs=[])
        tr.stage(inv, "r1")
        tr.commit("r1")
        with w.transaction() as conn:
            st = self.canon.loads(conn.execute(
                "SELECT state_json FROM records").fetchone()["state_json"])
        self.assertEqual(st["import_key"],
                         self.mig.import_key("mem0:user", "m1"))

    def test_machine_checks_on_clean_migration(self):
        _, w, sclock = self._mk("verify")
        tr = self.transcribe.Transcriber(w, clock=sclock)
        inv = tr.build_inventory(
            mem0_items=[{"id": "m1", "memory": "牛乳が好き",
                         "metadata": {"importance": 5}},
                        {"id": "m2", "memory": "猫が好き",
                         "metadata": {"agent_id": "companion"}}],
            turn_rows=[], files=[], db_rows={}, state_values={}, blobs=[])
        tr.stage(inv, "r1")
        tr.commit("r1")
        res = self.migverify.MigVerifier(w).machine_checks(inv)
        self.assertTrue(res["ok"], res["failed"])


# ----------------------------------------------------------------- DualRun
class TestDualRun(MigBase):
    def test_verbatim_extractor(self):
        ext = self.dualrun.verbatim_extractor
        ev = {"event_type": "message", "event_id": "e1",
              "payload": {"body": "牛乳を買った"},
              "occurred_at": "2026-10-09T00:00:00Z"}
        props = ext(ev)
        self.assertEqual(len(props), 1)
        st = props[0]["new_state"]
        self.assertEqual(st["scope"], "user")
        self.assertEqual(st["content"], "牛乳を買った")
        self.assertEqual(props[0]["producer"], "extraction")
        self.assertEqual(ext({"event_type": "migration",
                              "payload": {}}), [])
        self.assertEqual(ext({"event_type": "message",
                              "payload": {"body": "  "}}), [])

    def test_g1_report_covers_migrated(self):
        _, w, sclock = self._mk("g1")
        tr = self.transcribe.Transcriber(w, clock=sclock)
        inv = tr.build_inventory(
            mem0_items=[{"id": "m1", "memory": "牛乳が好き",
                         "metadata": {}}],
            turn_rows=[], files=[], db_rows={}, state_values={}, blobs=[])
        tr.stage(inv, "r1")
        tr.commit("r1")
        dr = self.dualrun.DualRun(w, extraction=None, admission=None)
        rep = dr.g1_report([{"id": "m1", "memory": "牛乳が好き"}], "user")
        self.assertTrue(rep["ok"], rep)
        rep2 = dr.g1_report([{"id": "mX", "memory": "全く別物の記憶文"}],
                            "user")
        self.assertFalse(rep2["ok"])


# ----------------------------------------------------------------- Cutover
class TestCutover(MigBase):
    def test_perform_sets_flag_and_event(self):
        _, w, sclock = self._mk("cut")
        state = {"memory_authority": "dual_run"}
        co = self.cutover.Cutover(
            w,
            update_state=lambda patch: state.update(patch),
            load_state=lambda: state)
        res = co.perform(from_mode="dual_run", run_id="r1", grace_days=14,
                         now=datetime(2026, 10, 10, tzinfo=timezone.utc))
        self.assertTrue(res["ok"])
        self.assertEqual(state["memory_authority"], "canonical")
        self.assertIn("mem0_grace_until", state)
        self.assertIn("legacy_frozen_at", state)
        with w.transaction() as conn:
            ev = conn.execute(
                "SELECT payload_json FROM events"
                " WHERE event_type='authority_transition'").fetchone()
        self.assertIsNotNone(ev)
        p = self.canon.loads(ev["payload_json"])
        self.assertEqual(p["to"], "canonical")
        self.assertEqual(p["run_id"], "r1")

        # 冪等: 二度目は idempotent 返却（event は増えない）
        res2 = co.perform(from_mode="dual_run", run_id="r1")
        self.assertTrue(res2["idempotent"])

    def test_rollback_flag_roundtrip(self):
        _, w, _ = self._mk("rb")
        state = {"memory_authority": "canonical"}
        co = self.cutover.Cutover(
            w,
            update_state=lambda patch: state.update(patch),
            load_state=lambda: state)
        res = co.rollback(reason="g1 fail")
        self.assertTrue(res["ok"])
        self.assertEqual(state["memory_authority"], "mem0")
        self.assertEqual(state["rollback_reason"], "g1 fail")
        self.assertEqual(co.authority(), "mem0")


if __name__ == "__main__":
    unittest.main()
