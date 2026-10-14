"""runtime/hermes.py・runtime/body.py・tools/body.py — Runtime Interface のテスト。

外部HTTPは全て偽Clientに差し替え、:8642 には一切触れない。
"""
import asyncio
import json
import unittest
from unittest import mock

import httpx

import _support


def _resp(payload, status_code=200):
    r = mock.MagicMock()
    r.status_code = status_code
    r.json.return_value = payload
    r.text = json.dumps(payload)
    return r


class FakeClient:
    """httpx.Client 偽物。routes: {(method, path): resp|exc|list[resp|exc]}
    リストなら呼ぶたびに先頭から取り出す（ポーリングの状態遷移用）"""

    def __init__(self, routes):
        self.routes = routes
        self.init_kwargs = None
        self.requests = []

    def __call__(self, **kw):
        self.init_kwargs = kw
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _serve(self, method, path, kw):
        self.requests.append({"method": method, "path": path, "kw": kw})
        item = self.routes[(method, path)]
        if isinstance(item, list):
            item = item.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request(self, method, path, **kw):
        return self._serve(method, path, kw)

    def get(self, path, **kw):
        return self._serve("GET", path, kw)


class _FakeStreamResponse:
    """httpx.AsyncClient.stream() が返す疑似応答（aiter_lines で SSE 行を流す）"""

    def __init__(self, lines, status_code=200):
        self.status_code = status_code
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeAsyncClient:
    """httpx.AsyncClient 偽物。lines: SSE の行リスト、exc: stream 呼出し時の例外"""

    def __init__(self, lines=(), status_code=200, exc=None):
        self.lines = list(lines)
        self.status_code = status_code
        self.exc = exc
        self.init_kwargs = None
        self.stream_calls = []

    def __call__(self, **kw):
        self.init_kwargs = kw
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, path, **kw):
        self.stream_calls.append({"method": method, "path": path, "kw": kw})
        if self.exc is not None:
            raise self.exc
        return _FakeStreamResponse(self.lines, self.status_code)


class HermesClientTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.hermes = self._iso.import_module("companion.runtime.hermes")

    def _client(self, routes):
        fake = FakeClient(routes)
        patcher = mock.patch.object(self.hermes.httpx, "Client", fake)
        self.addCleanup(patcher.stop)
        patcher.start()
        return fake

    def test_req_sends_bearer_and_base(self):
        fake = self._client({("GET", "/v1/capabilities"): _resp({"model": "x"})})
        self.hermes.capabilities()
        self.assertEqual(fake.init_kwargs["base_url"], "http://127.0.0.1:1")
        self.assertEqual(
            fake.init_kwargs["headers"]["Authorization"], "Bearer test-key"
        )

    def test_missing_key_is_unreachable(self):
        # 鍵の解決は hermes_conf に一元化されているのでそこを潰す
        with mock.patch.object(self.hermes.hermes_conf, "api_server_key",
                               return_value=("", "")):
            with self.assertRaises(self.hermes.BodyUnreachableError):
                self.hermes.capabilities()

    def test_http_error_is_api_error_with_status(self):
        self._client({("GET", "/v1/capabilities"): _resp({}, 500)})
        with self.assertRaises(self.hermes.BodyAPIError) as cm:
            self.hermes.capabilities()
        self.assertEqual(cm.exception.status_code, 500)

    def test_connection_error_is_unreachable(self):
        self._client({
            ("GET", "/v1/capabilities"): httpx.ConnectError("refused")
        })
        with self.assertRaises(self.hermes.BodyUnreachableError):
            self.hermes.capabilities()

    def test_create_run_payload(self):
        fake = self._client({("POST", "/v1/runs"): _resp({"run_id": "r1"})})
        self.hermes.create_run(
            "掃除して", session_id="companion-ops", instructions="手伝って"
        )
        body = fake.requests[0]["kw"]["json"]
        self.assertEqual(body["input"], "掃除して")
        self.assertEqual(body["model"], "body")
        self.assertEqual(body["session_id"], "companion-ops")
        self.assertEqual(body["instructions"], "手伝って")

    def test_run_approval_payload(self):
        fake = self._client({("POST", "/v1/runs/r1/approval"): _resp({"ok": True})})
        self.hermes.run_approval("r1", "once", request_id="q7")
        self.assertEqual(
            fake.requests[0]["kw"]["json"],
            {"choice": "once", "request_id": "q7"},
        )
        # request_id 省略時は含めない
        self.hermes.run_approval("r1", "deny")
        self.assertEqual(fake.requests[1]["kw"]["json"], {"choice": "deny"})

    def test_run_approval_409_distinguishable(self):
        self._client({("POST", "/v1/runs/r1/approval"): _resp({}, 409)})
        with self.assertRaises(self.hermes.BodyAPIError) as cm:
            self.hermes.run_approval("r1", "once")
        self.assertEqual(cm.exception.status_code, 409)

    def test_run_steer_stop_payloads(self):
        fake = self._client({
            ("POST", "/v1/runs/r1/steer"): _resp({"accepted": True}),
            ("POST", "/v1/runs/r1/stop"): _resp({"ok": True}),
        })
        self.hermes.run_steer("r1", "別のことをして")
        self.hermes.run_stop("r1")
        self.assertEqual(fake.requests[0]["kw"]["json"], {"input": "別のことをして"})
        self.assertEqual(fake.requests[1]["path"], "/v1/runs/r1/stop")

    def test_jobs_endpoints(self):
        fake = self._client({
            ("GET", "/api/jobs"): _resp({"jobs": [{"id": "j1"}]}),
            ("POST", "/api/jobs"): _resp({"job": {"id": "j2"}}),
            ("DELETE", "/api/jobs/j1"): _resp({"ok": True}),
            ("POST", "/api/jobs/j1/pause"): _resp({"job": {"id": "j1"}}),
        })
        self.assertEqual(self.hermes.jobs_list(), [{"id": "j1"}])
        job = self.hermes.job_create("n", "every 1d", "p", deliver="discord")
        self.assertEqual(job["id"], "j2")
        sent = fake.requests[1]["kw"]["json"]
        self.assertEqual(sent["deliver"], "discord")
        self.assertTrue(self.hermes.job_delete("j1"))
        self.assertEqual(self.hermes.job_pause("j1")["id"], "j1")


