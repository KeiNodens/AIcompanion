"""bodyruns.py — 身体側 run の記録・監視・承認決裁・報告のテスト。

ネットワークは使わない。runtime.body（start_task/run_check/watch_run/
steer/decide_approval）は全て差し替える。
"""
import asyncio
import sqlite3
import unittest
from unittest import mock

import _support


class _Base:
    def _make_iso(self, stubs=None):
        self._iso = _support.IsolatedModules(stubs=stubs)
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.bodyruns = self._iso.import_module("companion.bodyruns")
        self.body = self._iso.import_module("companion.runtime.body")
        # 通知先を確保（env 未設定 → state.json の last_channel_id を使う）
        self.db.save_state(last_channel_id=999)

    def _approval_row(self, ref=None):
        with self.db.get_db() as conn:
            conn.row_factory = sqlite3.Row
            if ref:
                return conn.execute(
                    "SELECT * FROM approvals WHERE ref=?", (ref,)
                ).fetchone()
            return conn.execute(
                "SELECT * FROM approvals WHERE kind='body_run'"
                " ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def _approval_code(self, ref=None):
        with self.db.get_db() as conn:
            q = "SELECT code FROM approvals WHERE kind='body_run'"
            if ref:
                return conn.execute(
                    q + " AND ref=?", (ref,)
                ).fetchone()[0]
            return conn.execute(q + " LIMIT 1").fetchone()[0]

    def _notifications(self):
        with self.db.get_db() as conn:
            return conn.execute(
                "SELECT dedupe_key, channel_id, body, status"
                " FROM notify_outbox ORDER BY id"
            ).fetchall()


class BodyRunsStartTest(_Base, unittest.TestCase):
    """start(): 猶予内完了→inline、未完→run_id返答、承認待ち→登録+通知"""

    def setUp(self):
        _Base._make_iso(self)

    def test_completed_in_grace_returns_inline(self):
        with mock.patch.object(self.body, "start_task",
                               return_value={"run_id": "r1",
                                             "body_session": "bs"}), \
                mock.patch.object(self.body, "run_check",
                                  return_value={"status": "completed",
                                                "output": "片付けたよ"}):
            out = self.bodyruns.start("掃除して", "clean", "sess1")
        self.assertIn("片付けたよ", out)
        self.assertIn("完了", out)
        row = self.db.body_run_get("r1")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["report_status"], "inline")
        self.assertEqual(row["origin_session"], "sess1")
        self.assertEqual(row["label"], "clean")
        self.assertEqual(row["body_session"], "bs")

    def test_incomplete_returns_run_id_message(self):
        with mock.patch.object(self.body, "start_task",
                               return_value={"run_id": "r1",
                                             "body_session": ""}), \
                mock.patch.object(self.body, "run_check",
                                  return_value={"status": "running"}):
            out = self.bodyruns.start("長い仕事", None, "sess1")
        self.assertIn("r1", out)
        self.assertIn("始め", out)
        row = self.db.body_run_get("r1")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["report_status"], "pending")

    def test_approval_wait_registers_and_notifies(self):
        st = {
            "status": "waiting_for_approval",
            "approval": {"request_id": "q1", "summary": "ファイルを消す"},
        }
        with mock.patch.object(self.body, "start_task",
                               return_value={"run_id": "r1",
                                             "body_session": ""}), \
                mock.patch.object(self.body, "run_check", return_value=st):
            out = self.bodyruns.start("消して", "del", "sess1")
        self.assertIn("承認待ち", out)
        self.assertIn("r1", out)
        # 承認レコード: kind=body_run, requester=body, 4桁コード
        code = self._approval_code()
        self.assertRegex(code, r"^\d{4}$")
        row = self._approval_row()
        self.assertEqual(row["requester"], "body")
        self.assertEqual(row["ref"], "r1:q1")
        # コードはツール応答には出さず、通知本文にだけ載せる
        self.assertNotIn(code, out)
        notes = self._notifications()
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0][0].startswith("body_approval:"))
        self.assertIn(code, notes[0][2])
        self.assertIn("ファイルを消す", notes[0][2])
        # run は承認待ちとして記録
        self.assertEqual(
            self.db.body_run_get("r1")["status"], "waiting_for_approval"
        )

    def test_duplicate_label_steers_existing_run(self):
        self.db.body_run_insert("r0", "最初の仕事", label="clean",
                                origin_session="s")
        steer = mock.MagicMock(return_value=True)
        start_task = mock.MagicMock()
        with mock.patch.object(self.body, "steer", steer), \
                mock.patch.object(self.body, "start_task", start_task):
            out = self.bodyruns.start("続きやって", "clean", "sess1")
        self.assertIn("追加指示", out)
        self.assertIn("r0", out)
        steer.assert_called_once_with("r0", "続きやって", backend="hermes")
        start_task.assert_not_called()

    def test_duplicate_label_waiting_approval(self):
        """同ラベルの進行中 run が承認待ちなら steer も新規 run もしない"""
        self.db.body_run_insert("r0", "最初の仕事", label="clean",
                                origin_session="s")
        self.db.body_run_update("r0", status="waiting_for_approval")
        steer = mock.MagicMock()
        start_task = mock.MagicMock()
        with mock.patch.object(self.body, "steer", steer), \
                mock.patch.object(self.body, "start_task", start_task):
            out = self.bodyruns.start("続きやって", "clean", "sess1")
        self.assertIn("承認待ち", out)
        self.assertIn("r0", out)
        steer.assert_not_called()
        start_task.assert_not_called()

    def test_duplicate_label_steer_rejected_creates_new(self):
        self.db.body_run_insert("r0", "最初の仕事", label="clean",
                                origin_session="s")
        with mock.patch.object(self.body, "steer", return_value=False), \
                mock.patch.object(self.body, "start_task",
                                  return_value={"run_id": "r2",
                                                "body_session": ""}), \
                mock.patch.object(self.body, "run_check",
                                  return_value={"status": "running"}):
            out = self.bodyruns.start("続きやって", "clean", "sess1")
        self.assertIn("r2", out)
        self.assertIsNotNone(self.db.body_run_get("r2"))

    def test_empty_task_rejected(self):
        self.assertIn("task", self.bodyruns.start("  ", None, "s"))


