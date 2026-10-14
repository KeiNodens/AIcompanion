"""test_tiering —— P8-8 L-TIER のテスト

カバー:
- kernel.tier: 閾値写像・hysteresis・dwell・cycle_id・supersede_carry
- tier_policies/tier_eval/tier_cycles（schema v5）
- TierCycle.run: cycle_id 冪等・遷移 proposal・archive 強制 pending
- supply: tier_filtered（turn で cold 不可見）・deep_recall（全 tier＋blurred）
- derived: mem_search.tier・vec0 membership（hot/warm のみ）・cue_report
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class TierBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        im = cls._iso.import_module
        cls.canon = im("companion.kernel.canon")
        cls.writer = im("companion.kernel.writer")
        cls.vocab = im("companion.kernel.vocab")
        cls.tier = im("companion.kernel.tier")
        cls.tiering = im("companion.adapters.tiering")
        cls.admission = im("companion.adapters.admission")
        cls.supply = im("companion.adapters.supply")
        cls.derived = im("companion.adapters.derived")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def _mk(self, name="tier"):
        tmp = Path(tempfile.mkdtemp(prefix=f"tier_{name}_"))
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        tick = {"i": 0}

        def clock():
            tick["i"] += 1
            return t0 + timedelta(seconds=tick["i"])

        def sclock():
            return self.canon.canon_ts(t0 + timedelta(seconds=tick["i"]))

        w = self.writer.open_core(tmp / "core.db", clock=clock)
        return tmp, w, sclock

    def _mem(self, rid, *, scope="user", strength=0.5, tier="hot",
             content="テスト記憶"):
        now = "2026-10-01T00:00:00Z"
        return {
            "record_family": "memory", "record_id": rid, "scope": scope,
            "status": "active", "record_version": 1,
            "memory_type": "fact", "content": content,
            "content_hash": self.canon.content_hash(content),
            "sensitivity": "normal", "importance": 3,
            "retrieval_strength": strength, "tier": tier,
            "pinned": False, "created_at": now, "updated_at": now,
        }

    def _write(self, w, st):
        w.apply_mutation("memory_write", {
            "memory_id": st["record_id"], "new_state": st,
            "reason": "seed", "basis_events": []}, actor="system")


# ------------------------------------------------------------ kernel.tier
class TestKernelTier(TierBase):
    def test_tiers_vocabulary(self):
        self.assertEqual(self.tier.TIERS,
                         ("hot", "warm", "cold", "archive"))
        for t in self.tier.TIERS:
            self.assertIn(t, self.vocab.TIERS)
        self.assertIn("tier_cycle", self.vocab.PRODUCERS)
        self.assertTrue(self.tier.valid_cause("decay_streak"))
        self.assertFalse(self.tier.valid_cause("nonsense"))

    def test_desired_tier_thresholds(self):
        pol = self.tier.DEFAULT_TIER_POLICIES["user"]
        self.assertEqual(self.tier.desired_tier(0.9, pol), "hot")
        self.assertEqual(self.tier.desired_tier(0.30, pol), "warm")
        self.assertEqual(self.tier.desired_tier(0.10, pol), "cold")
        self.assertEqual(self.tier.desired_tier(0.01, pol), "archive")
        self.assertEqual(self.tier.desired_tier(0.40, pol), "hot")

    def test_cycle_id_deterministic(self):
        self.assertEqual(self.tier.cycle_id_for("2026-10-10"),
                         "tier-2026-10-10")
        self.assertEqual(self.tier.cycle_id_for("2026-10-10T23:00:00"),
                         "tier-2026-10-10")

    def test_hysteresis_requires_streak(self):
        pol = dict(self.tier.DEFAULT_TIER_POLICIES["user"])
        pol["hysteresis"] = 3
        st = {"tier": "hot", "retrieval_strength": 0.05}
        # streak=1,2 では遷移しない
        e1 = self.tier.evaluate(st, pol, cycle_no=100, eval_row=None)
        self.assertFalse(e1["transition"])
        self.assertEqual(e1["streak"], 1)
        e2 = self.tier.evaluate(st, pol, cycle_no=101,
                                eval_row={"desired_tier": "cold",
                                          "streak": 2})
        self.assertTrue(e2["transition"])
        self.assertEqual(e2["desired"], "cold")

    def test_dwell_blocks_transition(self):
        pol = dict(self.tier.DEFAULT_TIER_POLICIES["user"])
        pol["hysteresis"] = 1
        pol["dwell_min_cycles"] = 2
        st = {"tier": "hot", "retrieval_strength": 0.05,
              "tier_cycle_no": 100}
        ev = self.tier.evaluate(st, pol, cycle_no=101,
                                eval_row={"desired_tier": "cold",
                                          "streak": 1})
        self.assertFalse(ev["transition"])
        self.assertFalse(ev["dwell_ok"])
        ev2 = self.tier.evaluate(st, pol, cycle_no=102,
                                 eval_row={"desired_tier": "cold",
                                           "streak": 2})
        self.assertTrue(ev2["transition"])

    def test_supersede_carry(self):
        prev = {"record_id": "m1", "tier": "cold",
                "retrieval_strength": 0.1,
                "last_recalled_at": "2026-01-01"}
        new = {"record_id": "m2", "tier": "hot",
               "retrieval_strength": 0.5}
        out = self.tier.supersede_carry(prev, new)
        self.assertEqual(out["tier"], "cold")
        self.assertEqual(out["retrieval_strength"], 0.1)
        self.assertEqual(out["carried"]["supersede_carry"]["from"], "m1")


# ------------------------------------------------------------ schema v5
class TestSchemaV5(TierBase):
    def test_tier_tables_exist(self):
        _, w, _ = self._mk("schema")
        with w.transaction() as conn:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("tier_policies", names)
            self.assertIn("tier_eval", names)
            self.assertIn("tier_cycles", names)
            ver = conn.execute(
                "SELECT value FROM core_meta WHERE key='schema_version'"
            ).fetchone()["value"]
            self.assertEqual(ver, "5")


# ------------------------------------------------------------ TierCycle
class TestTierCycle(TierBase):
    def test_install_defaults(self):
        _, w, sclock = self._mk("inst")
        adm = self.admission.Admission(w, clock=sclock)
        tc = self.tiering.TierCycle(w, adm, clock=sclock)
        dig = tc.install_defaults()
        self.assertIn("user", dig)
        self.assertIn("agent", dig)
        with w.transaction() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) c FROM tier_policies").fetchone()["c"], 2)

    def test_cycle_idempotent(self):
        _, w, sclock = self._mk("idem")
        adm = self.admission.Admission(w, clock=sclock)
        tc = self.tiering.TierCycle(w, adm, clock=sclock)
        self._write(w, self._mem("m1", strength=0.05))
        r1 = tc.run(scope="user", day="2026-10-10")
        self.assertFalse(r1.get("idempotent"))
        self.assertEqual(r1["evaluated"], 1)
        r2 = tc.run(scope="user", day="2026-10-10")
        self.assertTrue(r2["idempotent"])

    def test_transition_after_hysteresis(self):
        _, w, sclock = self._mk("trans")
        adm = self.admission.Admission(w, clock=sclock)
        tc = self.tiering.TierCycle(w, adm, clock=sclock)
        self._write(w, self._mem("m1", strength=0.10))   # → cold desired
        # hysteresis=3 なので3cycle目で遷移
        for i, day in enumerate(("2026-10-10", "2026-10-11",
                                 "2026-10-12")):
            res = tc.run(scope="user", day=day)
            if i < 2:
                self.assertEqual(res["transitions"], 0)
        self.assertEqual(res["transitions"], 1)
        with w.transaction() as conn:
            st = self.canon.loads(conn.execute(
                "SELECT state_json FROM records WHERE record_id='m1'"
            ).fetchone()["state_json"])
            self.assertEqual(st["tier"], "cold")
            self.assertEqual(st["tier_change"]["cause"], "decay_streak")

    def test_archive_goes_pending_not_applied(self):
        _, w, sclock = self._mk("arch")
        adm = self.admission.Admission(w, clock=sclock)
        tc = self.tiering.TierCycle(w, adm, clock=sclock)
        self._write(w, self._mem("m1", strength=0.01))   # → archive desired
        for day in ("2026-10-10", "2026-10-11", "2026-10-12"):
            res = tc.run(scope="user", day=day)
        self.assertEqual(res["archive_pending"], 1)
        self.assertEqual(res["transitions"], 0)
        with w.transaction() as conn:
            st = self.canon.loads(conn.execute(
                "SELECT state_json FROM records WHERE record_id='m1'"
            ).fetchone()["state_json"])
            # auto_apply しない —— record は hot のまま
            self.assertEqual(st["tier"], "hot")
            cand = conn.execute(
                "SELECT COUNT(*) c FROM candidates"
                " WHERE status='pending'").fetchone()["c"]
            self.assertGreaterEqual(cand, 1)

    def test_disabled_scope_skips(self):
        _, w, sclock = self._mk("dis")
        adm = self.admission.Admission(w, clock=sclock)
        tc = self.tiering.TierCycle(w, adm, clock=sclock)
        with w.transaction() as conn:
            conn.execute(
                "INSERT INTO tier_policies(scope, policy_json,"
                " policy_digest, updated_at) VALUES ('user', ?, 'd', 't')",
                (self.canon.dumps({"enabled": False}),))
        res = tc.run(scope="user", day="2026-10-10")
        self.assertTrue(res["disabled"])


# ------------------------------------------------------------ supply tier gate
class TestSupplyTier(TierBase):
    def test_turn_hides_cold_deep_recall_shows_blurred(self):
        _, w, sclock = self._mk("sup")
        sup_mod = self.supply
        self._write(w, self._mem(
            "m_hot", strength=0.9, tier="hot", content="牛乳が好き"))
        self._write(w, self._mem(
            "m_cold", strength=0.05, tier="cold",
            content="冷蔵庫の奥の記憶"))
        sp = sup_mod.Supply(w, None, clock=sclock)
        # canonical fallback 経路で tier gate が効くか確認（derived 無し）
        res = sp.supply_context(scopes=["user"], query="記憶",
                              purpose="turn", consumer="test")
        ids = {b["id"] for b in res["blocks"]}
        self.assertNotIn("m_cold", ids)
        self.assertGreaterEqual(
            res["omitted"].get("tier_filtered", 0), 1)

        res2 = sp.supply_context(scopes=["user"], query="冷蔵庫",
                                 purpose="deep_recall", consumer="test")
        ids2 = {b["id"]: b for b in res2["blocks"]}
        self.assertIn("m_cold", ids2)
        self.assertTrue(ids2["m_cold"]["blurred"])

    def test_hot_default_regression(self):
        """tier 未刻印の record は hot 扱い（全 hot 既定の回帰維持）。"""
        _, w, sclock = self._mk("reg")
        st = self._mem("m1", content="普通の記憶")
        del st["tier"]
        self._write(w, st)
        sp = self.supply.Supply(w, None, clock=sclock)
        res = sp.supply_context(scopes=["user"], query="記憶",
                                purpose="turn", consumer="test")
        ids = {b["id"] for b in res["blocks"]}
        self.assertIn("m1", ids)
        self.assertEqual(res["omitted"].get("tier_filtered", 0), 0)


# ------------------------------------------------------------ derived tier
class TestDerivedTier(TierBase):
    def test_mem_search_tier_and_vec_membership(self):
        tmp, w, sclock = self._mk("der")
        self._write(w, self._mem("m_hot", strength=0.9, tier="hot",
                                 content="温かい記憶"))
        self._write(w, self._mem("m_cold", strength=0.05, tier="cold",
                                 content="冷たい記憶"))
        d = self.derived.DerivedStore(w, dir=tmp, clock=sclock)
        res = d.rebuild()
        self.assertTrue(res["active"], res["verify"])
        acon, _ = d.open_active()
        try:
            tiers = {r["record_id"]: r["tier"] for r in acon.execute(
                "SELECT record_id, tier FROM mem_search")}
            self.assertEqual(tiers["m_cold"], "cold")
            self.assertEqual(tiers["m_hot"], "hot")
            # cue 索引には両方載る
            self.assertEqual(len(tiers), 2)
            # vec0 membership は hot/warm のみ
            try:
                n_vec = acon.execute(
                    "SELECT COUNT(*) FROM mem_vec").fetchone()[0]
                self.assertEqual(n_vec, 1)
            except Exception:
                # vec0 不在環境は mem_vec_blob を見る
                n_vec = acon.execute(
                    "SELECT COUNT(*) FROM mem_vec_blob").fetchone()[0]
                self.assertEqual(n_vec, 0)   # embedder 無しなら0
        finally:
            acon.close()

    def test_cue_report_finds_cold(self):
        tmp, w, sclock = self._mk("cue")
        self._write(w, self._mem("m_cold", strength=0.05, tier="cold",
                                 content="去年の夏に海で見た花火"))
        d = self.derived.DerivedStore(w, dir=tmp, clock=sclock)
        d.rebuild()
        adm = self.admission.Admission(w, clock=sclock)
        tc = self.tiering.TierCycle(w, adm, clock=sclock)
        rep = tc.cue_report(d)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["cold_archive_total"], 1)
        self.assertGreaterEqual(rep["hit_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
