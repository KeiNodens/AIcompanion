"""P8-5 L-VER のテスト

カバー: kernel.oracle（fold との意味層独立実装）の一致・reserved 値拒否・
hash 鎖の逆方向検査、Verifier.rederive_verdicts（乖離ゼロ＋乖離検出）、
sweep 12種（detect-only）、conformance suite（append-only trigger・第2接続・
receipt-first・supply gate floor・derived digest）、family 単位部分 rebuild
（Q-O48）。
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class OracleTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        cls.canon = cls._iso.import_module("companion.kernel.canon")
        cls.events = cls._iso.import_module("companion.kernel.events")
        cls.fold = cls._iso.import_module("companion.kernel.fold")
        cls.oracle = cls._iso.import_module("companion.kernel.oracle")
        cls.writer = cls._iso.import_module("companion.kernel.writer")
        cls.intake = cls._iso.import_module("companion.adapters.intake")
        cls.admission = cls._iso.import_module("companion.adapters.admission")
        cls.verify = cls._iso.import_module("companion.adapters.verify")
        cls.derived = cls._iso.import_module("companion.adapters.derived")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)


def _mem_state(rid, text, canon, scope="user", version=1):
    return {
        "record_id": rid, "record_family": "memory",
        "scope": scope, "status": "active", "record_version": version,
        "memory_type": "fact", "content": text,
        "content_hash": canon.content_hash(text),
        "created_at": "2026-10-09T10:00:00.000000Z",
        "updated_at": "2026-10-09T10:00:00.000000Z",
    }


class TestOracle(OracleTestBase):
    """合成 corpus で fold/oracle が一致することを確認（DB 不使用）。"""

    def _row(self, seq, et, payload, prev=None):
        e = self.events.build_event(
            event_id=f"ev{seq}", seq=seq, event_type=et,
            actor="companion",
            recorded_at="2026-10-09T10:00:00.000000Z",
            payload=payload,
            prev_hash=prev or self.events.GENESIS_HASH)
        e["payload"] = payload  # parse_row 済み形にする
        return e

    def _corpus(self):
        rows = []
        prev = None
        st1 = _mem_state("m1", "ラーメン好き", self.canon)
        r = self._row(1, "memory_write",
                      {"memory_id": "m1", "new_state": st1,
                       "reason": "t", "basis_events": []}, prev)
        prev = r["row_hash"]
        rows.append(r)
        st2 = dict(st1, record_version=2,
                   retrieval_strength=0.5)
        r = self._row(2, "memory_update",
                      {"memory_id": "m1", "new_state": st2,
                       "op": "param_update", "reason": "t",
                       "basis_events": []}, prev)
        prev = r["row_hash"]
        rows.append(r)
        rows.append(self._row(3, "context_assembly",
                              {"injected": [], "inputs_hash": "x",
                               "basis_seq": 2}, prev))
        prev = rows[-1]["row_hash"]
        rows.append(self._row(4, "future_event_type_xyz",
                              {"note": "unknown non-reserved"}, prev))
        return rows

    def test_oracle_matches_fold_on_synthetic(self):
        rows = self._corpus()
        f = self.fold.fold_rows(rows)
        o = self.oracle.oracle_fold(rows)
        cmp = self.oracle.compare(f, o)
        self.assertTrue(cmp["ok"], cmp["diffs"])
        # unhandled（未知型）も両実装で同じ計数であること（SD-63 の盲点排除）
        self.assertEqual(f["unhandled_event_types"],
                         {"future_event_type_xyz": 1})

    def test_oracle_rejects_reserved_value(self):
        rows = self._corpus()
        bad = self._row(5, "memory_write",
                        {"memory_id": "m9",
                         "new_state": _mem_state("m9", "x", self.canon),
                         "reason": "t", "basis_events": []},
                        rows[-1]["row_hash"])
        bad["event_type"] = "@core/reserved_type"
        rows.append(bad)
        with self.assertRaises(self.fold.FoldError):
            self.fold.fold_rows(rows)
        with self.assertRaises(self.oracle.OracleError):
            self.oracle.oracle_fold(rows)

    def test_oracle_chain_backward_walk(self):
        rows = self._corpus()
        res = self.oracle.oracle_verify_chain(rows)
        self.assertTrue(res["ok"], res["errors"])
        # 改竄（中間行の row_hash 書換え）を逆方向走査が拾う
        tampered = [dict(r) for r in rows]
        tampered[1]["row_hash"] = "0" * 64
        res2 = self.oracle.oracle_verify_chain(tampered)
        self.assertFalse(res2["ok"])
        self.assertTrue(any(e["seq"] == 2 for e in res2["errors"]))


class TestVerifier(OracleTestBase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="verify_test_"))
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
        self.ik = self.intake.Intake(self.w, clock=sclock)
        self.adm = self.admission.Admission(self.w, clock=sclock)
        self.verifier = self.verify.Verifier(
            self.w, derived=self.derived.DerivedStore(
                self.w, dir=self.tmp, clock=sclock))
        self.ik.register_adapter({
            "adapter_id": "test-adapter", "adapter_version": "v1",
            "coverage": {"classes": ["conversation"]},
            "capabilities": []})

    def tearDown(self):
        self.w.close()

    def _ingest(self, body="ラーメンの話"):
        env = {
            "envelope_id": "e1", "adapter_id": "test-adapter",
            "format_version": "pcore.envelope.v1", "stream_id": "s1",
            "span": {"start_cursor": "1", "end_cursor": "1"},
            "coverage": {"classes": ["conversation"]},
            "parts": [{"part_id": "p1", "class": "conversation",
                       "origin": "user",
                       "ts": "2026-10-09T10:00:00.000000Z",
                       "body": body}],
        }
        self.ik.submit_envelope(env)
        self.ik.drain()
        return self.w._conn.execute(
            "SELECT event_id FROM events WHERE event_type='message'"
            " LIMIT 1").fetchone()["event_id"]

    def _proposal(self, eid, rid="mem_v1", text="マスターはラーメンが好き"):
        return {
            "record_family": "memory", "target_id": None,
            "new_state": _mem_state(rid, text, self.canon),
            "basis_events": [{"event_id": eid}],
            "generated_by": "extraction",
            "generator": {"method": "t", "model": "m", "version": "0"},
            "producer": "extraction",
        }

    def _populated(self):
        eid = self._ingest()
        r1 = self.adm.submit(self._proposal(eid))
        self.assertEqual(r1["verdict"], "accept")
        return eid, r1

    def test_rederive_zero_divergence(self):
        self._populated()
        res = self.verifier.rederive_verdicts()
        self.assertTrue(res["ok"], res["mismatches"])
        self.assertGreaterEqual(res["proposals"], 1)

    def test_rederive_detects_divergence(self):
        """verdict 判定が変わった場合に乖離として報告されること
        （ctx 再構成経路の健全性確認。evaluate をスタブして強制乖離）。"""
        self._populated()
        orig = self.admit_evaluate = None
        import companion.kernel.admit as _admit
        orig = _admit.evaluate
        try:
            _admit.evaluate = lambda p, c: {
                "verdict": "reject", "inputs_hash": "x",
                "checks": [], "fired_rules": [], "gate_inputs": {},
                "evidence_basis": "self_derived", "suppression_key": None}
            res = self.verifier.rederive_verdicts()
            self.assertFalse(res["ok"])
            self.assertEqual(res["mismatches"][0]["recorded"], "accept")
            self.assertEqual(res["mismatches"][0]["rederived"], "reject")
        finally:
            _admit.evaluate = orig

    def test_oracle_parity_on_db(self):
        self._populated()
        res = self.verifier.oracle_parity()
        self.assertTrue(res["ok"], res)

    def test_sweep_clean_corpus(self):
        self._populated()
        res = self.verifier.sweep()
        self.assertEqual(len(res["checks"]), 12)
        bad = {k: v for k, v in res["checks"].items() if not v["ok"]}
        self.assertEqual(bad, {})

    def test_sweep_detects_stalled_claim(self):
        self._populated()
        self.w._conn.execute(
            "INSERT INTO claims(claim_id, event_id, processor, status,"
            " seq, updated_at) VALUES ('c_x','e_x','extraction','stalled',"
            " 1,'2026-10-09T10:00:00.000000Z')")
        self.w._conn.commit()
        res = self.verifier.sweep()
        self.assertFalse(res["checks"]["claims_stalled"]["ok"])

    def test_conformance_clean(self):
        self._populated()
        res = self.verifier.conformance()
        bad = {c["name"]: c["detail"] for c in res["checks"]
               if not c["ok"]}
        self.assertEqual(bad, {})

    def test_partial_rebuild_targets(self):
        """Q-O48: family 単位部分 rebuild —— memories のみ再構築し
        tombstones/links は対象外として検証対象からも外れる。"""
        self._populated()
        ds = self.derived.DerivedStore(self.w, dir=self.tmp)
        res = ds.rebuild(targets=["memories"])
        self.assertTrue(res["verify"]["ok"], res["verify"])
        names = {c["index"] for c in res["verify"]["checks"]}
        self.assertTrue(names <= {"mem_search", "mem_fts", "mem_vec"})
        acon, _ = ds.open_active()
        try:
            te = acon.execute(
                "SELECT COUNT(*) FROM tomb_emb").fetchone()[0]
            self.assertEqual(te, 0)
        finally:
            acon.close()
        # 全量 rebuild は引き続き動く
        res2 = ds.rebuild()
        self.assertTrue(res2["verify"]["ok"])


if __name__ == "__main__":
    unittest.main()