class BodyRunsWatcherTest(_Base, unittest.IsolatedAsyncioTestCase):
    """watcher: イベント記録・承認登録・終了確定・404→lost"""

    def setUp(self):
        _Base._make_iso(self)

    async def _run_watcher(self, run_id="r1"):
        await self.bodyruns.ensure_watcher(run_id)
        t = self.bodyruns._watchers.get(run_id)
        self.assertIsNotNone(t)
        await asyncio.wait_for(asyncio.shield(t), timeout=5)

    def _tool_calls(self, run_id="r1"):
        with self.db.get_db() as conn:
            return conn.execute(
                "SELECT tool, status, seq, session_key, result_excerpt"
                " FROM turn_tool_calls WHERE origin='body_run' AND ref=?"
                " ORDER BY id",
                (run_id,),
            ).fetchall()

    async def test_tool_events_and_finish_recorded(self):
        self.db.body_run_insert("r1", "掃除して", origin_session="s1")
        events = [
            {"seq": 1, "kind": "tool_started", "tool": "browser",
             "preview": "open"},
            {"seq": 2, "kind": "tool_completed", "tool": "browser",
             "duration": 1.5, "error": False},
            {"seq": 3, "kind": "finished", "status": "completed",
             "output": "done", "error": ""},
        ]

        async def fake_watch(run_id, last_seq=-1, backend="hermes"):
            for e in events:
                yield e

        with mock.patch.object(self.body, "watch_run", fake_watch):
            await self._run_watcher()
        rows = self._tool_calls()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "browser")
        self.assertEqual(rows[0][1], "ok")
        self.assertEqual(rows[0][3], "s1")
        self.assertIn("1.5", rows[0][4] or "")
        run = self.db.body_run_get("r1")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["last_seq"], 3)
        self.assertEqual(run["output"], "done")
        # 終了後は報告待ち（inline ではない）
        self.assertEqual(run["report_status"], "pending")

    async def test_approval_event_registers_and_notifies(self):
        self.db.body_run_insert("r1", "消して", label="del",
                                origin_session="s1")
        events = [
            {"seq": 1, "kind": "approval_request", "request_id": "q1",
             "summary": "rm -rf /tmp"},
        ]

        async def fake_watch(run_id, last_seq=-1, backend="hermes"):
            for e in events:
                yield e
            # 終了イベント無しで stream が切れる

        with mock.patch.object(self.body, "watch_run", fake_watch), \
                mock.patch.object(
                    self.body, "run_check",
                    return_value={"status": "interrupted", "error": "x"}):
            await self._run_watcher()
        ap = self._approval_row()
        self.assertIsNotNone(ap)
        self.assertEqual(ap["kind"], "body_run")
        code = self._approval_code()
        notes = self._notifications()
        self.assertIn(code, notes[0][2])
        self.assertIn("rm -rf /tmp", notes[0][2])
        # 同じ request_id の再登録は冪等（行は1件）
        self.bodyruns.register_approval("r1", "q1", "rm -rf /tmp")
        with self.db.get_db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM approvals WHERE kind='body_run'"
            ).fetchone()[0]
        self.assertEqual(n, 1)

    async def test_404_marks_lost(self):
        self.db.body_run_insert("r1", "消えた", origin_session="s")
        err404 = self.body.hermes.BodyAPIError("not found", status_code=404)

        async def boom(run_id, last_seq=-1, backend="hermes"):
            raise err404
            yield  # pragma: no cover - async gen にするための置き石

        with mock.patch.object(self.body, "watch_run", boom), \
                mock.patch.object(self.body, "run_check", side_effect=err404):
            await self._run_watcher()
        run = self.db.body_run_get("r1")
        self.assertEqual(run["status"], "lost")
        self.assertIn("見つからない", run["error"] or "")

    async def test_stream_end_recheck_continues_then_terminal(self):
        """stream が切れたら run_check で確定する（終了ならそこで終わり）"""
        self.db.body_run_insert("r1", "x", origin_session="s")

        async def empty_watch(run_id, last_seq=-1, backend="hermes"):
            return
            yield

        with mock.patch.object(self.body, "watch_run", empty_watch), \
                mock.patch.object(self.body, "run_check",
                                  return_value={"status": "failed",
                                                "error": "boom"}):
            await self._run_watcher()
        run = self.db.body_run_get("r1")
        self.assertEqual(run["status"], "failed")

    async def test_unreachable_retries_without_losing_row(self):
        """BodyUnreachableError は lost にしない（再接続で終了を拾う）"""
        self.db.body_run_insert("r1", "x", origin_session="s")
        calls = {"n": 0}

        async def flap(run_id, last_seq=-1, backend="hermes"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self.body.hermes.BodyUnreachableError("flap")
            yield {"seq": 1, "kind": "finished", "status": "completed",
                   "output": "ok", "error": ""}

        async def no_sleep(_):
            return

        with mock.patch.object(self.body, "watch_run", flap), \
                mock.patch.object(self.body, "run_check",
                                  return_value={"status": "running"}), \
                mock.patch("asyncio.sleep", new=lambda d: no_sleep(d)):
            await self._run_watcher()
        self.assertEqual(self.db.body_run_get("r1")["status"], "completed")


class BodyRunsReportTest(_Base, unittest.IsolatedAsyncioTestCase):
    """終了 run の報告: engine.generate を通して通知に載せる"""

    def setUp(self):
        self.gen = mock.AsyncMock(return_value={"reply": "できたみたい！"})
        _Base._make_iso(
            self,
            stubs={
                "companion.engine": _support.module_stub(
                    "companion.engine", generate=self.gen
                )
            },
        )

    async def test_finished_run_reports_once(self):
        self.db.body_run_insert(
            "r1", "掃除して", label="clean", origin_session="sess-o",
        )
        self.db.body_run_finish("r1", "completed", output="done")
        await self.bodyruns.tick()
        self.gen.assert_awaited_once()
        args, kw = self.gen.call_args
        self.assertEqual(args[0], "sess-o")       # origin_session で生成
        self.assertIn("[身体からの報告]", args[1])
        self.assertIn("done", args[1])
        self.assertEqual(kw["source"], "body")
        notes = self._notifications()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][0], "body_run:r1")
        self.assertEqual(notes[0][2], "できたみたい！")
        self.assertEqual(self.db.body_run_get("r1")["report_status"], "done")
        # 再 tick しても重複しない
        await self.bodyruns.tick()
        self.assertEqual(self.gen.await_count, 1)
        self.assertEqual(len(self._notifications()), 1)

    async def test_report_generate_failure_uses_plain_notification(self):
        self.db.body_run_insert("r1", "掃除して", label="clean",
                                origin_session="s")
        self.db.body_run_finish("r1", "failed", error="boom")
        self.gen.side_effect = RuntimeError("llm dead")
        await self.bodyruns.tick()
        self.assertEqual(self.db.body_run_get("r1")["report_status"], "failed")
        notes = self._notifications()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][0], "body_run:r1")
        self.assertIn("生成に失敗", notes[0][2])

    async def test_no_channel_stays_pending(self):
        self.db.save_state(last_channel_id=0)
        self.db.body_run_insert("r1", "x", origin_session="s")
        self.db.body_run_finish("r1", "completed", output="done")
        await self.bodyruns.tick()
        self.gen.assert_not_called()
        self.assertEqual(self.db.body_run_get("r1")["report_status"],
                         "pending")