class ParseSseTest(unittest.TestCase):
    """_parse_sse — SSE 行から (seq, payload) を取り出す純関数"""

    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.hermes = self._iso.import_module("companion.runtime.hermes")

    def test_id_and_data(self):
        evs = list(self.hermes._parse_sse([
            'id: 7',
            'data: {"event": "run.completed", "output": "ok"}',
            '',
        ]))
        self.assertEqual(len(evs), 1)
        seq, payload = evs[0]
        self.assertEqual(seq, 7)
        self.assertEqual(payload["event"], "run.completed")

    def test_comments_ignored(self):
        evs = list(self.hermes._parse_sse([
            ': keepalive',
            ':another comment',
            'data: {"event": "run.completed", "seq": 3}',
            '',
        ]))
        self.assertEqual(len(evs), 1)
        # seq は payload 優先（id 行が無くても取れる）
        self.assertEqual(evs[0][0], 3)

    def test_payload_seq_wins_over_id(self):
        evs = list(self.hermes._parse_sse([
            'id: 1',
            'data: {"seq": 9, "event": "tool.started"}',
            '',
        ]))
        self.assertEqual(evs[0][0], 9)

    def test_multiline_data_joined(self):
        evs = list(self.hermes._parse_sse([
            'data: {"event":',
            'data:  "x"}',
            '',
        ]))
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0][1], {"event": "x"})

    def test_trailing_block_without_blank_line(self):
        evs = list(self.hermes._parse_sse(['data: {"a": 1}']))
        self.assertEqual(len(evs), 1)

    def test_non_json_data_wrapped_raw(self):
        evs = list(self.hermes._parse_sse(['data: not json', '']))
        self.assertEqual(evs[0][1], {"raw": "not json"})

    def test_multiple_events_in_one_buffer(self):
        evs = list(self.hermes._parse_sse([
            'id: 1', 'data: {"n": 1}', '',
            ': comment',
            'id: 2', 'data: {"n": 2}', '',
        ]))
        self.assertEqual([s for s, _ in evs], [1, 2])


