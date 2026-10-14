"""test_consolidate —— C-5 近重複記憶の統合 producer

カバー:
- 近重複ペア検出（trigram jaccard 閾値）
- survivor 選定（importance/強度/新しさ、pinned 保護）
- merge proposal → admission → records への status=merged 反映
- basis 無し記憶のスキップ・cycle_id 冪等・感度違いの除外
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class ConsolidateBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        im = cls._iso.import_module
        cls.canon = im("companion.kernel.canon")
        cls.writer = im("companion.kernel.writer")
        cls.admit = im("companion.kernel.admit")
        cls.admission = im("companion.adapters.admission")
        cls.consolidate = im("companion.adapters.consolidate")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def _mk(self, name="con"):
        tmp = Path(tempfile.mkdtemp(prefix=f"con_{name}_"))
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        tick = {"i": 0}

        def clock():
            tick["i"] += 1
            return t0 + timedelta(seconds=tick["i"])

        def sclock():
            return self.canon.canon_ts(t0 + timedelta(seconds=tick["i"]))

        w = self.writer.open_core(tmp / "core.db", clock=clock)
        return tmp, w, sclock

    def _msg(self, w, text):
        r = w.append_event("message", {"text": text}, actor="master")
        return r["event_id"]

    def _mem(self, rid, *, scope="user", content="テスト記憶",
             importance=3, strength=0.5, pinned=False, basis=None,
             sensitivity="normal", valid_from=None):
        now = "2026-10-01T00:00:00Z"
        st = {
            "record_family": "memory", "record_id": rid, "scope": scope,
            "status": "active", "record_version": 1,
            "memory_type": "fact", "content": content,
            "content_hash": self.canon.content_hash(content),
            "sensitivity": sensitivity, "importance": importance,
            "retrieval_strength": strength, "tier": "hot",
            "pinned": pinned, "created_at": now, "updated_at": now,
        }
        if valid_from:
            st["valid_from"] = valid_from
        if basis:
            st["provenance"] = {
                "source_events": [{"event_id": e, "cite": "text"}
                                  for e in basis]}
        return st

    def _write(self, w, st):
        w.apply_mutation("memory_write", {
            "memory_id": st["record_id"], "new_state": st,
            "reason": "seed", "basis_events": []}, actor="system")

    def _state(self, w, rid):
        with w.transaction() as conn:
            return self.canon.loads(conn.execute(
                "SELECT state_json FROM records WHERE record_id=?",
                (rid,)).fetchone()["state_json"])


class TestConsolidation(ConsolidateBase):
    def test_near_duplicate_merges_loser(self):
        _, w, sclock = self._mk("merge")
        e1 = self._msg(w, "マスターはコーヒーが好き")
        e2 = self._msg(w, "マスターはコーヒーが好きだ")
        adm = self.admission.Admission(w, clock=sclock)
        cn = self.consolidate.Consolidation(w, adm, clock=sclock)
        # 低い方を loser にするため m1=強い・m2=弱い
        self._write(w, self._mem("m1", content="マスターはコーヒーが好き",
                                 importance=4, strength=0.9, basis=[e1]))
        self._write(w, self._mem("m2", content="マスターはコーヒーが好きだ",
                                 importance=2, strength=0.3, basis=[e2]))
        res = cn.run(scope="user", day="2026-10-10")
        self.assertEqual(res["pairs"], 1)
        self.assertEqual(res["proposed"], 1)
        st2 = self._state(w, "m2")
        self.assertEqual(st2["status"], "merged")
        self.assertEqual(st2["merged_into"], "m1")
        self.assertEqual(st2["record_version"], 2)
        self.assertEqual(self._state(w, "m1")["status"], "active")

    def test_pinned_never_loses(self):
        _, w, sclock = self._mk("pin")
        e1 = self._msg(w, "猫の名前はミケちゃん")
        e2 = self._msg(w, "猫の名前はミケちゃんだ")
        adm = self.admission.Admission(w, clock=sclock)
        cn = self.consolidate.Consolidation(w, adm, clock=sclock)
        # pinned で弱い m1 と、強いが非 pinned の m2 → m2 が loser
        self._write(w, self._mem("m1", content="猫の名前はミケちゃん",
                                 pinned=True, strength=0.1, basis=[e1]))
        self._write(w, self._mem("m2", content="猫の名前はミケちゃんだ",
                                 importance=3, strength=0.9, basis=[e2]))
        res = cn.run(scope="user", day="2026-10-10")
        self.assertEqual(res["proposed"], 1)
        self.assertEqual(self._state(w, "m1")["status"], "active")
        self.assertEqual(self._state(w, "m2")["status"], "merged")

    def test_no_basis_pair_skipped(self):
        _, w, sclock = self._mk("nobasis")
        adm = self.admission.Admission(w, clock=sclock)
        cn = self.consolidate.Consolidation(w, adm, clock=sclock)
        self._write(w, self._mem(
            "m1", content="同じ内容の記憶が残っている"))
        self._write(w, self._mem(
            "m2", content="同じ内容の記憶が残っているよ"))
        res = cn.run(scope="user", day="2026-10-10")
        self.assertEqual(res["proposed"], 0)
        self.assertEqual(res["skipped_no_basis"], 1)
        self.assertEqual(self._state(w, "m2")["status"], "active")

    def test_sensitivity_mismatch_not_paired(self):
        _, w, sclock = self._mk("sens")
        e1 = self._msg(w, "秘密の内容について")
        e2 = self._msg(w, "秘密の内容についてだ")
        adm = self.admission.Admission(w, clock=sclock)
        cn = self.consolidate.Consolidation(w, adm, clock=sclock)
        self._write(w, self._mem("m1", content="秘密の内容について詳しく",
                                 sensitivity="sensitive", basis=[e1]))
        self._write(w, self._mem("m2", content="秘密の内容について詳しく知る",
                                 sensitivity="normal", basis=[e2]))
        res = cn.run(scope="user", day="2026-10-10")
        self.assertEqual(res["pairs"], 0)
        self.assertEqual(res["proposed"], 0)

    def test_distinct_memories_untouched(self):
        _, w, sclock = self._mk("distinct")
        e1 = self._msg(w, "犬を飼っている")
        e2 = self._msg(w, "仕事はエンジニア")
        adm = self.admission.Admission(w, clock=sclock)
        cn = self.consolidate.Consolidation(w, adm, clock=sclock)
        self._write(w, self._mem("m1", content="犬を飼っている", basis=[e1]))
        self._write(w, self._mem("m2", content="仕事はエンジニア", basis=[e2]))
        res = cn.run(scope="user", day="2026-10-10")
        self.assertEqual(res["pairs"], 0)
        self.assertEqual(res["proposed"], 0)

    def test_cycle_idempotent(self):
        _, w, sclock = self._mk("idem")
        e1 = self._msg(w, "重複する内容がある")
        e2 = self._msg(w, "重複する内容があるよ")
        adm = self.admission.Admission(w, clock=sclock)
        cn = self.consolidate.Consolidation(w, adm, clock=sclock)
        self._write(w, self._mem("m1", content="重複する内容がある",
                                 basis=[e1]))
        self._write(w, self._mem("m2", content="重複する内容があるよ",
                                 basis=[e2]))
        r1 = cn.run(scope="user", day="2026-10-10")
        self.assertEqual(r1["proposed"], 1)
        r2 = cn.run(scope="user", day="2026-10-10")
        self.assertTrue(r2["idempotent"])
        self.assertEqual(r2["proposed"], 0)

    def test_merged_excluded_from_next_cycle(self):
        """merged になった記憶は次 cycle の走査対象外（連鎖 merge しない）。"""
        _, w, sclock = self._mk("chain")
        e1 = self._msg(w, "水曜日はゴミの日")
        e2 = self._msg(w, "水曜日はゴミの日だ")
        e3 = self._msg(w, "水曜日はゴミの日です")
        adm = self.admission.Admission(w, clock=sclock)
        cn = self.consolidate.Consolidation(w, adm, clock=sclock)
        self._write(w, self._mem("m1", content="水曜日はゴミの日",
                                 importance=4, basis=[e1]))
        self._write(w, self._mem("m2", content="水曜日はゴミの日だ",
                                 importance=2, basis=[e2]))
        self._write(w, self._mem("m3", content="水曜日はゴミの日です",
                                 importance=2, basis=[e3]))
        cn.run(scope="user", day="2026-10-10")
        # 2日目: merged は走査対象に載らないので残りのペアのみ処理
        res = cn.run(scope="user", day="2026-10-11")
        self.assertLessEqual(res["scanned"], 2)
        for rid in ("m2", "m3"):
            st = self._state(w, rid)
            if st["status"] == "merged":
                self.assertEqual(st["merged_into"], "m1")

    def test_stale_cas_drift_goes_stale_not_applied(self):
        """走査と submit の間に対象が変わっていたら stale で確定しない。"""
        _, w, sclock = self._mk("stale")
        e1 = self._msg(w, "本を読むのが好き")
        e2 = self._msg(w, "本を読むのが好きだ")
        adm = self.admission.Admission(w, clock=sclock)
        cn = self.consolidate.Consolidation(w, adm, clock=sclock)
        self._write(w, self._mem("m1", content="本を読むのが好き",
                                 basis=[e1]))
        st2 = self._mem("m2", content="本を読むのが好きだ", basis=[e2])
        self._write(w, st2)
        # fingerprint を古い版のまま手で流す（走査→submit の drift 模倣）
        prop_hit = []

        class StaleAdm:
            def submit(self, prop, *, actor=None):
                prop = dict(prop)
                prop["expected_fingerprint"] = "deadbeef"
                prop_hit.append(1)
                return adm.submit(prop, actor=actor)
        cn2 = self.consolidate.Consolidation(w, StaleAdm(), clock=sclock)
        res = cn2.run(scope="user", day="2026-10-10")
        self.assertTrue(prop_hit)
        self.assertEqual(res["proposals"][0]["verdict"], "stale")
        self.assertEqual(self._state(w, "m2")["status"], "active")

    def test_receipt_events_written(self):
        """merge 提案の write_proposal/candidate_review 刻印が残る。"""
        _, w, sclock = self._mk("receipt")
        e1 = self._msg(w, "週末は実家に帰る")
        e2 = self._msg(w, "週末は実家に帰るよ")
        adm = self.admission.Admission(w, clock=sclock)
        cn = self.consolidate.Consolidation(w, adm, clock=sclock)
        self._write(w, self._mem("m1", content="週末は実家に帰る",
                                 basis=[e1]))
        self._write(w, self._mem("m2", content="週末は実家に帰るよ",
                                 basis=[e2]))
        cn.run(scope="user", day="2026-10-10")
        with w.transaction() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM events"
                " WHERE event_type='memory_update'").fetchone()["c"]
            self.assertEqual(n, 1)
            p = self.canon.loads(conn.execute(
                "SELECT payload_json FROM events"
                " WHERE event_type='memory_update'").fetchone()[
                "payload_json"])
            self.assertEqual(p["op"], "merge")
            self.assertEqual(p["memory_id"], "m2")


if __name__ == "__main__":
    unittest.main()