class BodyRunsDecideTest(_Base, unittest.TestCase):
    """decide(): コード/ID による決裁と Hermes への転送"""

    def setUp(self):
        _Base._make_iso(self)
        self.db.body_run_insert("r1", "消して", label="del",
                                origin_session="s")

    def _register(self, run_id="r1", request_id="q1", summary="消す操作"):
        self.bodyruns.register_approval(run_id, request_id, summary)
        return self._approval_row(f"{run_id}:{request_id}")

    def test_code_approval_calls_hermes_with_request_id(self):
        ap = self._register()
        dec = mock.MagicMock(return_value={"ok": True, "stale": False})
        with mock.patch.object(self.body, "decide_approval", dec):
            ok, msg = self.bodyruns.decide(
                code=ap["code"], approve=True, decided_by="api-chat")
        self.assertTrue(ok)
        self.assertIn("送った", msg)
        dec.assert_called_once_with("r1", True, request_id="q1",
                                    backend="hermes")
        row = self._approval_row(ap["ref"])
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["decided_by"], "api-chat")
        # 決裁文言にコードは載せない
        self.assertNotIn(ap["code"], msg)

    def test_double_decision_rejected(self):
        ap = self._register()
        with mock.patch.object(
                self.body, "decide_approval",
                return_value={"ok": True, "stale": False}):
            self.bodyruns.decide(code=ap["code"], approve=True,
                                 decided_by="api-chat")
            ok2, msg2 = self.bodyruns.decide(code=ap["code"], approve=True,
                                             decided_by="api-chat")
        self.assertFalse(ok2)

    def test_expired_rejected_and_marked(self):
        ap = self._register()
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE approvals SET expires_at='2000-01-01 00:00:00'"
                " WHERE id=?",
                (ap["id"],),
            )
        dec = mock.MagicMock()
        with mock.patch.object(self.body, "decide_approval", dec):
            ok, msg = self.bodyruns.decide(code=ap["code"], approve=True,
                                           decided_by="api-chat")
        self.assertFalse(ok)
        self.assertIn("期限切れ", msg)
        dec.assert_not_called()
        self.assertEqual(self._approval_row(ap["ref"])["status"], "expired")

    def test_hermes_409_marks_stale(self):
        ap = self._register()
        with mock.patch.object(
                self.body, "decide_approval",
                return_value={"ok": False, "stale": True}):
            ok, msg = self.bodyruns.decide(code=ap["code"], approve=True,
                                           decided_by="api-chat")
        self.assertFalse(ok)
        self.assertEqual(self._approval_row(ap["ref"])["status"], "stale")

    def test_unreachable_keeps_decision_recorded(self):
        """到達不能でも pending には戻さない（誤承認を防ぐ安全側）"""
        ap = self._register()
        err = self.body.hermes.BodyUnreachableError("down")
        with mock.patch.object(self.body, "decide_approval",
                               side_effect=err):
            ok, msg = self.bodyruns.decide(code=ap["code"], approve=True,
                                           decided_by="api-chat")
        self.assertFalse(ok)
        self.assertIn("届かなかった", msg)
        self.assertEqual(self._approval_row(ap["ref"])["status"], "approved")

    def test_decide_by_approval_id_deny(self):
        ap = self._register()
        dec = mock.MagicMock(return_value={"ok": True, "stale": False})
        with mock.patch.object(self.body, "decide_approval", dec):
            ok, msg = self.bodyruns.decide(
                approval_id=ap["id"], approve=False, decided_by="desktop")
        self.assertTrue(ok)
        dec.assert_called_once_with("r1", False, request_id="q1",
                                    backend="hermes")
        self.assertEqual(self._approval_row(ap["ref"])["status"], "denied")

    def test_unknown_code_rejected(self):
        self._register()
        ok, msg = self.bodyruns.decide(code="7777", approve=True,
                                       decided_by="api-chat")
        # 7777 が偶然当たることもあり得るので、その場合は別コードで確認
        if ok:
            ok, msg = self.bodyruns.decide(code="7778", approve=True,
                                           decided_by="api-chat")
        self.assertFalse(ok)


