"""jobs.py のスキル面 — §7.5 staged まとめ通知＋事前要約、§7.3 upstream 通知"""
import hashlib
import unittest
from unittest import mock

import _support


def _pkg(inbox, name: str, body: str = "手順", desc: str = "説明"):
    d = inbox / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n{body}\n",
        encoding="utf-8",
    )
    return d


class SkillJobsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mem = _support.memory_stub()
        self.fake = _support.FakeCompletions()
        self._iso = _support.IsolatedModules(
            stubs={
                "companion.engine": _support.module_stub(
                    "companion.engine"),
                "companion.memory": self.mem,
                "companion.llm": _support.llm_stub(self.fake),
                "companion.reflect": _support.reflect_stub(),
            }
        )
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.cfg = self._iso.cfg
        self.cfg.NOTIFY_CHANNEL_ID = 42
        self.db = self._iso.import_module("companion.db")
        self.jobs = self._iso.import_module("companion.jobs")
        self.se = self._iso.import_module("companion.skill_export")

    async def test_staged_batch_notification_with_summary(self):
        """複数の staged を1通にまとめ、要約・リスク旗・差分行数を添える"""
        _pkg(self.cfg.SKILLS_INBOX_DIR, "tea-time", desc="お茶の淹れ方")
        _pkg(self.cfg.SKILLS_INBOX_DIR, "wipe", body="rm -rf する手順")
        self.fake.queue.append(
            _support.make_resp("お茶を淹れる手順。危険なし"))
        self.fake.queue.append(
            _support.make_resp("削除コマンドを含む手順"))
        await self.jobs._scan_skills_inbox()
        outs = self.db.due_notifications()
        self.assertEqual(len(outs), 1)          # 1件ずつではなく1通
        body = outs[0][3]
        self.assertIn("2件", body)
        self.assertIn("tea-time", body)
        self.assertIn("wipe", body)
        self.assertIn("お茶を淹れる手順", body)    # 裏方要約が載る
        self.assertIn("削除操作", body)           # リスク旗（rm -rf）
        self.assertIn("all-staged", body)
        # 要約は SKILL_SUMMARY_MODEL(空) → LLM_MODEL、minimal effort で呼ばれる
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(self.fake.calls[0]["model"], "test-model")
        self.assertEqual(
            self.fake.calls[0]["reasoning_effort"], "minimal")
        # 自己メモは技能ごとに1件ずつ残る
        notes = [a[0][0] for a in self.mem.calls["add_self_note"]]
        self.assertEqual(len(notes), 2)

    async def test_summary_failure_still_notifies(self):
        """裏方要約が失敗しても通知は出る（要約なし + フラグだけ）"""
        _pkg(self.cfg.SKILLS_INBOX_DIR, "tea-time")
        self.fake.queue.append(RuntimeError("llm down"))
        await self.jobs._scan_skills_inbox()
        outs = self.db.due_notifications()
        self.assertEqual(len(outs), 1)
        self.assertIn("tea-time", outs[0][3])
        self.assertNotIn("要約", outs[0][3])

    async def test_restaged_notification_has_diff_lines(self):
        """更新版の通知には前版との差分行数が載る"""
        _pkg(self.cfg.SKILLS_INBOX_DIR, "ver", body="v1")
        await self.jobs._scan_skills_inbox()
        self.db.skill_set_status("ver", "active", decided_by="master")
        _pkg(self.cfg.SKILLS_INBOX_DIR, "ver", body="v1\nv2追記")
        self.fake.queue.append(_support.make_resp("要約"))
        await self.jobs._scan_skills_inbox()
        outs = self.db.due_notifications()
        # 2回目の通知は別バッチ（dedupe_key が違う）として届く
        self.assertEqual(len(outs), 2)
        body = outs[1][3]
        self.assertIn("更新版", body)
        self.assertIn("+1/-0行", body)

    async def test_upstream_flag_writes_self_note(self):
        """§7.3: upstream 消失は flag 記録＋通知＋自己メモ"""
        pkg = self.cfg.SKILLS_DIR / "cook"
        pkg.mkdir()
        md = pkg / "SKILL.md"
        md.write_text(
            "---\nname: cook\ndescription: 説明\n---\n\n# cook\n手順\n",
            encoding="utf-8",
        )
        h = hashlib.sha256(md.read_bytes()).hexdigest()
        self.db.skill_stage("cook", "説明", "", "imported", h, str(pkg), "cook")
        self.db.skill_set_status("cook", "active", decided_by="master")
        self.se.SKILL_UPSTREAM_GRACE_MIN = 0
        with mock.patch.object(self.se.body, "skills", return_value=[]):
            await self.jobs._sync_skills_export()
        row = self.db.skill_get("cook")
        self.assertTrue(row["upstream_gone_at"])
        self.assertEqual(row["status"], "active")  # 自動 disable しない
        outs = self.db.due_notifications()
        self.assertTrue(any("upstream_gone" in o[3] for o in outs))
        notes = [a[0][0] for a in self.mem.calls["add_self_note"]]
        self.assertTrue(any("消えた" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
