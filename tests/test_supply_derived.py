"""P8-3 抽出＋supply＋derived のテスト

カバー: derived 空 rebuild→index_status 一致・vec0∪fts_or union recall・
canonical fallback・supply_admit（scope/status/sensitivity）・abstention・
context_assembly receipt 必須・extraction 水位/poison/claim 遷移・
増分 index_pending。
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class SupplyTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        cls.writer = cls._iso.import_module("companion.kernel.writer")
        cls.canon = cls._iso.import_module("companion.kernel.canon")
        cls.supply_k = cls._iso.import_module("companion.kernel.supply")
        cls.extract_k = cls._iso.import_module("companion.kernel.extract")
        cls.intake = cls._iso.import_module("companion.adapters.intake")
        cls.admission = cls._iso.import_module("companion.adapters.admission")
        cls.extraction = cls._iso.import_module("companion.adapters.extraction")
        cls.derived = cls._iso.import_module("companion.adapters.derived")
        cls.supply = cls._iso.import_module("companion.adapters.supply")
        cls.embedder = cls._iso.import_module("companion.adapters.embedder")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="supply_test_"))
        self.t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        self._tick = {"i": 0}

        def clock():
            self._tick["i"] += 1
            return self.t0 + timedelta(seconds=self._tick["i"])

        def sclock():
            self._tick["i"] += 1
            return self.canon.canon_ts(
                self.t0 + timedelta(seconds=self._tick["i"]))

        self.w = self.writer.open_core(self.tmp / "core.db", clock=clock)
        self.adm = self.admission.Admission(self.w, clock=sclock)
        self.ds = self.derived.DerivedStore(self.w, dir=self.tmp,
                                            clock=sclock)
        self.emb = self.embedder.TrigramHashEmbedder(dims=384)
        self.sup = self.supply.Supply(self.w, self.ds, embedder=self.emb,
                                    clock=sclock)

    def tearDown(self):
        self.w.close()

    def _write_mem(self, rid, content, scope="user", status="active", **kw):
        st = {
            "record_id": rid, "record_family": "memory", "scope": scope,
            "status": status, "record_version": 1, "memory_type": "fact",
            "content": content,
            "content_hash": self.canon.content_hash(content),
            "importance": 2, "retrieval_strength": 0.5,
            "created_at": "2026-10-09T10:00:00.000000Z",
            "updated_at": "2026-10-09T10:00:00.000000Z",
            **kw}
        return self.w.apply_mutation(
            "memory_write",
            {"memory_id": rid, "new_state": st, "reason": "test"},
            actor="companion")


class TestDerivedBuild(SupplyTestBase):
    def test_rebuild_empty_then_populated(self):
        # 空で rebuild → gen1 active・index_status は 0 件
        r0 = self.ds.rebuild(embedder=self.emb)
        self.assertTrue(r0["active"])
        self.assertEqual(r0["gen"], 1)

        self._write_mem("mem_a", "マスターはラーメンが好き")
        self._write_mem("mem_b", "マスターの好きな映画はもののけ姫")
        r1 = self.ds.rebuild(embedder=self.emb)
        self.assertTrue(r1["active"])
        self.assertEqual(r1["gen"], 2)
        # gen2 が active、gen1 は rollback 窓として残る
        rows = self.w._conn.execute(
            "SELECT gen, is_active FROM derived_generations"
            " ORDER BY gen").fetchall()
        self.assertEqual([(r["gen"], r["is_active"]) for r in rows],
                         [(1, 0), (2, 1)])
        # index_status 件数一致
        acon, gen = self.ds.open_active()
        self.assertEqual(gen, 2)
        n = acon.execute("SELECT COUNT(*) FROM mem_search").fetchone()[0]
        self.assertEqual(n, 2)
        st = {r["name"]: r["rows_count"] for r in acon.execute(
            "SELECT name, rows_count FROM index_status")}
        self.assertEqual(st["mem_search"], 2)
        self.assertEqual(st["mem_fts"], 2)
        acon.close()

    def test_rebuild_deterministic_digest(self):
        self._write_mem("mem_a", "テスト記憶")
        self.ds.rebuild(embedder=self.emb)
        acon, _ = self.ds.open_active()
        base = acon.execute(
            "SELECT value FROM derived_meta WHERE key='canonical_digest'"
        ).fetchone()["value"]
        acon.close()
        self.ds.rebuild(embedder=self.emb)
        acon, _ = self.ds.open_active()
        again = acon.execute(
            "SELECT value FROM derived_meta WHERE key='canonical_digest'"
        ).fetchone()["value"]
        acon.close()
        self.assertEqual(base, again)

    def test_rebuild_emits_migration_marker(self):
        self.ds.rebuild(embedder=self.emb)
        row = self.w._conn.execute(
            "SELECT payload_json FROM events WHERE event_type='migration'"
            " ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertEqual(
            self.canon.loads(row["payload_json"])["action"],
            "rebuild_indexes")


class TestRecall(SupplyTestBase):
    def setUp(self):
        super().setUp()
        self._write_mem("mem_ramen", "マスターはラーメンが好き")
        self._write_mem("mem_movie", "マスターの好きな映画はもののけ姫")
        self._write_mem("mem_agent", "私はマスターと初めて話した",
                        scope="agent")
        self.ds.rebuild(embedder=self.emb)

    def test_union_recall_returns_memory(self):
        r = self.sup._recall("user", "ラーメン", 5)
        ids = [i["record_id"] for i in r["items"]]
        self.assertIn("mem_ramen", ids)

    def test_scope_isolation(self):
        r = self.sup._recall("agent", "ラーメン", 5)
        ids = [i["record_id"] for i in r["items"]]
        self.assertNotIn("mem_ramen", ids)

    def test_abstention_no_match(self):
        r = self.sup._recall("user", "qqqqqqqqq", 5)
        self.assertEqual(r["items"], [])
        self.assertIn(r["abstain"], ("no_match", "below_threshold"))

    def test_canonical_fallback_without_derived(self):
        # derived を壊しても canonical 併合走査に落ちる
        sup2 = self.supply.Supply(self.w, None, embedder=self.emb)
        r = sup2._recall("user", "ラーメン", 5)
        ids = [i["record_id"] for i in r["items"]]
        self.assertIn("mem_ramen", ids)

    def test_status_filtered_not_recalled(self):
        self._write_mem("mem_old", "昔の記憶", status="superseded")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "昔の記憶", 5)
        ids = [i["record_id"] for i in r["items"]]
        self.assertNotIn("mem_old", ids)


class TestSupplyContext(SupplyTestBase):
    def setUp(self):
        super().setUp()
        self._write_mem("mem_ramen", "マスターはラーメンが好き")
        self.ds.rebuild(embedder=self.emb)

    def test_supply_emits_receipt_first(self):
        out = self.sup.supply_context(
            scopes=["user"], query="ラーメン", purpose="turn",
            consumer="test")
        self.assertTrue(out["receipt"]["accepted"])  # receipt は {accepted, event_id, seq}
        ev = self.w._conn.execute(
            "SELECT payload_json FROM events WHERE event_type="
            "'context_assembly'").fetchone()
        pl = self.canon.loads(ev["payload_json"])
        self.assertEqual(pl["purpose"], "turn")
        self.assertEqual(pl["consumer"], "test")
        self.assertTrue(pl["basis_seq"] >= 1)
        injected_ids = [i["id"] for i in pl["injected"]]
        self.assertIn("mem_ramen", injected_ids)

    def test_scope_required(self):
        with self.assertRaises(ValueError):
            self.sup.supply_context(scopes=[], query="x")

    def test_purpose_gate(self):
        with self.assertRaises(ValueError):
            self.sup.supply_context(scopes=["user"], query="x",
                                    purpose="bogus")

    def test_no_receipt_no_leakage(self):
        # _recall は receipt を刻まない（供給面は supply_context のみ）
        before = self.w._conn.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0]
        self.sup._recall("user", "ラーメン", 5)
        after = self.w._conn.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(before, after)

    def test_omitted_counts(self):
        self._write_mem("mem_hidden", "隠し記憶", sensitivity="hidden")
        self.ds.rebuild(embedder=self.emb)
        out = self.sup.supply_context(
            scopes=["user"], query="隠し記憶", purpose="turn")
        ev = self.w._conn.execute(
            "SELECT payload_json FROM events WHERE event_type="
            "'context_assembly' ORDER BY seq DESC LIMIT 1").fetchone()
        pl = self.canon.loads(ev["payload_json"])
        self.assertGreaterEqual(
            pl["omitted"].get("sensitivity_blocked", 0), 1)


class TestExtraction(SupplyTestBase):
    def _extractor(self, ev):
        body = ((ev.get("payload") or {}).get("body")) or ""
        if "好き" in body:
            return [{
                "record_family": "memory",
                "new_state": {
                    "record_id": "mem_ext1", "record_family": "memory",
                    "scope": "user", "status": "active",
                    "record_version": 1, "memory_type": "preference",
                    "content": "マスターの好みが示された",
                    "content_hash": self.canon.content_hash("x"),
                    "created_at": "2026-10-09T10:00:00.000000Z",
                    "updated_at": "2026-10-09T10:00:00.000000Z",
                    "importance": 1,
                },
                "basis_events": [{"event_id": ev["event_id"]}],
                "basis_seq": ev["seq"],
                "generated_by": "extraction",
                "generator": {"method": "t", "model": "m", "version": "0"},
                "producer": "extraction",
            }]
        return []

    def test_run_extracts_and_marks(self):
        ik = self.intake.Intake(self.w)
        ik.register_adapter({"adapter_id": "t", "adapter_version": "v1",
                             "coverage": {"classes": ["conversation"]}})
        ik.submit_envelope({
            "envelope_id": "e1", "adapter_id": "t",
            "format_version": "pcore.envelope.v1", "stream_id": "s",
            "span": {"start_cursor": "1", "end_cursor": "1"},
            "coverage": {"classes": ["conversation"]},
            "parts": [{"part_id": "m1", "class": "conversation",
                       "origin": "user",
                       "ts": "2026-10-09T10:00:00.000000Z",
                       "body": "私はラーメンが好き"}]})
        ik.drain()

        ex = self.extraction.Extraction(self.w, self.adm,
                                      extractor=self._extractor)
        out = ex.run()
        self.assertEqual(out["proposed"], 1)
        # extraction_scanned marker
        ev = self.w._conn.execute(
            "SELECT payload_json FROM events WHERE event_type="
            "'extraction_scanned'").fetchone()
        self.assertIsNotNone(ev)
        pl = self.canon.loads(ev["payload_json"])
        self.assertEqual(pl["proposed"], 1)
        # claim が stored に遷移
        c = self.w._conn.execute(
            "SELECT status FROM claims").fetchone()
        self.assertEqual(c["status"], "stored")
        # memory が admission 経由で書かれた
        self.assertIsNotNone(self.w._conn.execute(
            "SELECT 1 FROM records WHERE record_id='mem_ext1'").fetchone())
        self.assertTrue(self.w.replay_check()["ok"])

    def test_poison_isolation(self):
        def boom(ev):
            raise RuntimeError("extract fail")
        ex = self.extraction.Extraction(self.w, self.adm, extractor=boom)
        self.w.append_event("message", {"body": "x"}, actor="master")
        total = 0
        for _ in range(4):  # POISON_MAX_ATTEMPTS=3 を超えるまで回す
            total += ex.run()["poisoned"]
        self.assertEqual(total, 1)
        ev = self.w._conn.execute(
            "SELECT 1 FROM events WHERE event_type='extraction_poisoned'"
        ).fetchone()
        self.assertIsNotNone(ev)
        # 水位は poison を超えて進む
        wm = self.w._conn.execute(
            "SELECT last_done_seq FROM watermarks WHERE processor='extraction'"
        ).fetchone()["last_done_seq"]
        poison_seq = self.w._conn.execute(
            "SELECT seq FROM events WHERE event_type='extraction_poisoned'"
        ).fetchone()[0]
        self.assertGreaterEqual(wm, poison_seq)

    def test_material_gate(self):
        # mutation 系・context_assembly・approval は材料にならない
        for et in ("memory_write", "context_assembly", "approval",
                   "message"):
            ok, _ = self.extract_k.material_ok({"event_type": et})
            self.assertEqual(ok, et == "message")


class TestIncremental(SupplyTestBase):
    def test_index_pending(self):
        self._write_mem("mem_a", "最初の記憶")
        self.ds.rebuild(embedder=self.emb)
        self._write_mem("mem_b", "追加分の記憶")
        out = self.ds.index_pending(embedder=self.emb)
        self.assertTrue(out["ok"])
        self.assertEqual(out["indexed"], 1)
        acon, _ = self.ds.open_active()
        n = acon.execute("SELECT COUNT(*) FROM mem_search").fetchone()[0]
        acon.close()
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