class BodyRunsLookupTest(_Base, unittest.TestCase):
    """find_pending_by_code / pending_approvals — code を外に出さない"""

    def setUp(self):
        _Base._make_iso(self)
        self.db.body_run_insert("r1", "消して", label="del",
                                origin_session="s")

    def test_find_by_code_returns_summary_not_code(self):
        self.bodyruns.register_approval("r1", "q1", "ファイルを消す")
        code = self._approval_code("r1:q1")
        found = self.bodyruns.find_pending_by_code(code)
        self.assertIsNotNone(found)
        self.assertIn("ファイルを消す", found["request"])
        self.assertNotIn("code", found)
        # 別コードは見つからない
        other = "0000" if code != "0000" else "0001"
        self.assertIsNone(self.bodyruns.find_pending_by_code(other))

    def test_pending_approvals_shape(self):
        self.bodyruns.register_approval("r1", "q1", "掃除する")
        rows = self.bodyruns.pending_approvals()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "body_run")
        self.assertIn("掃除する", rows[0]["request"])
        self.assertIn("expires_at", rows[0])
        self.assertNotIn("code", rows[0])

    def test_expired_swept(self):
        self.bodyruns.register_approval("r1", "q1", "x")
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE approvals SET expires_at='2000-01-01 00:00:00'"
                " WHERE kind='body_run'"
            )
        self.assertEqual(self.bodyruns.pending_approvals(), [])
        self.assertEqual(
            self._approval_row("r1:q1")["status"], "expired"
        )


