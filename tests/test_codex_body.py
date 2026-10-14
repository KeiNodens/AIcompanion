"""runtime/codex.py — codex app-server (JSON-RPC 2.0) client のテスト。

実プロセスは立てない。subprocess.Popen を os.pipe 駆動の偽プロセスに
差し替え、テスト側が JSON-RPC の server 側（codex app-server）を演じる。
reader スレッド・request/notification/承認要求の demux・run lifecycle
全てを実配線で通す。
"""
import asyncio
import json
import os
import threading
import unittest
from unittest import mock

import _support


class _FakeProc:
    """codex app-server に見立てた偽 Popen。srv_read/srv_write は
    テストが演じる server 側の口（client.stdin→srv_read、
    srv_write→client.stdout）"""

    def __init__(self):
        app_stdin_r, app_stdin_w = os.pipe()
        app_stdout_r, app_stdout_w = os.pipe()
        app_stderr_r, app_stderr_w = os.pipe()
        # client（runtime.codex）から見た口
        self.stdin = os.fdopen(app_stdin_w, "wb")
        self.stdout = os.fdopen(app_stdout_r, "rb")
        self.stderr = os.fdopen(app_stderr_r, "rb")
        # server（テスト）から見た口
        self.srv_read = os.fdopen(app_stdin_r, "rb")
        self.srv_write = os.fdopen(app_stdout_w, "wb")
        self._srv_stderr_w = os.fdopen(app_stderr_w, "wb")
        self._code = None

    def poll(self):
        return self._code

    def terminate(self):
        self._code = 0

    def kill(self):
        self._code = -9

    def wait(self, timeout=None):
        self._code = self._code if self._code is not None else 0
        return self._code

    def send(self, msg: dict) -> None:
        """server→client に1行 JSON を流す（notification/応答/要求）"""
        self.srv_write.write(
            (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        self.srv_write.flush()

    def die(self):
        """プロセス死亡を再現（stdout/stderr を閉じて reader を EOF に）"""
        self._code = -9
        try:
            self.srv_write.close()
        except OSError:
            pass
        try:
            self._srv_stderr_w.close()
        except OSError:
            pass


class _Base:
    """偽 app-server 駆動の隔離環境。turns/handler はテストごとに差し替える"""

    def _make_iso(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.codex = self._iso.import_module("companion.runtime.codex")
        self.proc = _FakeProc()
        self.received: list[dict] = []
        self.turns: list[dict] = []          # thread/turns/list が返す turn
        self._stop = threading.Event()
        self._popen = mock.patch("subprocess.Popen",
                                 return_value=self.proc)
        self._popen.start()
        self.addCleanup(self._popen.stop)
        # codex 実行ファイル解決も偽物に（実機に codex が無くても通る）
        self._exe = mock.patch.object(self.codex, "_exe",
                                      return_value="fake-codex")
        self._exe.start()
        self.addCleanup(self._exe.stop)
        self.addCleanup(self.codex._mark_dead)
        self._server = threading.Thread(
            target=self._server_loop, daemon=True)
        self._server.start()

    def _handle(self, msg: dict):
        """server 側の応答生成。戻り値 None = 応答しない（notification 等）"""
        if "id" not in msg or "method" not in msg:
            return None
        rid, method = msg["id"], msg.get("method")
        if method == "initialize":
            return {"id": rid, "result": {"userAgent": "codex-test/0.0"}}
        if method == "thread/start":
            return {"id": rid, "result": {"thread": {"id": "t1"}}}
        if method == "thread/resume":
            return {"id": rid, "result": {
                "thread": {"id": msg["params"]["threadId"]}}}
        if method == "turn/start":
            return {"id": rid, "result": {
                "turn": {"id": "u1", "status": "inProgress"}}}
        if method == "thread/turns/list":
            return {"id": rid, "result": {"items": list(self.turns)}}
        if method in ("turn/steer", "turn/interrupt"):
            return {"id": rid, "result": {"turn": {"id": "u1"}}}
        return {"id": rid, "error": {"code": -32601,
                                     "message": f"unsupported {method}"}}

    def _server_loop(self):
        try:
            for raw in self.proc.srv_read:
                if self._stop.is_set():
                    return
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self.received.append(msg)
                resp = self._handle(msg)
                if resp is not None:
                    self.proc.send(resp)
        except (OSError, ValueError):
            pass

    def _wait_msg(self, pred, timeout=5.0):
        """受信メッセージが pred を満たすまで待つ"""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hit = [m for m in self.received if pred(m)]
            if hit:
                return hit[-1]
            threading.Event().wait(0.02)
        raise AssertionError(
            f"期待したメッセージが来ない。受信済み: {self.received}")


class CodexSpawnTest(_Base, unittest.TestCase):
    def setUp(self):
        self._make_iso()

    def test_initialize_handshake(self):
        res = self.codex.health()
        self.assertEqual(res.get("userAgent"), "codex-test/0.0")
        init = [m for m in self.received if m.get("method") == "initialize"]
        self.assertTrue(init)
        self.assertEqual(
            init[0]["params"]["capabilities"], {"experimentalApi": True})
        # initialized notification が返っている
        self._wait_msg(lambda m: m.get("method") == "initialized")

    def test_disabled_raises_unreachable(self):
        self.codex.CODEX_BODY_ENABLED = False
        with self.assertRaises(self.codex.BodyUnreachableError):
            self.codex.health()
        self.codex.CODEX_BODY_ENABLED = True


class CodexRunTest(_Base, unittest.TestCase):
    def setUp(self):
        self._make_iso()

    def test_create_run_returns_composite_run_id(self):
        created = self.codex.create_run("作業して", instructions="ops")
        self.assertEqual(created["run_id"], "t1/u1")
        self.assertEqual(created["body_session"], "t1")
        # thread/start の中身: 作業 cwd・policy・reviewer が既定通り
        ts = self._wait_msg(lambda m: m.get("method") == "thread/start")
        self.assertEqual(ts["params"]["sandbox"], "workspace-write")
        self.assertEqual(ts["params"]["approvalPolicy"], "on-request")
        self.assertEqual(ts["params"]["approvalsReviewer"], "user")
        st = self._wait_msg(lambda m: m.get("method") == "turn/start")
        self.assertEqual(st["params"]["threadId"], "t1")
        self.assertEqual(
            st["params"]["input"], [{"type": "text", "text": "作業して"}])
        self.assertEqual(st["params"]["sandboxPolicy"]["type"],
                         "workspaceWrite")

    def test_resume_with_session_id(self):
        created = self.codex.create_run("続き", session_id="t-old")
        self.assertEqual(created["run_id"], "t-old/u1")
        resume = self._wait_msg(
            lambda m: m.get("method") == "thread/resume")
        self.assertEqual(resume["params"]["threadId"], "t-old")

    def test_run_status_maps_turn(self):
        self.turns.append({
            "id": "u1", "status": "completed",
            "items": [{"type": "agentMessage", "text": "やった"}],
        })
        st = self.codex.run_status("t1/u1")
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["output"], "やった")
        self.assertEqual(st["session_id"], "t1")

    def test_run_status_404_when_turn_missing(self):
        with self.assertRaises(self.codex.BodyAPIError) as cm:
            self.codex.run_status("t1/ghost")
        self.assertEqual(cm.exception.status_code, 404)

    def test_steer_and_stop(self):
        self.assertTrue(self.codex.run_steer("t1/u1", "追い指示") is not None)
        steer = self._wait_msg(lambda m: m.get("method") == "turn/steer")
        self.assertEqual(steer["params"]["expectedTurnId"], "u1")
        self.assertTrue(self.codex.run_stop("t1/u1") is not None)
        intr = self._wait_msg(
            lambda m: m.get("method") == "turn/interrupt")
        self.assertEqual(intr["params"]["turnId"], "u1")


class CodexEventTest(_Base, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._make_iso()

    async def test_finished_event_from_notification(self):
        self.codex.create_run("作業")
        self.turns.append({"id": "u1", "status": "inProgress"})

        threading.Timer(0.1, lambda: self.proc.send({
            "method": "turn/completed",
            "params": {"threadId": "t1", "turn": {
                "id": "u1", "status": "completed",
                "items": [{"type": "agentMessage", "text": "done!"}],
            }},
        })).start()

        got = []
        async for seq, ev in self.codex.run_events("t1/u1"):
            got.append(ev)
            if ev.get("kind") == "finished":
                break
        fin = next(e for e in got if e.get("kind") == "finished")
        self.assertEqual(fin["status"], "completed")
        self.assertEqual(fin["output"], "done!")

    async def test_snapshot_finish_when_already_terminal(self):
        """既終了 turn は stream なしでも finished が即返る"""
        self.turns.append({
            "id": "u1", "status": "failed",
            "error": {"message": "boom"},
        })
        got = []
        async for seq, ev in self.codex.run_events("t1/u1"):
            got.append(ev)
            if ev.get("kind") == "finished":
                break
        self.assertEqual(got[0]["status"], "failed")
        self.assertEqual(got[0]["error"], "boom")


class CodexApprovalTest(_Base, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._make_iso()

    async def _start_and_ask(self):
        self.codex.create_run("作業")
        self.turns.append({"id": "u1", "status": "inProgress"})
        self.proc.send({
            "id": 50, "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "t1", "turnId": "u1",
                "command": "rm -rf /tmp/x",
                "commandActions": [{"type": "run", "description": "掃除"}],
            },
        })
        await asyncio.sleep(0.15)

    async def test_approval_request_pends_and_decide_responds(self):
        await self._start_and_ask()
        pend = self.codex.pending_approval_for("t1/u1")
        self.assertIsNotNone(pend)
        self.assertEqual(pend["request_id"], "50")
        self.assertIn("rm -rf", pend["summary"])
        self.codex.run_approval("t1/u1", "once", request_id="50")
        resp = self._wait_msg(
            lambda m: m.get("id") == 50 and "result" in m)
        self.assertEqual(resp["result"], {"decision": "accept"})

    async def test_deny_sends_decline(self):
        await self._start_and_ask()
        self.codex.run_approval("t1/u1", "deny", request_id="50")
        resp = self._wait_msg(
            lambda m: m.get("id") == 50 and "result" in m)
        self.assertEqual(resp["result"], {"decision": "decline"})

    async def test_decide_unknown_request_409(self):
        await self._start_and_ask()
        with self.assertRaises(self.codex.BodyAPIError) as cm:
            self.codex.run_approval("t1/u1", "once", request_id="999")
        self.assertEqual(cm.exception.status_code, 409)

    async def test_unsupported_server_request_gets_error(self):
        self.codex.create_run("作業")
        self.proc.send({
            "id": 77, "method": "item/tool/call",
            "params": {"threadId": "t1", "tool": "x", "arguments": {}},
        })
        resp = self._wait_msg(lambda m: m.get("id") == 77)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)


class CodexDeathTest(_Base, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._make_iso()

    async def test_process_death_fails_pending_and_watchers(self):
        self.codex.create_run("作業")
        self.turns.append({"id": "u1", "status": "inProgress"})
        gen = self.codex.run_events("t1/u1")
        # 購読開始（resume+status snapshot）を進めてから殺す
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.15)
        self.proc.die()
        try:
            res = await asyncio.wait_for(task, timeout=3)
        except StopAsyncIteration:
            res = None
        except (self.codex.BodyUnreachableError,
                self.codex.BodyAPIError):
            # resume 応答前に死んだ場合も許容（輸送断として上がる）
            res = None
        # _DEAD で stream が畳む（未終了扱い→watcher 側が run_check で確定）
        self.assertTrue(res is None or isinstance(res, tuple))
        with self.assertRaises((self.codex.BodyUnreachableError,
                                self.codex.BodyAPIError)):
            self.codex.run_status("t1/u1")


if __name__ == "__main__":
    unittest.main()
