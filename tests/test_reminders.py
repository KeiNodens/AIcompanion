"""reminders.py: リマインドの通知先を依頼チャンネルに固定し、未解決なら保存しない"""
import unittest

import _support


class RemindChannelTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.actor = self._iso.import_module("companion.tools.actor")
        self.rem = self._iso.import_module("companion.tools.reminders")
        # ContextVar はテスト間で漏れるので正規化
        self.actor.CURRENT_CHANNEL.set(None)

    def _saved_channel_ids(self):
        with self.db.get_db() as conn:
            return [
                r[0]
                for r in conn.execute("SELECT channel_id FROM reminders").fetchall()
            ]

    def test_request_channel_wins_over_saved_default(self):
        self.db.save_state(last_channel_id=777)
        self.actor.CURRENT_CHANNEL.set(4242)
        res = self.rem.remind_handler(
            {"when": "2030-01-01 10:00", "text": "牛乳を買う"}
        )
        self.assertIn("リマインド", res)
        self.assertEqual(self._saved_channel_ids(), [4242])

    def test_fallback_to_saved_notify_channel(self):
        self.db.save_state(last_channel_id=777)
        res = self.rem.remind_handler({"when": "2030-01-01 10:00", "text": "test"})
        self.assertIn("リマインド", res)
        self.assertEqual(self._saved_channel_ids(), [777])

    def test_env_notify_channel_used_when_no_context(self):
        self._iso.cfg.NOTIFY_CHANNEL_ID = 555
        res = self.rem.remind_handler({"when": "2030-01-01 10:00", "text": "test"})
        self.assertIn("リマインド", res)
        self.assertEqual(self._saved_channel_ids(), [555])

    def test_unresolved_channel_gives_guidance_without_saving(self):
        res = self.rem.remind_handler({"when": "2030-01-01 10:00", "text": "test"})
        self.assertIn("チャンネル", res)
        self.assertEqual(self._saved_channel_ids(), [])

    def test_bad_time_format_still_rejected(self):
        self.actor.CURRENT_CHANNEL.set(4242)
        res = self.rem.remind_handler({"when": "あした", "text": "test"})
        self.assertIn("解釈できません", res)
        self.assertEqual(self._saved_channel_ids(), [])

    def test_past_time_rejected(self):
        """過去の時刻は即時発火しないよう登録自体を拒否する"""
        self.actor.CURRENT_CHANNEL.set(4242)
        res = self.rem.remind_handler({"when": "2020-01-01 10:00", "text": "x"})
        self.assertIn("過去", res)
        self.assertEqual(self._saved_channel_ids(), [])


if __name__ == "__main__":
    unittest.main()