class BodyRunsNoRequestIdTest(_Base, unittest.TestCase):
    """request_id 無しの承認要求: ref="run_id:" が潰れても別要求を拾う"""

    def setUp(self):
        _Base._make_iso(self)
        self.db.body_run_insert("r1", "消して", label="del",
                                origin_session="s")

    def _pending_refs(self):
        with self.db.get_db() as conn:
            return conn.execute(
                "SELECT id, code FROM approvals"
                " WHERE kind='body_run' AND ref='r1:'"
                " AND status='pending' ORDER BY id"
            ).fetchall()

    def test_missing_request_id_registers_separate_rows(self):
        a1 = self.bodyruns.register_approval("r1", None, "削除A")
        a2 = self.bodyruns.register_approval("r1", None, "削除B")
        # 同じ ref="r1:" でも内容が違えば別レコード（先着1件で潰れない）
        self.assertNotEqual(a1["id"], a2["id"])
        rows = self._pending_refs()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][1], rows[1][1])   # 別コード
        notes = [n for n in self._notifications()
                 if n[0].startswith("body_approval:")]
        self.assertEqual(len(notes), 2)

    def test_missing_request_id_same_pending_summary_dedups(self):
        """同じ内容の重複イベント（SSE再接続等）は1件に畳む"""
        a1 = self.bodyruns.register_approval("r1", None, "削除A")
        a2 = self.bodyruns.register_approval("r1", None, "削除A")
        self.assertEqual(a1["id"], a2["id"])
        self.assertEqual(len(self._pending_refs()), 1)

    def test_missing_request_id_decide_forwards_none(self):
        self.bodyruns.register_approval("r1", None, "削除A")
        code = self._approval_code("r1:")
        dec = mock.MagicMock(return_value={"ok": True, "stale": False})
        with mock.patch.object(self.body, "decide_approval", dec):
            ok, _msg = self.bodyruns.decide(
                code=code, approve=True, decided_by="12345")
        self.assertTrue(ok)
        # ref="r1:" の request_id 部は空 → Hermes には None で渡す
        dec.assert_called_once_with("r1", True, request_id=None,
                                    backend="hermes")
        # 決裁済みの後の同名要求は別行として登録される
        b = self.bodyruns.register_approval("r1", None, "削除A")
        self.assertIsNotNone(b)
        self.assertEqual(len(self._pending_refs()), 1)


if __name__ == "__main__":
    unittest.main()
