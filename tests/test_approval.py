"""ステップ4: 委譲ジョブの本人承認とLLM判断の分離テスト。

- prepare は承認レコード（内容ハッシュ・期限・4桁コード）を作り、
  コードを通知に載せる
- delegate_confirm（LLM経路）は起動しない。案内だけ返す
- 「承認 NNNN」発言/承認ボタン経路 delegate_approve が承認を消費して
  起動する。却下経路 delegate_deny は実行せず取り消す
- 二重承認・期限切れ・内容改変を防ぐ
"""
import unittest
from unittest import mock

import _support


class ApprovalFlowTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.delegate = self._iso.import_module("companion.tools.delegate")
        self.actor = self._iso.import_module("companion.tools.actor")
        self.actor.CURRENT_ACTOR.set("12345")  # fake cfg OWNER_USER_ID
        # 通知先を確保（承認コードを載せる通知が積まれる経路にする）
        self.db.save_state(last_channel_id=999)

    def _prepare(self, request="テスト依頼"):
        res = self.delegate.delegate_prepare(request, workdir=str(self._iso.tmp))
        self.assertIn("依頼 #", res)
        jid = int(res.split("依頼 #")[1].split(" ")[0])
        return jid, res

    def _job(self, jid):
        with self.db.get_db() as conn:
            return conn.execute(
                "SELECT * FROM delegate_jobs WHERE id=?", (jid,)
            ).fetchone()

    def _approval(self, jid):
        with self.db.get_db() as conn:
            return conn.execute(
                "SELECT * FROM approvals WHERE job_id=?", (jid,)
            ).fetchone()

    def _code(self, jid):
        return self._approval(jid)[11]   # code 列

    def _notifications(self):
        with self.db.get_db() as conn:
            return conn.execute(
                "SELECT dedupe_key, channel_id, body, status"
                " FROM notify_outbox ORDER BY id"
            ).fetchall()

    def _patched_launch(self):
        """codex Popen をスタブ化。proc は起動成功したように見せる"""
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.poll.return_value = None
        popen = mock.MagicMock(return_value=proc)
        return (
            mock.patch.object(self.delegate, "_codex_exe", return_value="codex"),
            mock.patch.object(self.delegate.subprocess, "Popen", popen),
            proc,
        )

    def test_prepare_creates_pending_approval(self):
        jid, res = self._prepare()
        # 死に経路の !approve は案内しない（「承認 NNNN」/ボタン経路）
        self.assertNotIn("!approve", res)
        self.assertIn("承認 NNNN", res)
        ap = self._approval(jid)
        self.assertIsNotNone(ap)
        # status=pending, requester=12345
        self.assertEqual(ap[4], "pending")
        self.assertEqual(ap[3], "12345")
        self.assertEqual(self._job(jid)[6], "pending")

    def test_prepare_notifies_with_code_but_hides_it_in_reply(self):
        """4桁コードは通知文にだけ載り、ツール応答には出ない"""
        jid, res = self._prepare()
        code = self._code(jid)
        self.assertRegex(code, r"^\d{4}$")
        self.assertNotIn(code, res)
        notes = self._notifications()
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0][0].startswith("delegate_approval:"))
        self.assertIn(code, notes[0][2])
        self.assertIn("承認", notes[0][2])

    def test_find_pending_by_code(self):
        jid, _ = self._prepare()
        found = self.delegate.find_pending_by_code(self._code(jid))
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], jid)
        self.assertIn("テスト依頼", found["request"])
        self.assertNotIn("code", found)
        # 別コードは見つからない
        other = "0000" if self._code(jid) != "0000" else "0001"
        self.assertIsNone(self.delegate.find_pending_by_code(other))

    def test_deny_cancels_without_launch(self):
        jid, _ = self._prepare()
        cm, popen, _ = self._patched_launch()
        with cm, popen as p:
            res = self.delegate.delegate_deny(jid, "12345")
        self.assertIn("却下", res)
        p.assert_not_called()
        self.assertEqual(self._job(jid)[6], "cancelled")
        ap = self._approval(jid)
        self.assertEqual(ap[4], "denied")
        self.assertEqual(ap[7], "12345")   # decided_by

    def test_confirm_never_launches(self):
        jid, _ = self._prepare()
        cm, popen, _ = self._patched_launch()
        with cm, popen as p:
            res = self.delegate.delegate_confirm(jid)
        self.assertNotIn("!approve", res)
        self.assertIn("承認", res)
        p.assert_not_called()
        self.assertEqual(self._job(jid)[6], "pending")

    def test_approve_consumes_and_launches(self):
        jid, _ = self._prepare()
        cm, popen, _ = self._patched_launch()
        with cm, popen as p:
            res = self.delegate.delegate_approve(jid, "12345")
        self.assertIn("codexに出しました", res)
        p.assert_called_once()
        self.assertEqual(self._job(jid)[6], "running")
        ap = self._approval(jid)
        self.assertEqual(ap[4], "consumed")
        self.assertEqual(ap[7], "12345")
        # 再承認は拒否（jobはrunningなので起動もされない）
        res2 = self.delegate.delegate_approve(jid, "12345")
        self.assertIn("running", res2)

    def test_approve_rejects_non_pending_job(self):
        jid, _ = self._prepare()
        self.delegate._set_status(jid, "cancelled")
        res = self.delegate.delegate_approve(jid, "12345")
        self.assertIn("cancelled", res)

    def test_approve_expired(self):
        jid, _ = self._prepare()
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE approvals SET expires_at='2000-01-01 00:00:00'"
                " WHERE job_id=?", (jid,)
            )
        res = self.delegate.delegate_approve(jid, "12345")
        self.assertIn("期限", res)
        self.assertEqual(self._job(jid)[6], "pending")  # 起動しない
        self.assertEqual(self._approval(jid)[4], "expired")

    def test_approve_rejects_tampered_content(self):
        """承認後に依頼内容が変わっていたら実行しない"""
        jid, _ = self._prepare()
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE delegate_jobs SET prompt='改ざんされた依頼' WHERE id=?",
                (jid,),
            )
        res = self.delegate.delegate_approve(jid, "12345")
        self.assertIn("変わっています", res)
        self.assertEqual(self._job(jid)[6], "pending")

    def test_approve_creates_missing_approval_for_legacy_job(self):
        """移行前に存在した pending ジョブには承認レコードを補完する"""
        with self.db.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO delegate_jobs (prompt, request, workdir, status)"
                " VALUES ('p','古い依頼','/tmp','pending')"
            )
            jid = cur.lastrowid
        cm, popen, _ = self._patched_launch()
        with cm, popen as p:
            res = self.delegate.delegate_approve(jid, "12345")
        self.assertIn("codexに出しました", res)
        p.assert_called_once()

    def test_pending_approvals_listing(self):
        jid, _ = self._prepare("重要な修正依頼")
        rows = self.delegate.pending_approvals()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], jid)
        self.assertIn("重要な修正", rows[0]["request"])
        # 期限切れにしたら一覧から消える
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE approvals SET expires_at='2000-01-01 00:00:00'"
                " WHERE job_id=?", (jid,)
            )
        self.assertEqual(self.delegate.pending_approvals(), [])

    def test_approve_while_another_running(self):
        jid1, _ = self._prepare("依頼1")
        jid2, _ = self._prepare("依頼2")
        cm, popen, _ = self._patched_launch()
        with cm, popen:
            self.delegate.delegate_approve(jid1, "12345")
        res = self.delegate.delegate_approve(jid2, "12345")
        self.assertIn("実行中", res)
        self.assertEqual(self._job(jid2)[6], "pending")


if __name__ == "__main__":
    unittest.main()