class RunEventsTest(unittest.IsolatedAsyncioTestCase):
    """run_events — /v1/runs/{id}/events の SSE 読取（偽 AsyncClient）"""

    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.hermes = self._iso.import_module("companion.runtime.hermes")

    def _client(self, lines=(), status_code=200, exc=None):
        fake = FakeAsyncClient(lines, status_code, exc)
        patcher = mock.patch.object(self.hermes.httpx, "AsyncClient", fake)
        self.addCleanup(patcher.stop)
        patcher.start()
        return fake

    async def test_streams_parsed_events(self):
        fake = self._client(lines=[
            ': keepalive',
            'id: 4',
            'data: {"event": "tool.started", "tool": "browser"}',
            '',
            'id: 5',
            'data: {"event": "run.completed", "output": "done"}',
            '',
        ])
        got = [ev async for ev in self.hermes.run_events("r1")]
        self.assertEqual([s for s, _ in got], [4, 5])
        self.assertEqual(got[1][1]["event"], "run.completed")
        # Bearer 認証で叩く
        self.assertIn("Bearer", fake.init_kwargs["headers"]["Authorization"])

    async def test_last_event_id_header_resumes(self):
        fake = self._client(lines=['data: {"seq": 8}', ''])
        [ev async for ev in self.hermes.run_events("r1", last_seq=7)]
        self.assertEqual(
            fake.init_kwargs["headers"]["Last-Event-ID"], "7"
        )

    async def test_404_raises_api_error_with_status(self):
        self._client(status_code=404)
        with self.assertRaises(self.hermes.BodyAPIError) as cm:
            [ev async for ev in self.hermes.run_events("r9")]
        self.assertEqual(cm.exception.status_code, 404)

    async def test_connect_error_is_unreachable(self):
        self._client(exc=httpx.ConnectError("refused"))
        with self.assertRaises(self.hermes.BodyUnreachableError):
            [ev async for ev in self.hermes.run_events("r1")]

    async def test_missing_key_is_unreachable(self):
        self._client(lines=[])
        with mock.patch.object(self.hermes.hermes_conf, "api_server_key",
                               return_value=("", "")):
            with self.assertRaises(self.hermes.BodyUnreachableError):
                [ev async for ev in self.hermes.run_events("r1")]


class BodyInterfaceTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.body = self._iso.import_module("companion.runtime.body")

    def test_session_label_ops_maps_to_config(self):
        self.assertEqual(self.body._session_for_label("ops"), "companion-ops")
        self.assertIsNone(self.body._session_for_label(""))
        self.assertIsNone(self.body._session_for_label(None))

    def test_session_label_deterministic_slug(self):
        s1 = self.body._session_for_label("レポート作成")
        s2 = self.body._session_for_label("レポート作成")
        self.assertEqual(s1, s2)
        self.assertTrue(s1.startswith("companion-"))
        self.assertNotEqual(s1, self.body._session_for_label("別の作業"))

    def test_start_task_returns_run_id(self):
        with mock.patch.object(self.body.hermes, "create_run",
                               return_value={"run_id": "r1",
                                             "session_id": "bs"}) as m:
            out = self.body.start_task("掃除して", session="ops")
        self.assertEqual(out, {"run_id": "r1", "body_session": "bs"})
        self.assertEqual(m.call_args.kwargs["session_id"], "companion-ops")
        self.assertTrue(m.call_args.kwargs["instructions"])

    def test_start_task_requires_run_id(self):
        with mock.patch.object(self.body.hermes, "create_run",
                               return_value={}):
            with self.assertRaises(self.body.hermes.BodyAPIError):
                self.body.start_task("x")

    def test_run_check_includes_approval(self):
        st = {"status": "waiting_for_approval",
              "approval": {"request_id": "q1", "command": "rm -rf /tmp"}}
        with mock.patch.object(self.body.hermes, "run_status",
                               return_value=st):
            out = self.body.run_check("r1")
        self.assertEqual(out["approval"]["request_id"], "q1")
        self.assertEqual(out["approval"]["summary"], "rm -rf /tmp")

    def test_run_check_no_approval_field(self):
        with mock.patch.object(self.body.hermes, "run_status",
                               return_value={"status": "running"}):
            out = self.body.run_check("r1")
        self.assertNotIn("approval", out)

    def test_run_check_passes_error(self):
        """失敗 run の error 文を呼び出し側（bodyruns 確定）が読める"""
        st = {"status": "failed", "error": "exploded", "output": ""}
        with mock.patch.object(self.body.hermes, "run_status",
                               return_value=st):
            out = self.body.run_check("r1")
        self.assertEqual(out["error"], "exploded")
        self.assertEqual(out["status"], "failed")

    def test_steer_409_is_false(self):
        err = self.body.hermes.BodyAPIError("conflict", status_code=409)
        with mock.patch.object(self.body.hermes, "run_steer", side_effect=err):
            self.assertFalse(self.body.steer("r1", "追加"))
        with mock.patch.object(self.body.hermes, "run_steer",
                               return_value={"accepted": True}):
            self.assertTrue(self.body.steer("r1", "追加"))

    def test_stop_409_is_false(self):
        err = self.body.hermes.BodyAPIError("conflict", status_code=409)
        with mock.patch.object(self.body.hermes, "run_stop", side_effect=err):
            self.assertFalse(self.body.stop("r1"))
        with mock.patch.object(self.body.hermes, "run_stop",
                               return_value={"ok": True}):
            self.assertTrue(self.body.stop("r1"))

    def test_decide_approval_409_is_stale(self):
        err = self.body.hermes.BodyAPIError("conflict", status_code=409)
        with mock.patch.object(self.body.hermes, "run_approval",
                               side_effect=err) as m:
            res = self.body.decide_approval("r1", True, request_id="q1")
        self.assertEqual(res, {"ok": False, "stale": True})
        m.assert_called_once_with("r1", "once", request_id="q1")

    def test_decide_approval_ok_and_deny(self):
        with mock.patch.object(self.body.hermes, "run_approval",
                               return_value={"ok": True}) as m:
            res = self.body.decide_approval("r1", False, request_id="q1")
        self.assertTrue(res["ok"])
        m.assert_called_once_with("r1", "deny", request_id="q1")

    def test_decide_approval_other_error(self):
        err = self.body.hermes.BodyAPIError("teapot", status_code=418)
        with mock.patch.object(self.body.hermes, "run_approval",
                               side_effect=err):
            res = self.body.decide_approval("r1", True)
        self.assertFalse(res["ok"])
        self.assertFalse(res["stale"])
        self.assertIn("teapot", res["error"])

    def test_status_unreachable(self):
        err = self.body.hermes.BodyUnreachableError("down")
        with mock.patch.object(self.body.hermes, "health", side_effect=err):
            st = self.body.status()
        self.assertFalse(st["reachable"])
        self.assertIn("down", st["error"])

    def test_status_reachable_but_no_api(self):
        err = self.body.hermes.BodyUnreachableError("no key")
        with mock.patch.object(self.body.hermes, "health", return_value={}), \
                mock.patch.object(self.body.hermes, "capabilities",
                                  side_effect=err):
            st = self.body.status()
        self.assertTrue(st["reachable"])
        self.assertFalse(st["api_ok"])

    def test_status_ok_collects_features(self):
        caps = {"model": "m", "features": {"runs": True, "jobs": True}}
        with mock.patch.object(self.body.hermes, "health", return_value={}), \
                mock.patch.object(self.body.hermes, "capabilities",
                                  return_value=caps):
            st = self.body.status()
        self.assertTrue(st["api_ok"])
        self.assertEqual(st["features"]["runs"], True)

    def test_send_message_delegates_to_discord_out(self):
        with mock.patch.object(self.body.discord_out, "send_channel_message",
                               new=mock.AsyncMock()) as m:
            asyncio.run(self.body.send_message(123, "おはよ"))
        m.assert_awaited_once_with(123, "おはよ")


