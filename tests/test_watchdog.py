"""watchdog.py: 連続失敗→障害通知・復旧通知（1障害1通知）"""
import asyncio
import unittest

from _support import IsolatedModules


def _setup(iso):
    iso.cfg.NOTIFY_CHANNEL_ID = 777
    db = iso.import_module("companion.db")
    wd = iso.import_module("companion.watchdog")
    wd._state.clear()
    wd._last_run = 0.0
    return db, wd


def _outbox(db):
    with db.get_db() as conn:
        return conn.execute(
            "SELECT dedupe_key, body FROM notify_outbox ORDER BY id"
        ).fetchall()


class WatchdogTest(unittest.TestCase):
    def test_down_then_up_notifies_once_each(self):
        with IsolatedModules() as iso:
            db, wd = _setup(iso)
            fails = iso.cfg.WATCHDOG_FAILS
            # fails-1 回までは通知なし
            for _ in range(fails - 1):
                wd._update("llm", "LLM", ok=False)
            self.assertEqual(_outbox(db), [])
            # 閾値到達で down 通知
            wd._update("llm", "LLM", ok=False)
            rows = _outbox(db)
            self.assertEqual(len(rows), 1)
            self.assertIn("watchdog:llm:down:", rows[0][0])
            self.assertIn("不調", rows[0][1])
            # down 中に追加失敗しても通知は増えない
            wd._update("llm", "LLM", ok=False)
            self.assertEqual(len(_outbox(db)), 1)
            # 復旧で up 通知
            wd._update("llm", "LLM", ok=True)
            rows = _outbox(db)
            self.assertEqual(len(rows), 2)
            self.assertIn("watchdog:llm:up:", rows[1][0])
            self.assertIn("復旧", rows[1][1])

    def test_recovery_resets_streak(self):
        with IsolatedModules() as iso:
            db, wd = _setup(iso)
            wd._update("llm", "LLM", ok=False)
            wd._update("llm", "LLM", ok=True)   # down 前の成功はカウンタリセット
            for _ in range(iso.cfg.WATCHDOG_FAILS - 1):
                wd._update("llm", "LLM", ok=False)
            self.assertEqual(_outbox(db), [])

    def test_no_notify_channel_logs_but_keeps_state(self):
        with IsolatedModules() as iso:
            db, wd = _setup(iso)
            iso.cfg.NOTIFY_CHANNEL_ID = 0  # 宛先未解決
            for _ in range(iso.cfg.WATCHDOG_FAILS):
                wd._update("llm", "LLM", ok=False)
            self.assertEqual(_outbox(db), [])
            self.assertTrue(wd._state["llm"]["down"])  # 二重通知を防ぐためdownのまま
            # 復旧も通知は出ないが状態は戻る
            wd._update("llm", "LLM", ok=True)
            self.assertFalse(wd._state["llm"]["down"])

    def test_tick_probes_and_throttles(self):
        with IsolatedModules() as iso:
            db, wd = _setup(iso)
            calls = {"llm": 0, "hermes": 0}
            wd._TARGETS = (
                ("llm", "LLM", lambda: calls.__setitem__("llm", calls["llm"] + 1) or False),
                ("hermes", "Hermes API", lambda: calls.__setitem__("hermes", calls["hermes"] + 1) or True),
            )
            asyncio.run(wd.tick())
            self.assertEqual(calls, {"llm": 1, "hermes": 1})
            asyncio.run(wd.tick())  # 間隔内は probe しない
            self.assertEqual(calls, {"llm": 1, "hermes": 1})
            wd._last_run -= iso.cfg.WATCHDOG_INTERVAL + 1
            asyncio.run(wd.tick())
            self.assertEqual(calls, {"llm": 2, "hermes": 2})


if __name__ == "__main__":
    unittest.main()
