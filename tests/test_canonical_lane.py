"""P8-B canonical 記録 lane のテスト

カバー: submit_turn→envelope→drain→events（turn_id/session 刻印）・dedup・
disabled・queue full/open 失敗での degraded 印＋通知・tick drain・
engine 配線（generate→記録）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class LiveLaneTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        cls.cfg = cls._iso.cfg
        cls.live = cls._iso.import_module("companion.adapters.live")
        cls.db = cls._iso.import_module("companion.db")
        cls.wmod = cls._iso.import_module("companion.kernel.writer")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def setUp(self):
        self.cfg.CANONICAL_RECORD = True
        self.tmp = Path(tempfile.mkdtemp(prefix="lane_test_"))
        self.path = self.tmp / "core.db"
        self.lane = self.live.RecordLane(self.path)

    def tearDown(self):
        self.lane.shutdown(2.0)
        self.cfg.CANONICAL_RECORD = False

    def _rows(self, sql, args=()):
        conn = self.wmod.connect_ro(self.path)
        rows = conn.execute(sql, args).fetchall()
        conn.close()
        return rows

    def _submit(self, turn_id=1):
        return self.lane.submit_turn(
            turn_id, "sess-1", "おはよう", "おはようございます！",
            ts_iso="2026-10-15T08:00:00+00:00", source="hermes")

    def test_submit_turn_records_message_and_response(self):
        self.assertTrue(self._submit())
        self.assertTrue(self.lane.wait_idle(10))

        evs = self._rows(
            "SELECT event_type, actor, turn_id, session_channel, session_id,"
            " ingress_key FROM events ORDER BY seq")
        self.assertEqual([e["event_type"] for e in evs],
                         ["message", "response"])
        self.assertEqual([e["actor"] for e in evs], ["master", "companion"])
        self.assertEqual([e["turn_id"] for e in evs], ["1", "1"])
        self.assertEqual(evs[0]["session_channel"], "hermes")
        self.assertEqual(evs[0]["session_id"], "sess-1")
        self.assertTrue(all(e["ingress_key"] for e in evs))

        # envelope は生保存済み・normalized、queue は空
        env = self._rows("SELECT status, stream_id FROM ingress_envelopes")
        self.assertEqual(env[0]["status"], "normalized")
        self.assertEqual(env[0]["stream_id"], "sess-1")
        self.assertFalse(self._rows(
            "SELECT 1 FROM normalize_queue WHERE state='pending'"))
        # 抽出 claim が発行されている（後続ステージの仕事の権威）
        self.assertTrue(self._rows("SELECT 1 FROM claims"))

    def test_dedup_same_turn(self):
        self.assertTrue(self._submit())
        self.assertTrue(self.lane.wait_idle(10))
        self.assertTrue(self._submit())       # 同 turn_id 再送
        self.assertTrue(self.lane.wait_idle(10))
        self.assertEqual(
            self._rows("SELECT COUNT(*) c FROM events")[0]["c"], 2)
        self.assertEqual(
            self._rows("SELECT COUNT(*) c FROM ingress_envelopes")[0]["c"], 1)

    def test_disabled_noop(self):
        self.cfg.CANONICAL_RECORD = False
        self.assertFalse(self._submit())
        self.assertFalse(self.path.exists())
        self.assertFalse(self.lane.status()["worker_alive"])

    def test_turn_id_none_noop(self):
        self.assertFalse(self.lane.submit_turn(
            None, "s", "u", "a", ts_iso="t", source="x"))

    def test_queue_full_degrades(self):
        for _ in range(self.live._QUEUE_MAX):
            self.lane._q.put_nowait(("x", None))
        self.db.update_state(last_channel_id=42)
        self.assertFalse(self._submit())
        st = self.db.load_state().get("canonical_lane")
        self.assertTrue(st["degraded"])
        # 通知が outbox に載る（dedupe_key は1日1回）
        n = self.db.get_db().execute(
            "SELECT dedupe_key, body FROM notify_outbox").fetchall()
        self.assertTrue(any(k[0].startswith("canonical_lane:") for k in n))
        # 後始末: キューを空にして worker が生えないことを確認
        while not self.lane._q.empty():
            self.lane._q.get_nowait()
            self.lane._q.task_done()

    def test_open_failure_degrades_and_notifies(self):
        # core.db の位置に壊れたファイル → open_core が sanity/DDL で落ちる
        self.path.write_bytes(b"not a sqlite database")
        self.db.update_state(last_channel_id=42)
        self.assertTrue(self._submit())
        self.lane.shutdown(3.0)
        st = self.db.load_state().get("canonical_lane")
        self.assertTrue(st["degraded"])
        self.assertIn("open failed", st["error"])
        n = self.db.get_db().execute(
            "SELECT dedupe_key FROM notify_outbox").fetchall()
        self.assertTrue(any(k[0].startswith("canonical_lane:") for k in n))

    def test_tick_keeps_worker_alive(self):
        self.lane.tick()
        self.assertTrue(self.lane.status()["worker_alive"])

    def test_recover_clears_degraded_flag(self):
        self.db.update_state(mutator=lambda s: s.__setitem__(
            "canonical_lane", {"degraded": True, "error": "old"}))
        self.assertTrue(self._submit())
        self.assertTrue(self.lane.wait_idle(10))
        st = self.db.load_state().get("canonical_lane")
        self.assertFalse(st["degraded"])


class EngineLaneWiringTest(unittest.IsolatedAsyncioTestCase):
    """engine.generate → lane.submit_turn の配線確認（turns 記録後に複写）。"""

    def setUp(self):
        self.mem = _support.memory_stub()
        self.llm_mod = _support.llm_stub()
        self.refl = _support.reflect_stub()
        self.tpkg = _support.tools_pkg_stub()
        self._iso = _support.IsolatedModules(
            stubs={
                "companion.memory": self.mem,
                "companion.llm": self.llm_mod,
                "companion.reflect": self.refl,
                "companion.tools": self.tpkg,
            })
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.engine = self._iso.import_module("companion.engine")
        self.wmod = self._iso.import_module("companion.kernel.writer")
        self._iso.cfg.CANONICAL_RECORD = True

    async def test_generate_records_turn_to_core_db(self):
        self.llm_mod.fake.queue.append(_support.make_resp("こんにちは！"))
        res = await self.engine.generate("s1", "やあ")
        self.assertEqual(res["reply"], "こんにちは！")

        lane = self.engine.canonical_lane.lane
        self.assertTrue(lane.wait_idle(10))
        lane.shutdown(2.0)

        conn = self.wmod.connect_ro(self._iso.cfg.DATA_DIR / "core.db")
        evs = conn.execute(
            "SELECT event_type, session_id FROM events ORDER BY seq"
        ).fetchall()
        conn.close()
        self.assertEqual([e["event_type"] for e in evs],
                         ["message", "response"])
        self.assertEqual(evs[0]["session_id"], "s1")


if __name__ == "__main__":
    unittest.main()