class WatchRunTest(unittest.IsolatedAsyncioTestCase):
    """body.watch_run — Hermes イベント名を正規化して捨てる/写す"""

    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.body = self._iso.import_module("companion.runtime.body")

    async def _collect(self, raw_events):
        async def fake_events(run_id, last_seq=-1):
            for e in raw_events:
                yield e
        with mock.patch.object(self.body.hermes, "run_events", fake_events):
            return [ev async for ev in self.body.watch_run("r1")]

    async def test_normalizes_known_events(self):
        got = await self._collect([
            (1, {"event": "message.delta", "x": 1}),         # 捨てる
            (2, {"event": "tool.started", "tool": "browser",
                 "preview": "open"}),
            (3, {"event": "tool.completed", "tool": "browser",
                 "duration": 1.5, "error": False}),
            (4, {"event": "approval.request", "request_id": "q1",
                 "command": "rm x"}),
            (5, {"event": "approval.responded", "choice": "once"}),
            (6, {"event": "run.completed", "output": "done"}),
        ])
        kinds = [e["kind"] for e in got]
        self.assertEqual(
            kinds,
            ["tool_started", "tool_completed", "approval_request",
             "approval_responded", "finished"],
        )
        self.assertEqual(got[0]["seq"], 2)
        self.assertEqual(got[0]["tool"], "browser")
        self.assertTrue(got[3]["kind"] == "approval_responded")
        self.assertEqual(got[4]["status"], "completed")
        self.assertEqual(got[4]["output"], "done")

    async def test_finished_maps_all_terminal_names(self):
        for name, status in (("run.failed", "failed"),
                             ("run.cancelled", "cancelled"),
                             ("run.interrupted", "interrupted")):
            got = await self._collect([(1, {"event": name, "error": "e"})])
            self.assertEqual(got[0]["kind"], "finished")
            self.assertEqual(got[0]["status"], status)

    async def test_approval_summary_fallback(self):
        got = await self._collect([
            (1, {"event": "approval.request", "request_id": "q1"}),
        ])
        self.assertEqual(got[0]["summary"], "(内容不明)")


class BodyToolsTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.tools_body = self._iso.import_module("companion.tools.body")

    def test_status_disconnected(self):
        with mock.patch.object(self.tools_body.body, "status",
                               return_value={"reachable": False,
                                             "error": "timeout"}):
            self.assertIn("繋がらない", self.tools_body.body_status())

    def test_status_connected_lists_features(self):
        st = {"reachable": True, "api_ok": True, "model": "m",
              "features": {"runs": True, "jobs": False}}
        with mock.patch.object(self.tools_body.body, "status",
                               return_value=st), \
                mock.patch.object(self.tools_body.body, "schedule_list",
                                  return_value=[{"id": "j"}]):
            out = self.tools_body.body_status()
        self.assertIn("繋がってる", out)
        self.assertIn("runs", out)
        self.assertIn("1件", out)

    def test_task_start_delegates_to_bodyruns(self):
        with mock.patch.object(self.tools_body.bodyruns, "start",
                               return_value="始めたよ") as m:
            out = self.tools_body.body_task("見てきて", session="rep")
        self.assertEqual(out, "始めたよ")
        self.assertEqual(m.call_args.args[0], "見てきて")
        self.assertEqual(m.call_args.args[1], "rep")

    def test_task_empty_needs_task(self):
        self.assertIn("task", self.tools_body.body_task(""))

    def test_task_run_id_checks_status(self):
        with mock.patch.object(self.tools_body.body, "run_check",
                               return_value={"status": "running",
                                             "run_id": "r9"}) as m:
            out = self.tools_body.body_task(run_id="r9")
        self.assertIn("動いてる", out)
        self.assertIn("r9", out)
        m.assert_called_once_with("r9", backend="hermes")

    def test_task_check_waiting_approval_wording(self):
        """承認待ちは「マスターの承認」（Hermes側で承認して、とは言わない）"""
        st = {"status": "waiting_for_approval", "run_id": "r1",
              "approval": {"request_id": "q1", "summary": "消す"}}
        with mock.patch.object(self.tools_body.body, "run_check",
                               return_value=st):
            out = self.tools_body.body_task(run_id="r1")
        self.assertIn("承認待ち", out)
        self.assertIn("こっち", out)          # Core 側決裁
        self.assertNotIn("Hermes 側で承認", out)

    def test_task_list_shows_runs(self):
        self.db.body_run_insert("r1", "長い仕事", label="job1",
                                origin_session="s")
        self.db.body_run_insert("r2", "済み", origin_session="s")
        self.db.body_run_finish("r2", "completed", output="ok")
        out = self.tools_body.body_task(action="list")
        self.assertIn("r1", out)
        self.assertIn("r2", out)
        self.assertIn("進行中", out)

    def test_task_steer_needs_run_id_and_task(self):
        out = self.tools_body.body_task(action="steer", run_id="r1")
        self.assertIn("要る", out)
        with mock.patch.object(self.tools_body.body, "steer",
                               return_value=True) as m:
            out = self.tools_body.body_task(action="steer", run_id="r1",
                                            task="続き")
        self.assertIn("送った", out)
        m.assert_called_once_with("r1", "続き", backend="hermes")

    def test_task_steer_rejected(self):
        with mock.patch.object(self.tools_body.body, "steer",
                               return_value=False):
            out = self.tools_body.body_task(action="steer", run_id="r1",
                                            task="x")
        self.assertIn("受け付けない", out)

    def test_task_stop(self):
        with mock.patch.object(self.tools_body.body, "stop",
                               return_value=True) as m:
            out = self.tools_body.body_task(action="stop", run_id="r1")
        self.assertIn("止めた", out)
        m.assert_called_once_with("r1", backend="hermes")

    def test_task_start_unreachable(self):
        """start 経路で身体に届かないときは汎用エラーではなく接続文言"""
        err = self.tools_body.body.hermes.BodyUnreachableError("down")
        with mock.patch.object(self.tools_body.bodyruns, "start",
                               side_effect=err):
            out = self.tools_body.body_task("やって")
        self.assertIn("繋がらない", out)

    def test_task_start_api_error(self):
        err = self.tools_body.body.hermes.BodyAPIError(
            "bad request", status_code=422)
        with mock.patch.object(self.tools_body.bodyruns, "start",
                               side_effect=err):
            out = self.tools_body.body_task("やって")
        self.assertIn("受け付けなかった", out)

    def test_task_unknown_action(self):
        self.assertIn("不明", self.tools_body.body_task(action="explode"))

    def test_schedule_list_empty(self):
        with mock.patch.object(self.tools_body.body, "schedule_list",
                               return_value=[]):
            self.assertIn("まだない", self.tools_body.body_schedule("list"))

    def test_schedule_add_requires_params(self):
        out = self.tools_body.body_schedule("add", name="n")
        self.assertIn("要る", out)

    def test_schedule_add_creates(self):
        """§5.3: deliver 省略時は local 固定＋core_report 指示を prompt に焼く"""
        with mock.patch.object(self.tools_body.body, "schedule_create",
                               return_value={"id": "j1"}) as m:
            out = self.tools_body.body_schedule(
                "add", name="朝刊", schedule="0 9 * * *", prompt="ニュース"
            )
        self.assertIn("j1", out)
        self.assertEqual(m.call_args.kwargs["deliver"], "local")
        self.assertIn("core_report", m.call_args.args[2])
        self.assertIn("ニュース", m.call_args.args[2])
        self.assertIn("core_report で私に戻る", out)

    def test_schedule_add_explicit_discord(self):
        """deliver=discord の明示指定なら直送（core_report 指示は付けない）"""
        with mock.patch.object(self.tools_body.body, "schedule_create",
                               return_value={"id": "j2"}) as m:
            out = self.tools_body.body_schedule(
                "add", name="緊急", schedule="0 8 * * *",
                prompt="確認", deliver="discord",
            )
        self.assertEqual(m.call_args.kwargs["deliver"], "discord")
        self.assertNotIn("core_report", m.call_args.args[2])
        self.assertIn("discord へ配送", out)

    def test_schedule_remove(self):
        with mock.patch.object(self.tools_body.body, "schedule_delete",
                               return_value=True):
            self.assertIn("消した", self.tools_body.body_schedule(
                "remove", job_id="j1"))

    def test_schedule_pause_resume_run(self):
        job = {"id": "j1", "name": "朝刊"}
        for action, word in (("pause", "止めた"), ("resume", "再開した"),
                             ("run", "走らせた")):
            with mock.patch.object(self.tools_body.body,
                                   f"schedule_{'run_now' if action == 'run' else action}",
                                   return_value=job):
                self.assertIn(word, self.tools_body.body_schedule(
                    action, job_id="j1"))

    def test_schedule_unknown_action(self):
        self.assertIn("不明", self.tools_body.body_schedule("explode"))


if __name__ == "__main__":
    unittest.main()
