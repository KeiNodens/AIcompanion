"""P8-4 認知機構のテスト

カバー: decay sweep（param_update/demote/pinned 免除/min_delta）・
boost 導出（receipt anchor・SD-10 適格性）・state 投影（現行 inner.py
等価の気分表現）・reflection_trigger・suspect lane・prediction
auto-resolve・規則版 pin（policy_change）・simulate 差分レポート。
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class CogTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        cls.writer = cls._iso.import_module("companion.kernel.writer")
        cls.canon = cls._iso.import_module("companion.kernel.canon")
        cls.rules = cls._iso.import_module("companion.kernel.rules")
        cls.admission = cls._iso.import_module("companion.adapters.admission")
        cls.cognitive = cls._iso.import_module("companion.adapters.cognitive")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cog_test_"))
        self.t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        self._tick = {"i": 0}

        def clock():
            self._tick["i"] += 1
            return self.t0 + timedelta(seconds=self._tick["i"])

        def sclock():
            self._tick["i"] += 1
            return self.canon.canon_ts(
                self.t0 + timedelta(seconds=self._tick["i"]))

        self.sclock = sclock
        self.w = self.writer.open_core(self.tmp / "core.db", clock=clock)
        self.adm = self.admission.Admission(self.w, clock=sclock)
        self.cog = self.cognitive.CognitiveJobs(self.w, self.adm,
                                              clock=sclock)

    def tearDown(self):
        self.w.close()

    def _write_mem(self, rid, content, scope="user", status="active",
                   strength=0.5, last_recalled_at=None, importance=1,
                   **kw):
        st = {
            "record_id": rid, "record_family": "memory", "scope": scope,
            "status": status, "record_version": 1, "memory_type": "fact",
            "content": content,
            "content_hash": self.canon.content_hash(content),
            "importance": importance,
            "retrieval_strength": strength,
            "last_recalled_at": last_recalled_at,
            "created_at": "2026-10-09T10:00:00.000000Z",
            "updated_at": "2026-10-09T10:00:00.000000Z",
            **kw}
        return self.w.apply_mutation(
            "memory_write",
            {"memory_id": rid, "new_state": st, "reason": "test"},
            actor="companion")

    def _write_pred(self, rid, horizon, scoring="deterministic",
                    expected="yes", actual="yes"):
        st = {
            "record_id": rid, "record_family": "prediction",
            "scope": "agent", "status": "active", "record_version": 1,
            "claim": {"text": "マスターは今週ラーメンを食べる",
                      "outcome_check": {"expected": expected,
                                        "actual": actual}},
            "target": {"kind": "master_habit", "ref": "ramen"},
            "horizon": horizon, "scoring": scoring,
            "outcome": "pending",
            "created_at": "2026-10-09T10:00:00.000000Z",
            "updated_at": "2026-10-09T10:00:00.000000Z"}
        return self.w.apply_mutation(
            "record_write",
            {"record_family": "prediction", "record_id": rid,
             "new_state": st, "reason": "test"},
            actor="companion")

    def _assembly(self, injected_ids, purpose="turn"):
        return self.w.append_event(
            "context_assembly",
            {"injected": [{"type": "memory", "id": i}
                          for i in injected_ids],
             "purpose": purpose, "consumer": "test"},
            actor="system")


class TestRulesRegistry(CogTestBase):
    def test_register_rules_pins_policy_state(self):
        out = self.cog.register_rules()
        rules_stamped = {s["rule"] for s in out["stamped"]}
        self.assertIn("decay", rules_stamped)
        self.assertIn("boost", rules_stamped)
        # policy_state に現在版
        row = self.w._conn.execute(
            "SELECT version FROM policy_state WHERE rule_id='decay'"
        ).fetchone()
        self.assertEqual(row["version"], "decay-v1")
        # kernel_table に版 pin
        row = self.w._conn.execute(
            "SELECT params_json FROM kernel_table WHERE"
            " algo='rule:decay' AND version='decay-v1'").fetchone()
        self.assertEqual(
            self.canon.loads(row["params_json"])["weekly_factor"], 0.9)
        # policy_change イベント刻印（rule=decay のもの）
        pl = None
        for ev in self.w._conn.execute(
                "SELECT payload_json FROM events WHERE"
                " event_type='policy_change'"):
            cand = self.canon.loads(ev["payload_json"])
            if cand.get("rule") == "decay":
                pl = cand
        self.assertIsNotNone(pl)
        self.assertEqual(pl["to_version"], "decay-v1")
        # 再登録は冪等（policy_change は増えない）
        n0 = self.w._conn.execute(
            "SELECT COUNT(*) FROM events WHERE"
            " event_type='policy_change'").fetchone()[0]
        self.cog.register_rules()
        n1 = self.w._conn.execute(
            "SELECT COUNT(*) FROM events WHERE"
            " event_type='policy_change'").fetchone()[0]
        self.assertEqual(n0, n1)


class TestDecaySweep(CogTestBase):
    def test_decay_stamps_param_update(self):
        old = self.canon.canon_ts(self.t0 - timedelta(days=100))
        self._write_mem("mem_a", "古い記憶", strength=0.5,
                        last_recalled_at=old, importance=1)
        out = self.cog.decay_sweep()
        self.assertEqual(out["stamped"], 1)
        row = self.w._conn.execute(
            "SELECT retrieval_strength, param_policy_ref, state_json"
            " FROM records WHERE record_id='mem_a'").fetchone()
        # 0.5 * 0.9^(100/7) ≈ 0.11 → floor 超・strength 減少
        self.assertLess(row["retrieval_strength"], 0.5)
        self.assertGreater(row["retrieval_strength"], 0.05)
        self.assertEqual(row["param_policy_ref"], "decay-v1")
        st = self.canon.loads(row["state_json"])
        self.assertEqual(st["param_policy_ref"], "decay-v1")
        # replay 一致（param_update は完全スナップショットで刻印）
        self.assertTrue(self.w.replay_check()["ok"])

    def test_floor_demotes(self):
        old = self.canon.canon_ts(self.t0 - timedelta(days=100))
        self._write_mem("mem_b", "ほぼ消えた記憶", strength=0.06,
                        last_recalled_at=old, importance=1)
        out = self.cog.decay_sweep()
        self.assertEqual(out["demoted"], 1)
        st = self.w._conn.execute(
            "SELECT status FROM records WHERE record_id='mem_b'"
        ).fetchone()
        self.assertEqual(st["status"], "demoted")

    def test_pinned_exempt(self):
        old = self.canon.canon_ts(self.t0 - timedelta(days=100))
        self._write_mem("mem_c", "pin された記憶", strength=0.06,
                        last_recalled_at=old, pinned=True)
        out = self.cog.decay_sweep()
        self.assertEqual(out["demoted"], 0)
        self.assertEqual(out["stamped"], 0)
        st = self.w._conn.execute(
            "SELECT status, retrieval_strength FROM records WHERE"
            " record_id='mem_c'").fetchone()
        self.assertEqual(st["status"], "active")
        self.assertEqual(st["retrieval_strength"], 0.06)

    def test_recent_memory_skipped(self):
        now = self.canon.canon_ts(self.t0)
        self._write_mem("mem_d", "新しい記憶", strength=0.5,
                        last_recalled_at=now)
        out = self.cog.decay_sweep()
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(out["stamped"], 0)


class TestBoost(CogTestBase):
    def test_receipt_boost_derives_strength(self):
        old = self.canon.canon_ts(self.t0 - timedelta(days=10))
        self._write_mem("mem_a", "agent の記憶", scope="agent",
                        strength=0.5, last_recalled_at=old)
        self._assembly(["mem_a"])  # purpose=turn, scope=agent eligible
        st = self.canon.loads(self.w._conn.execute(
            "SELECT state_json FROM records WHERE record_id='mem_a'"
        ).fetchone()["state_json"])
        derived = self.cog.strength_at(st)
        # 10日経過で減衰しても、receipt boost（+0.1*(1-s)）で anchor より上
        stored = st["retrieval_strength"]
        pure_decay = self.rules.decay_value(
            0.5, 10.0, 1, self.rules.params("decay"))
        self.assertGreater(derived, pure_decay)
        self.assertNotEqual(round(derived, 6), round(stored, 6))

    def test_boost_scope_default_agent_only(self):
        old = self.canon.canon_ts(self.t0 - timedelta(days=10))
        self._write_mem("mem_u", "user の記憶", scope="user",
                        strength=0.5, last_recalled_at=old)
        self._assembly(["mem_u"])
        st = self.canon.loads(self.w._conn.execute(
            "SELECT state_json FROM records WHERE record_id='mem_u'"
        ).fetchone()["state_json"])
        # scope=user は SD-10 既定（agent）対象外 → boost 寄与なし
        derived = self.cog.strength_at(st)
        pure_decay = self.rules.decay_value(
            0.5, 10.0, 1, self.rules.params("decay"))
        self.assertAlmostEqual(derived, pure_decay, places=6)

    def test_purpose_not_turn_no_boost(self):
        old = self.canon.canon_ts(self.t0 - timedelta(days=10))
        self._write_mem("mem_a", "agent の記憶", scope="agent",
                        strength=0.5, last_recalled_at=old)
        self._assembly(["mem_a"], purpose="verify")
        st = self.canon.loads(self.w._conn.execute(
            "SELECT state_json FROM records WHERE record_id='mem_a'"
        ).fetchone()["state_json"])
        derived = self.cog.strength_at(st)
        pure_decay = self.rules.decay_value(
            0.5, 10.0, 1, self.rules.params("decay"))
        self.assertAlmostEqual(derived, pure_decay, places=6)


class TestStateProjection(CogTestBase):
    def _state_record(self):
        st = {"record_id": "state_inner", "record_family": "state",
              "scope": "agent", "status": "active", "record_version": 1,
              "key": "inner",
              "baseline": {"mood": "普通", "valence": 0, "energy": 60},
              "created_at": "2026-10-09T10:00:00.000000Z",
              "updated_at": "2026-10-09T10:00:00.000000Z"}
        self.w.apply_mutation(
            "record_write",
            {"record_family": "state", "record_id": "state_inner",
             "new_state": st, "reason": "test"}, actor="companion")

    def test_projection_baseline(self):
        self._state_record()
        v = self.cog.project_inner("agent")
        self.assertEqual(v["mood"], "普通")
        self.assertEqual(v["energy"], 60)
        self.assertEqual(v["valence"], 0)

    def test_projection_change_and_return(self):
        self._state_record()
        # 2時間前に「元気/valence+2/energy90」
        ts = self.canon.canon_ts(self.t0 - timedelta(hours=2))
        self.w.append_event(
            "state_change",
            {"key": "inner", "scope": "agent",
             "value": {"mood": "元気", "valence": 2, "energy": 90}},
            actor="companion", occurred_at=ts)
        v = self.cog.project_inner("agent", at_ts=self.sclock())
        self.assertEqual(v["mood"], "元気")
        # 2h/24h → k=0.083: energy 90→90-(30*0.083)≈87.5→88 未満
        self.assertLess(v["energy"], 90)
        self.assertGreater(v["energy"], 60)
        self.assertLess(v["valence"], 2)

    def test_projection_full_return(self):
        self._state_record()
        ts = self.canon.canon_ts(self.t0 - timedelta(hours=30))
        self.w.append_event(
            "state_change",
            {"key": "inner", "scope": "agent",
             "value": {"mood": "元気", "valence": 2, "energy": 90}},
            actor="companion", occurred_at=ts)
        v = self.cog.project_inner("agent")
        # 30h > 24h → ほぼニュートラル（現行 inner.py と同型）
        self.assertEqual(v["energy"], 60)
        self.assertEqual(v["valence"], 0)
        self.assertEqual(v["mood"], "元気")  # ラベルは残る


class TestReflection(CogTestBase):
    def test_trigger_on_accumulated_importance(self):
        self._write_mem("mem_a", "a", scope="agent", importance=8)
        self._write_mem("mem_b", "b", scope="agent", importance=8)
        self._assembly(["mem_a", "mem_b"])
        out = self.cog.reflection_check()
        # 8+8=16 >= 15 → fired
        self.assertTrue(out["fired"])
        self.assertEqual(out["accumulated"], 16)
        ev = self.w._conn.execute(
            "SELECT payload_json FROM events WHERE"
            " event_type='reflection_trigger'").fetchone()
        pl = self.canon.loads(ev["payload_json"])
        self.assertEqual(pl["kind"], "cumulative_importance")
        # 直後は再発火しない
        self.assertFalse(self.cog.reflection_check()["fired"])

    def test_below_threshold_no_fire(self):
        self._write_mem("mem_a", "a", scope="agent", importance=5)
        self._assembly(["mem_a"])
        self.assertFalse(self.cog.reflection_check()["fired"])


class TestSuspectLane(CogTestBase):
    def test_suspect_scan_pends_candidate(self):
        st = {"record_id": "mem_new", "record_family": "memory",
              "scope": "user", "status": "active", "record_version": 1,
              "memory_type": "fact", "content": "new",
              "content_hash": "h", "subject": {"kind": "t"},
              "created_at": "2026-10-09T10:00:00.000000Z",
              "updated_at": "2026-10-09T10:00:00.000000Z"}
        n = dict(st)
        n.update(record_id="mem_old", content="old")
        self._write_mem("mem_old", "old", subject={"kind": "t"})

        calls = []

        def suspector(new, neigh):
            calls.append(neigh["record_id"])
            return {"claim": "近傍と矛盾の疑い", "reason": "test"}

        out = self.cog.suspect_scan(st, suspector)
        self.assertEqual(calls, ["mem_old"])
        self.assertEqual(out["scanned"], 1)
        self.assertEqual(out["proposals"][0]["verdict"], "pending")
        cand = self.w._conn.execute(
            "SELECT status FROM candidates").fetchone()
        self.assertEqual(cand["status"], "pending")

    def test_scope_filter(self):
        st = {"record_id": "x", "record_family": "memory",
              "scope": "user", "status": "active", "memory_type": "fact",
              "subject": None}
        n = dict(st)
        n.update(record_id="y", scope="agent")
        self.assertFalse(self.rules.suspect_in_scope(st, n))


class TestPrediction(CogTestBase):
    def test_deterministic_hit_resolved(self):
        past = self.canon.canon_ts(self.t0 - timedelta(days=1))
        self._write_pred("pred_a", past, expected="yes", actual="yes")
        out = self.cog.prediction_sweep()
        self.assertEqual(out["resolved"], 1)
        st = self.canon.loads(self.w._conn.execute(
            "SELECT state_json FROM records WHERE record_id='pred_a'"
        ).fetchone()["state_json"])
        self.assertEqual(st["outcome"], "hit")
        self.assertEqual(st["param_policy_ref"], "prediction-v1")

    def test_miss_and_future_pending(self):
        past = self.canon.canon_ts(self.t0 - timedelta(days=1))
        future = self.canon.canon_ts(self.t0 + timedelta(days=1))
        self._write_pred("pred_b", past, expected="yes", actual="no")
        self._write_pred("pred_c", future)
        out = self.cog.prediction_sweep()
        self.assertEqual(out["resolved"], 1)
        self.assertEqual(out["pending"], 1)
        st = self.canon.loads(self.w._conn.execute(
            "SELECT state_json FROM records WHERE record_id='pred_b'"
        ).fetchone()["state_json"])
        self.assertEqual(st["outcome"], "miss")

    def test_verdict_scoring_stays_pending(self):
        past = self.canon.canon_ts(self.t0 - timedelta(days=1))
        self._write_pred("pred_d", past, scoring="verdict")
        out = self.cog.prediction_sweep()
        self.assertEqual(out["resolved"], 0)
        self.assertEqual(out["pending"], 1)


class TestSimulate(CogTestBase):
    def test_diff_report(self):
        old = self.canon.canon_ts(self.t0 - timedelta(days=100))
        self._write_mem("mem_a", "a", strength=0.5,
                        last_recalled_at=old)
        params_b = dict(self.rules.params("decay"))
        params_b["weekly_factor"] = 0.5  # 強い減衰
        rep = self.cog.simulate(params_b)
        self.assertEqual(rep["rule"], "decay")
        self.assertGreater(rep["changed"], 0)
        self.assertEqual(rep["from_params"]["weekly_factor"], 0.9)
        self.assertEqual(rep["to_params"]["weekly_factor"], 0.5)
        # 0.5→強い減衰で床割れ
        self.assertIn("mem_a", rep["would_demote"])


class TestAcceptHook(CogTestBase):
    def test_on_accept_hook_fires_on_memory(self):
        seen = []
        self.adm.on_accept = lambda st: seen.append(st["record_id"])
        prop = {
            "record_family": "memory",
            "new_state": {
                "record_id": "mem_h", "record_family": "memory",
                "scope": "user", "status": "active",
                "record_version": 1, "memory_type": "fact",
                "content": "hook 経路", "content_hash": "h",
                "created_at": "2026-10-09T10:00:00.000000Z",
                "updated_at": "2026-10-09T10:00:00.000000Z",
                "importance": 1},
            "producer": "test", "generated_by": "test",
            "generator": {"method": "t", "model": "m", "version": "0"},
            "basis_events": [{"event_id": "e1", "cite": "x"}],
        }
        self.w.append_event("message", {"body": "b"}, actor="master")
        # basis event 実在化（id を得るため append した event_id を使う）
        ev_id = self.w._conn.execute(
            "SELECT event_id FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()["event_id"]
        prop["basis_events"] = [{"event_id": ev_id, "cite": "x"}]
        res = self.adm.submit(prop, actor="master")
        self.assertEqual(res["verdict"], "accept")
        self.assertEqual(seen, ["mem_h"])


if __name__ == "__main__":
    unittest.main()
