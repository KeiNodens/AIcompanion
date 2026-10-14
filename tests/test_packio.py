"""P8-6 L-EXP pack export/import のテスト

カバー: export manifest の構造・digest 照合、staged→test-apply→commit
の検疫フロー、round-trip（migration-m5 §5.3: replay digest・record 数・
content_hash 一致）、冪等再実行、破損 pack の検出、needs_repair 集計。
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class PackTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        cls.canon = cls._iso.import_module("companion.kernel.canon")
        cls.writer = cls._iso.import_module("companion.kernel.writer")
        cls.pack = cls._iso.import_module("companion.kernel.pack")
        cls.intake = cls._iso.import_module("companion.adapters.intake")
        cls.admission = cls._iso.import_module("companion.adapters.admission")
        cls.packio = cls._iso.import_module("companion.adapters.packio")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def _mk(self, name="src"):
        tmp = Path(tempfile.mkdtemp(prefix=f"pack_{name}_"))
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        tick = {"i": 0}

        def clock():
            tick["i"] += 1
            return t0 + timedelta(seconds=tick["i"])

        def sclock():
            tick["i"] += 1
            return self.canon.canon_ts(t0 + timedelta(seconds=tick["i"]))

        w = self.writer.open_core(tmp / "core.db", clock=clock)
        return tmp, w, sclock

    def _populate(self, tmp, w, sclock):
        ik = self.intake.Intake(w, clock=sclock)
        adm = self.admission.Admission(w, clock=sclock)
        ik.register_adapter({
            "adapter_id": "a1", "adapter_version": "v1",
            "coverage": {"classes": ["conversation"]}, "capabilities": []})
        ik.submit_envelope({
            "envelope_id": "e1", "adapter_id": "a1",
            "format_version": "pcore.envelope.v1", "stream_id": "s1",
            "span": {"start_cursor": "1", "end_cursor": "1"},
            "coverage": {"classes": ["conversation"]},
            "parts": [{"part_id": "p1", "class": "conversation",
                       "origin": "user",
                       "ts": "2026-10-09T10:00:00.000000Z",
                       "body": "ラーメンの話"}]})
        ik.drain()
        eid = w._conn.execute(
            "SELECT event_id FROM events WHERE event_type='message'"
            " LIMIT 1").fetchone()["event_id"]
        text = "マスターはラーメンが好き"
        prop = {
            "record_family": "memory", "target_id": None,
            "new_state": {
                "record_id": "m1", "record_family": "memory",
                "scope": "user", "status": "active", "record_version": 1,
                "memory_type": "fact", "content": text,
                "content_hash": self.canon.content_hash(text),
                "created_at": "2026-10-09T10:01:00.000000Z",
                "updated_at": "2026-10-09T10:01:00.000000Z",
            },
            "basis_events": [{"event_id": eid}],
            "generated_by": "extraction",
            "generator": {"method": "t", "model": "m", "version": "0"},
            "producer": "extraction",
        }
        r = adm.submit(prop)
        assert r["verdict"] == "accept", r
        return ik, adm


class TestPack(PackTestBase):
    def setUp(self):
        self.tmp, self.w, self.sclock = self._mk("src")
        self.io = self.packio.PackIO(self.w, clock=self.sclock)
        self._populate(self.tmp, self.w, self.sclock)
        self.addCleanup(self.w.close)

    def _export(self):
        d = self.tmp / "pack"
        man = self.io.export_pack(d)
        return d, man

    def _import_into_empty(self, pack_dir):
        tmp2, w2, sc2 = self._mk("dst")
        io2 = self.packio.PackIO(w2, clock=sc2)
        run = io2.stage(pack_dir)
        return tmp2, w2, io2, run

    def test_export_manifest_shape(self):
        d, man = self._export()
        self.assertEqual(man["format"], "pcore.pack.v1")
        self.assertEqual(man["mode"], "full")
        self.assertEqual(man["counts"]["events"],
                         self.w._conn.execute(
                             "SELECT COUNT(*) FROM events").fetchone()[0])
        self.assertTrue(man["replay_digest"])
        self.assertFalse(self.pack.verify_manifest(man))
        self.assertTrue((d / "events.jsonl").exists())
        self.assertTrue((d / "tables" / "records.jsonl").exists())
        self.assertTrue((d / "watermark_hint.json").exists())

    def test_round_trip(self):
        """§5.3: export→空環境 import→validate が機械的に成立する。"""
        d, man = self._export()
        tmp2, w2, io2, run = self._import_into_empty(d)
        self.addCleanup(w2.close)
        ta = io2.test_apply(run["run_id"])
        self.assertTrue(ta["ok"], ta)
        cm = io2.commit(run["run_id"], d)
        self.assertGreater(cm["inserted_events"], 0)
        v = io2.validate_pack(d)
        self.assertTrue(v["ok"], v)

    def test_import_idempotent_reskip(self):
        """pack の events を持つ DB へ import すると event_id 冪等で
        全件 skip される（重複しない）。また同 pack の再 stage は
        run_id 内容一致で既存 run を返す（二重 stage しない）。"""
        d, man = self._export()
        # 同一 DB（pack の events を既に持つ）へ import → 全件 skip
        run = self.io.stage(d)
        ta = self.io.test_apply(run["run_id"])
        self.assertTrue(ta["ok"], ta)
        cm = self.io.commit(run["run_id"], d)
        self.assertEqual(cm["inserted_events"], 0)
        self.assertGreater(cm["skipped_events"], 0)
        n1 = self.w._conn.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0]
        run2 = self.io.stage(d)
        self.assertEqual(run2["run_id"], run["run_id"])
        self.assertTrue(run2.get("resumed"))
        n2 = self.w._conn.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(n2, n1)

    def test_commit_requires_verified(self):
        d, _ = self._export()
        tmp2, w2, io2, run = self._import_into_empty(d)
        self.addCleanup(w2.close)
        with self.assertRaises(self.packio.PackError):
            io2.commit(run["run_id"], d)

    def test_tampered_pack_detected(self):
        """staged event の内容を改竄すると test_apply の鎖検査で失敗する。"""
        d, _ = self._export()
        tmp2, w2, io2, run = self._import_into_empty(d)
        self.addCleanup(w2.close)
        w2._conn.execute(
            "UPDATE staged_events SET row_json="
            " json_replace(row_json,'$.payload_json','{\"tampered\":1}')"
            " WHERE run_id=? AND seq=1", (run["run_id"],))
        w2._conn.commit()
        ta = io2.test_apply(run["run_id"])
        self.assertFalse(ta["ok"])
        self.assertTrue(ta["needs_repair"])

    def test_manifest_digest_mismatch(self):
        d, _ = self._export()
        # manifest の pack_digest を壊す
        man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        man["counts"]["events"] += 1
        (d / "manifest.json").write_text(
            self.canon.dumps(man), encoding="utf-8")
        tmp2, w2, sc2 = self._mk("dst2")
        self.addCleanup(w2.close)
        io2 = self.packio.PackIO(w2, clock=sc2)
        with self.assertRaises(self.packio.PackError):
            io2.stage(d)

    def test_blob_entity_bundled(self):
        """permanent blob の実体が pack に同梱され、commit で復元される。"""
        blob_file = self.tmp / "entity.bin"
        blob_file.write_bytes(b"blob-entity-data")
        st = {
            "record_id": self.canon.hash_bytes("pcore.blob.v1",
                                             b"blob-entity-data"),
            "record_family": "blob", "scope": "user", "status": "active",
            "record_version": 1, "kind": "file", "size": 16,
            "retention_class": "permanent", "ref": str(blob_file),
            "created_at": "2026-10-09T10:02:00.000000Z",
            "updated_at": "2026-10-09T10:02:00.000000Z",
        }
        self.w.apply_mutation(
            "record_write",
            {"record_family": "blob", "record_id": st["record_id"],
             "new_state": st, "reason": "test blob"}, actor="system")
        d, man = self._export()
        self.assertTrue((d / "blobs" / st["record_id"]).exists())
        tmp2, w2, io2, run = self._import_into_empty(d)
        self.addCleanup(w2.close)
        ta = io2.test_apply(run["run_id"])
        self.assertTrue(ta["ok"], ta)
        io2.commit(run["run_id"], d)
        restored = tmp2 / "pack-blobs" / st["record_id"]
        self.assertTrue(restored.exists())
        self.assertEqual(restored.read_bytes(), b"blob-entity-data")


if __name__ == "__main__":
    unittest.main()
