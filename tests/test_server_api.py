"""ステップ6C: API互換範囲の明示。

- session フィールドでセッションを識別（省略時はdesktop互換）
- 処理対象は最後のuser発言のみ（送った過去履歴は連結しない）
- text / image_url 以外のcontent型は「読み取れない添付」の印を本文に残して通す
- actor はサーバー側で確定する（ヘッダ無し=desktop、Hermes由来は
  state.db 解決。ここでは resolve_actor を差し替えて検証）
"""
import asyncio
import json
import unittest
from unittest import mock

import httpx

import _support


class ServerApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []

        async def fake_generate(session_key, user_text, **kw):
            self.calls.append({"session": session_key, "text": user_text, **kw})
            return {"reply": "返信だよ", "inner": "内面", "tools": ["x"], "model": "m"}

        engine_stub = _support.module_stub("companion.engine", generate=fake_generate)
        delegate_stub = _support.module_stub(
            "companion.tools.delegate",
            pending_approvals=lambda: [{"id": 7}],
            delegate_approve=lambda jid, who: f"ok {jid} {who}",
            delegate_deny=lambda jid, who="": f"deny {jid} {who}",
            find_pending_by_code=lambda code: None,
        )
        self._iso = _support.IsolatedModules(
            stubs={
                "companion.engine": engine_stub,
                "companion.tools.delegate": delegate_stub,
            }
        )
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.server = self._iso.import_module("companion.server")
        # Hermes 由来リクエストの actor 解決は差し替え（state.db 越しの実解決は
        # test_actor_resolution.py で検証）
        self.actor_result = "12345"   # fake cfg OWNER_USER_ID と同じ
        resolver = mock.patch.object(
            self.server.body_rt, "resolve_actor",
            side_effect=lambda _aff: self.actor_result)
        resolver.start()
        self.addCleanup(resolver.stop)

    def _client(self):
        app = self.server.create_app()
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    async def test_default_session_is_desktop(self):
        async with self._client() as c:
            res = await c.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "こんにちは"}],
            })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.calls[0]["session"], "desktop")
        self.assertEqual(self.calls[0]["text"], "こんにちは")
        body = res.json()
        self.assertEqual(body["choices"][0]["message"]["content"], "返信だよ")
        self.assertEqual(body["companion"]["inner"], "内面")

    async def test_explicit_session_is_used(self):
        async with self._client() as c:
            await c.post("/v1/chat/completions", json={
                "session": "mobile-app",
                "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(self.calls[0]["session"], "mobile-app")

    async def test_hermes_session_header_wins(self):
        """Hermes の session_affinity_header → セッション識別 + source=hermes"""
        async with self._client() as c:
            await c.post(
                "/v1/chat/completions",
                headers={"x-companion-session": "hermes-discord-123"},
                json={
                    "session": "ignored-body-session",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        self.assertEqual(self.calls[0]["session"], "hermes-discord-123")
        self.assertEqual(self.calls[0]["source"], "hermes")

    async def test_no_header_is_plain_api(self):
        async with self._client() as c:
            await c.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(self.calls[0]["source"], "api")

    async def test_only_last_user_message_processed(self):
        """クライアントが送る過去履歴は結合しない。最後のuser発言だけを処理"""
        async with self._client() as c:
            await c.post("/v1/chat/completions", json={
                "messages": [
                    {"role": "user", "content": "1番目の発言"},
                    {"role": "assistant", "content": "昔の返答"},
                    {"role": "user", "content": "最新の発言"},
                ],
            })
        self.assertEqual(self.calls[0]["text"], "最新の発言")

    async def test_image_parts_passed_to_engine(self):
        async with self._client() as c:
            res = await c.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "これ見て"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                ]}],
            })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.calls[0]["text"], "これ見て")
        self.assertEqual(self.calls[0]["images"], ["data:image/png;base64,AA"])

    async def test_unsupported_content_becomes_note(self):
        """非対応content型は400ではなく「読めない添付」の印を残して往復を通す"""
        async with self._client() as c:
            res = await c.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "音声も"},
                    {"type": "input_audio", "input_audio": {"data": "AA"}},
                ]}],
            })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.calls[0]["text"],
                         "音声も\n（添付「input_audio」は読み取れない形式です）")

    async def test_unsupported_file_part_keeps_filename(self):
        """file型はファイル名が取れるとき名前付きで残す"""
        async with self._client() as c:
            res = await c.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": [
                    {"type": "file",
                     "file": {"filename": "memo.pdf", "file_data": "AA"}},
                ]}],
            })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.calls[0]["text"],
                         "（添付「memo.pdf」は読み取れない形式です）")

    async def test_malformed_body_rejected_400(self):
        """JSONデコード不能なボディは500ではなく400。engineには届かない"""
        async with self._client() as c:
            res = await c.post(
                "/v1/chat/completions",
                content=b"\x82\xb1\x82\xf1",   # UTF-8として不正なバイト列
                headers={"content-type": "application/json"},
            )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["type"], "bad_request")
        self.assertEqual(self.calls, [])

    async def test_actor_fixed_server_side(self):
        """クライアントがactorらしき値を送ってもサーバー側でdesktopに確定"""
        async with self._client() as c:
            await c.post("/v1/chat/completions", json={
                "actor": "evil",
                "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(self.calls[0]["actor"], "desktop")

    async def test_stream_splits_after_generation(self):
        async with self._client() as c:
            res = await c.post("/v1/chat/completions", json={
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(res.status_code, 200)
        self.assertIn("data: ", res.text)
        self.assertTrue(res.text.rstrip().endswith("data: [DONE]"))
        self.assertIn("返信だよ", res.text)

    # --- 身体承認の決裁コード（「承認 1234」）の横取り ---

    async def test_approval_code_invokes_decide_without_leaking_code(self):
        pending = {"id": 5, "request": "ファイルを消す", "expires_at": "x"}
        decide = mock.MagicMock(return_value=(True, "承認を身体に送ったよ"))
        with mock.patch.object(self.server.bodyruns, "find_pending_by_code",
                               return_value=pending), \
                mock.patch.object(self.server.bodyruns, "decide", decide):
            async with self._client() as c:
                res = await c.post("/v1/chat/completions", json={
                    "messages": [{"role": "user", "content": "承認 1234"}],
                })
        self.assertEqual(res.status_code, 200)
        decide.assert_called_once_with(
            code="1234", approve=True, decided_by="desktop"
        )
        text = self.calls[0]["text"]
        # generate には結果要約が渡るがコードは渡さない
        self.assertIn("承認", text)
        self.assertIn("ファイルを消す", text)
        self.assertIn("承認を身体に送ったよ", text)
        self.assertNotIn("1234", text)

    async def test_approval_code_fullwidth_digits(self):
        pending = {"id": 5, "request": "消す", "expires_at": "x"}
        decide = mock.MagicMock(return_value=(False, "期限切れ"))
        with mock.patch.object(self.server.bodyruns, "find_pending_by_code",
                               return_value=pending), \
                mock.patch.object(self.server.bodyruns, "decide", decide):
            async with self._client() as c:
                await c.post("/v1/chat/completions", json={
                    "messages": [{"role": "user", "content": "却下 ４３２１"}],
                })
        decide.assert_called_once_with(
            code="4321", approve=False, decided_by="desktop"
        )

    async def test_approval_code_hermes_session_marks_decider(self):
        """Hermes 由来の決裁は解決済み actor（ownerのユーザーID）で記録する"""
        pending = {"id": 5, "request": "消す", "expires_at": "x"}
        decide = mock.MagicMock(return_value=(True, "ok"))
        with mock.patch.object(self.server.bodyruns, "find_pending_by_code",
                               return_value=pending), \
                mock.patch.object(self.server.bodyruns, "decide", decide):
            async with self._client() as c:
                await c.post(
                    "/v1/chat/completions",
                    headers={"x-companion-session": "hermes-1"},
                    json={"messages": [{"role": "user",
                                        "content": "承認 1234"}]},
                )
        self.assertEqual(decide.call_args.kwargs["decided_by"], "12345")

    async def test_non_owner_cannot_consume_approval_code(self):
        """非owner・未解決 actor の「承認 NNNN」は決裁経路に届かない"""
        for actor in ("99999", None):
            self.actor_result = actor
            self.calls.clear()
            find = mock.MagicMock()
            decide = mock.MagicMock()
            with mock.patch.object(
                    self.server.bodyruns, "find_pending_by_code", find), \
                    mock.patch.object(self.server.bodyruns, "decide", decide):
                async with self._client() as c:
                    res = await c.post(
                        "/v1/chat/completions",
                        headers={"x-companion-session": "hermes-1"},
                        json={"messages": [{"role": "user",
                                            "content": "承認 1234"}]},
                    )
            self.assertEqual(res.status_code, 200)
            text = res.json()["choices"][0]["message"]["content"]
            self.assertIn("マスター本人", text)
            find.assert_not_called()
            decide.assert_not_called()
            self.assertEqual(self.calls, [])   # LLM には届かない

    async def test_approval_code_in_hermes_wrapped_text(self):
        """Hermes が付ける [Triggering message id: ...] 等の装飾行が
        前にあっても「承認 NNNN」の単独行は決裁に引き当てる"""
        pending = {"id": 5, "request": "消す", "expires_at": "x"}
        decide = mock.MagicMock(return_value=(True, "ok"))
        with mock.patch.object(self.server.bodyruns, "find_pending_by_code",
                               return_value=pending), \
                mock.patch.object(self.server.bodyruns, "decide", decide):
            async with self._client() as c:
                await c.post("/v1/chat/completions", json={
                    "messages": [{"role": "user", "content":
                        "[Triggering message id: `1` — note.]\n\n承認0621"}],
                })
        decide.assert_called_once_with(
            code="0621", approve=True, decided_by="desktop")

    async def test_approval_code_mid_line_not_matched(self):
        """「承認 1234 して」のように行内に埋まったコードは決裁に引き当てず
        普通の発言として LLM に渡す"""
        find = mock.MagicMock()
        with mock.patch.object(self.server.bodyruns, "find_pending_by_code",
                               find):
            async with self._client() as c:
                await c.post("/v1/chat/completions", json={
                    "messages": [{"role": "user",
                                  "content": "承認 1234 して"}],
                })
        find.assert_not_called()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["text"], "承認 1234 して")

    async def test_delegate_code_dispatches_to_delegate_path(self):
        """委譲承認コードは body の後に引いて delegate の明示経路へ"""
        app = mock.MagicMock(return_value="起動した")
        deny = mock.MagicMock(return_value="取り消した")
        with mock.patch.object(
                self.server.bodyruns, "find_pending_by_code",
                return_value=None), \
            mock.patch.object(
                self.server.delegate, "find_pending_by_code",
                return_value={"id": 7, "request": "修正して"}), \
            mock.patch.object(
                self.server.delegate, "delegate_approve", app), \
            mock.patch.object(
                self.server.delegate, "delegate_deny", deny):
            async with self._client() as c:
                await c.post("/v1/chat/completions", json={
                    "messages": [{"role": "user", "content": "承認 5555"}]})
                await c.post("/v1/chat/completions", json={
                    "messages": [{"role": "user", "content": "却下 5555"}]})
        app.assert_called_once_with(7, "desktop")
        deny.assert_called_once_with(7, "desktop")
        # 差し替え文に依頼要約と結果は入るがコードは入らない
        text = self.calls[-1]["text"]
        self.assertIn("修正して", text)
        self.assertIn("取り消した", text)
        self.assertNotIn("5555", text)

    async def test_pending_approvals_hidden_from_non_owner(self):
        """承認待ち一覧（内容要約）は非ownerの応答に混ぜない"""
        self.actor_result = "99999"
        async with self._client() as c:
            res = await c.post(
                "/v1/chat/completions",
                headers={"x-companion-session": "hermes-1"},
                json={"messages": [{"role": "user", "content": "hi"}]})
        pas = res.json()["companion"]["pending_approvals"]
        self.assertEqual(pas, [])

    async def test_actor_mode_legacy_treats_hermes_as_desktop(self):
        with mock.patch.object(self.server, "COMPANION_ACTOR_MODE", "legacy"):
            self.actor_result = None   # resolve に依らず desktop
            async with self._client() as c:
                await c.post("/v1/chat/completions",
                             headers={"x-companion-session": "h"},
                             json={"messages": [{"role": "user",
                                                 "content": "hi"}]})
        self.assertEqual(self.calls[0]["actor"], "desktop")

    async def test_actor_mode_off_drops_user_ids(self):
        """off: ローカル面のみ desktop、user_id は非ownerに倒す"""
        with mock.patch.object(self.server, "COMPANION_ACTOR_MODE", "off"):
            self.actor_result = "12345"   # owner id でも採用しない
            async with self._client() as c:
                await c.post("/v1/chat/completions",
                             headers={"x-companion-session": "h"},
                             json={"messages": [{"role": "user",
                                                 "content": "hi"}]})
            self.assertIsNone(self.calls[0]["actor"])
            self.actor_result = "desktop"
            async with self._client() as c:
                await c.post("/v1/chat/completions",
                             headers={"x-companion-session": "h"},
                             json={"messages": [{"role": "user",
                                                 "content": "hi"}]})
            self.assertEqual(self.calls[1]["actor"], "desktop")

    async def test_unknown_code_passes_through_unchanged(self):
        """数字がpendingに無ければ普通の発言としてgenerateに渡る"""
        with mock.patch.object(self.server.bodyruns, "find_pending_by_code",
                               return_value=None):
            async with self._client() as c:
                await c.post("/v1/chat/completions", json={
                    "messages": [{"role": "user", "content": "承認 9999"}],
                })
        self.assertEqual(self.calls[0]["text"], "承認 9999")

    async def test_unrelated_four_digits_pass_through(self):
        async with self._client() as c:
            await c.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "あと1234円払った"}],
            })
        self.assertEqual(self.calls[0]["text"], "あと1234円払った")

    async def test_body_approvals_endpoints(self):
        pending = [{"id": 5, "kind": "body_run", "request": "消す",
                    "expires_at": "2099-01-01 00:00:00"}]
        decide = mock.MagicMock(return_value=(True, "却下を送ったよ"))
        with mock.patch.object(self.server.bodyruns, "pending_approvals",
                               return_value=pending), \
                mock.patch.object(self.server.bodyruns, "decide", decide):
            async with self._client() as c:
                res = await c.get("/v1/body/approvals")
                self.assertEqual(res.status_code, 200)
                body = res.json()
                self.assertEqual(body["pending"], pending)
                # 承認コードは決して載らない
                self.assertNotIn("code", res.text)
                res = await c.post("/v1/body/approvals/5",
                                   json={"decision": "deny"})
                self.assertEqual(res.status_code, 200)
                self.assertEqual(
                    res.json(), {"ok": True, "result": "却下を送ったよ"}
                )
                decide.assert_called_once_with(
                    approval_id=5, approve=False, decided_by="desktop"
                )
                res = await c.post("/v1/body/approvals/5",
                                   json={"decision": "maybe"})
                self.assertEqual(res.status_code, 400)

    async def test_chat_pending_approvals_merges_delegate_and_body(self):
        with mock.patch.object(
                self.server.delegate, "pending_approvals",
                return_value=[{"id": 7, "request": "委譲"}]), \
                mock.patch.object(
                    self.server.bodyruns, "pending_approvals",
                    return_value=[{"id": 5, "kind": "body_run",
                                   "request": "消す",
                                   "expires_at": "x"}]):
            async with self._client() as c:
                res = await c.post("/v1/chat/completions", json={
                    "messages": [{"role": "user", "content": "hi"}],
                })
        pas = res.json()["companion"]["pending_approvals"]
        kinds = {p["kind"] for p in pas}
        self.assertEqual(kinds, {"delegate", "body_run"})
        self.assertNotIn("code", res.text)

    async def test_ui_page_open_but_data_stays_gated(self):
        """GET /ui は器なので鍵無しで200。enforce + 鍵設定済みでも素通しし、
        /v1 系は従来どおり鍵を要求する"""
        with mock.patch.object(
                self.server.hermes_conf, "companion_key",
                return_value=("secret", "test")), \
                mock.patch.object(
                    self.server, "COMPANION_API_AUTH", "enforce"):
            async with self._client() as c:
                res = await c.get("/ui")
                self.assertEqual(res.status_code, 200)
                self.assertIn("text/html", res.headers["content-type"])
                res = await c.get("/v1/delegate/pending")
                self.assertEqual(res.status_code, 401)
                res = await c.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]})
                self.assertEqual(res.status_code, 401)
                res = await c.get(
                    "/v1/delegate/pending",
                    headers={"x-companion-key": "secret"})
                self.assertEqual(res.status_code, 200)

    async def test_root_redirects_to_ui(self):
        with mock.patch.object(
                self.server.hermes_conf, "companion_key",
                return_value=("secret", "test")), \
                mock.patch.object(
                    self.server, "COMPANION_API_AUTH", "enforce"):
            async with self._client() as c:
                res = await c.get("/", follow_redirects=False)
        self.assertEqual(res.status_code, 307)
        self.assertEqual(res.headers["location"], "/ui")

    async def test_delegate_deny_endpoint(self):
        """ローカルUI の却下ボタン経路。decided_by は desktop で記録"""
        deny = mock.MagicMock(return_value="取り消した")
        with mock.patch.object(self.server.delegate, "delegate_deny", deny):
            async with self._client() as c:
                res = await c.post("/v1/delegate/7/deny")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["result"], "取り消した")
        deny.assert_called_once_with(7, "desktop")


def _fn(name):
    return {"type": "function",
            "function": {"name": name, "description": "d",
                         "parameters": {"type": "object"}}}


class ServerBridgeTest(unittest.IsolatedAsyncioTestCase):
    """形式橋（BODY_BRIDGE + via_hermes + tools）の保留/継続/中断/再送"""

    TOOLS = [_fn("web_search"), _fn("terminal"), _fn("delegate_task")]
    HEADERS = {"x-companion-session": "h-sess"}

    def setUp(self):
        self.gen_calls = []
        self.emit_errors = []

        async def fake_generate(session_key, user_text, bridge=None, **kw):
            self.gen_calls.append(
                {"session": session_key, "text": user_text,
                 "bridge": bridge is not None})
            if bridge is not None:
                try:
                    results = await bridge.emit(
                        [{"id": "tc-1", "name": "web_search",
                          "arguments": '{"q": "x"}'}])
                except Exception as e:
                    self.emit_errors.append(e)
                    raise
                return {"reply": "結果は" + results[0], "inner": "",
                        "tools": ["web_search"], "model": "m"}
            return {"reply": "返信だよ", "inner": "", "tools": [],
                    "model": "m"}

        engine_stub = _support.module_stub(
            "companion.engine", generate=fake_generate)
        delegate_stub = _support.module_stub(
            "companion.tools.delegate",
            pending_approvals=lambda: [],
            delegate_approve=lambda jid, who: "ok",
            delegate_deny=lambda jid, who="": "deny",
            find_pending_by_code=lambda code: None,
        )
        self._iso = _support.IsolatedModules(
            stubs={
                "companion.engine": engine_stub,
                "companion.tools.delegate": delegate_stub,
            }
        )
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.server = self._iso.import_module("companion.server")
        self.bridge = self._iso.import_module("companion.bridge")
        # 橋の検証は actor 解決と無関係に行う（owner 固定）
        resolver = mock.patch.object(
            self.server.body_rt, "resolve_actor", return_value="12345")
        resolver.start()
        self.addCleanup(resolver.stop)

    def _client(self):
        app = self.server.create_app()
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    def _new_turn(self, **extra):
        return {
            "messages": [{"role": "user", "content": "調べて"}],
            "tools": self.TOOLS,
            **extra,
        }

    def _continuation(self, tool_msgs, extra_tail=()):
        msgs = [
            {"role": "user", "content": "調べて"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "tc-1", "type": "function",
                             "function": {"name": "web_search",
                                          "arguments": "{}"}}]},
            *tool_msgs,
            *extra_tail,
        ]
        return {"messages": msgs, "tools": self.TOOLS}

    async def test_tool_calls_response_nonstream(self):
        async with self._client() as c:
            res = await c.post("/v1/chat/completions",
                               headers=self.HEADERS, json=self._new_turn())
        self.assertEqual(res.status_code, 200)
        body = res.json()
        ch = body["choices"][0]
        self.assertEqual(ch["finish_reason"], "tool_calls")
        msg = ch["message"]
        self.assertIsNone(msg["content"])
        tc = msg["tool_calls"][0]
        self.assertEqual(tc["id"], "tc-1")
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "web_search")
        self.assertIsInstance(tc["function"]["arguments"], str)
        # 途中ラウンドには companion 独自フィールドを付けない
        self.assertNotIn("companion", body)
        # generate は bridge 付きで走った
        self.assertTrue(self.gen_calls[0]["bridge"])

    async def test_tool_calls_response_stream(self):
        async with self._client() as c:
            res = await c.post(
                "/v1/chat/completions", headers=self.HEADERS,
                json=self._new_turn(stream=True))
        self.assertEqual(res.status_code, 200)
        lines = [ln for ln in res.text.splitlines() if ln.startswith("data: ")]
        chunks = [json.loads(ln[6:]) for ln in lines if ln != "data: [DONE]"]
        deltas = [c["choices"][0] for c in chunks]
        self.assertIn("tool_calls", deltas[0]["delta"])
        self.assertEqual(
            deltas[0]["delta"]["tool_calls"][0]["function"]["name"],
            "web_search")
        self.assertEqual(deltas[-1]["finish_reason"], "tool_calls")
        self.assertTrue(res.text.rstrip().endswith("data: [DONE]"))
        self.assertNotIn("companion", res.text)

    async def test_continuation_resolves_and_returns_final(self):
        async with self._client() as c:
            await c.post("/v1/chat/completions",
                         headers=self.HEADERS, json=self._new_turn())
            res = await c.post(
                "/v1/chat/completions", headers=self.HEADERS,
                json=self._continuation(
                    [{"role": "tool", "tool_call_id": "tc-1",
                      "content": "曇り"}]))
        body = res.json()
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["choices"][0]["message"]["content"],
                         "結果は曇り")
        self.assertIn("companion", body)

    async def test_new_user_message_aborts_old_suspension(self):
        """保留中に新しい user 発言 → 旧 emit は BridgeAborted、新規 generate"""
        async with self._client() as c:
            await c.post("/v1/chat/completions",
                         headers=self.HEADERS, json=self._new_turn())
            res = await c.post(
                "/v1/chat/completions", headers=self.HEADERS,
                json={"messages": [{"role": "user", "content": "別の話"}],
                      "tools": self.TOOLS})
        # 旧 emit に BridgeAborted が届き、新しい generate が走った
        self.assertTrue(any(
            isinstance(e, self.bridge.BridgeAborted)
            for e in self.emit_errors))
        self.assertEqual(len(self.gen_calls), 2)
        self.assertEqual(self.gen_calls[1]["text"], "別の話")

    async def test_orphaned_tool_tail_gets_apology_without_generate(self):
        """保留が無いのに末尾が tool → 途切れた継続として謝罪。generate 不発"""
        async with self._client() as c:
            res = await c.post(
                "/v1/chat/completions", headers=self.HEADERS,
                json={
                    "messages": [
                        {"role": "user", "content": "調べて"},
                        {"role": "assistant", "tool_calls": [
                            {"id": "zz", "type": "function",
                             "function": {"name": "web_search",
                                          "arguments": "{}"}}]},
                        {"role": "tool", "tool_call_id": "zz",
                         "content": "曇り"},
                    ],
                    "tools": self.TOOLS,
                })
        self.assertEqual(res.status_code, 200)
        text = res.json()["choices"][0]["message"]["content"]
        self.assertIn("途切れ", text)
        self.assertEqual(res.json()["choices"][0]["finish_reason"], "stop")
        self.assertEqual(self.gen_calls, [])

    async def test_steer_appended_to_last_tool_result(self):
        """継続の末尾 tool の後ろの user 発言（/steer）は結果に追記される"""
        async with self._client() as c:
            await c.post("/v1/chat/completions",
                         headers=self.HEADERS, json=self._new_turn())
            res = await c.post(
                "/v1/chat/completions", headers=self.HEADERS,
                json=self._continuation(
                    [{"role": "tool", "tool_call_id": "tc-1",
                      "content": "曇り"}],
                    extra_tail=[{"role": "user",
                                 "content": "やっぱり大阪で"}]))
        text = res.json()["choices"][0]["message"]["content"]
        self.assertIn("曇り", text)
        self.assertIn("マスターからの追加指示", text)
        self.assertIn("やっぱり大阪で", text)

    async def test_positional_fallback_when_ids_differ(self):
        """tool_call_id が食い違っても、関数名列と個数が一致すれば位置で対応"""
        async with self._client() as c:
            await c.post("/v1/chat/completions",
                         headers=self.HEADERS, json=self._new_turn())
            res = await c.post(
                "/v1/chat/completions", headers=self.HEADERS,
                json=self._continuation(
                    [{"role": "tool", "tool_call_id": "different-id",
                      "content": "曇り"}]))
        self.assertEqual(res.json()["choices"][0]["message"]["content"],
                         "結果は曇り")

    async def test_resend_same_request_reuses_result(self):
        """同一リクエストの再送は generate を再実行しない"""
        payload = self._new_turn()
        async with self._client() as c:
            r1 = await c.post("/v1/chat/completions",
                              headers=self.HEADERS, json=payload)
            r2 = await c.post("/v1/chat/completions",
                              headers=self.HEADERS, json=payload)
        self.assertEqual(len(self.gen_calls), 1)
        self.assertEqual(r1.json()["choices"][0]["finish_reason"],
                         "tool_calls")
        self.assertEqual(r2.json()["choices"][0]["finish_reason"],
                         "tool_calls")

    async def test_inflight_resend_waits_for_first(self):
        """処理中の同一リクエストは同じタスクの結果を待つ"""
        gate = asyncio.Event()

        orig_generate = None

        async def slow_generate(session_key, user_text, bridge=None, **kw):
            self.gen_calls.append({"session": session_key})
            if bridge is not None:
                await gate.wait()
                results = await bridge.emit(
                    [{"id": "tc-1", "name": "web_search",
                      "arguments": "{}"}])
                return {"reply": "r", "inner": "", "tools": [], "model": "m"}
            return {"reply": "r", "inner": "", "tools": [], "model": "m"}

        with mock.patch.object(self.server, "generate", slow_generate):
            async with self._client() as c:
                payload = self._new_turn()
                t1 = asyncio.create_task(c.post(
                    "/v1/chat/completions", headers=self.HEADERS,
                    json=payload))
                await asyncio.sleep(0.05)
                t2 = asyncio.create_task(c.post(
                    "/v1/chat/completions", headers=self.HEADERS,
                    json=payload))
                await asyncio.sleep(0.05)
                gate.set()
                r1, r2 = await asyncio.gather(t1, t2)
        self.assertEqual(len(self.gen_calls), 1)
        self.assertEqual(r1.json()["choices"][0]["finish_reason"],
                         "tool_calls")
        self.assertEqual(r2.json()["choices"][0]["finish_reason"],
                         "tool_calls")

    async def test_no_tools_is_legacy_path(self):
        """tools 無しの via_hermes は従来経路（bridge 無し generate）"""
        async with self._client() as c:
            res = await c.post(
                "/v1/chat/completions", headers=self.HEADERS,
                json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(res.json()["choices"][0]["finish_reason"], "stop")
        self.assertFalse(self.gen_calls[0]["bridge"])

    async def test_body_bridge_off_is_legacy(self):
        with mock.patch.object(self.server, "BODY_BRIDGE", False):
            async with self._client() as c:
                res = await c.post("/v1/chat/completions",
                                   headers=self.HEADERS,
                                   json=self._new_turn())
        self.assertEqual(res.json()["choices"][0]["finish_reason"], "stop")
        self.assertFalse(self.gen_calls[0]["bridge"])

    async def test_two_round_bridge_turn(self):
        """emit→継続→再emit→継続→最終応答 の複数ラウンドを通す"""
        async def gen2(session_key, user_text, bridge=None, **kw):
            self.gen_calls.append({"text": user_text})
            r1 = await bridge.emit(
                [{"id": "tc-1", "name": "web_search", "arguments": "{}"}])
            r2 = await bridge.emit(
                [{"id": "tc-2", "name": "web_search", "arguments": "{}"}])
            return {"reply": f"{r1[0]}と{r2[0]}", "inner": "",
                    "tools": ["web_search"], "model": "m"}

        second_continuation = {"messages": [
            {"role": "user", "content": "調べて"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc-1", "type": "function",
                 "function": {"name": "web_search", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "tc-1", "content": "雨"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc-2", "type": "function",
                 "function": {"name": "web_search", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "tc-2", "content": "雪"},
        ], "tools": self.TOOLS}

        with mock.patch.object(self.server, "generate", gen2):
            async with self._client() as c:
                r1 = await c.post("/v1/chat/completions",
                                  headers=self.HEADERS, json=self._new_turn())
                self.assertEqual(r1.json()["choices"][0]["finish_reason"],
                                 "tool_calls")
                self.assertEqual(
                    r1.json()["choices"][0]["message"]["tool_calls"][0]["id"],
                    "tc-1")
                # 1回目の継続 → さらに tool_calls が返る（2ラウンド目）
                r2 = await c.post(
                    "/v1/chat/completions", headers=self.HEADERS,
                    json=self._continuation(
                        [{"role": "tool", "tool_call_id": "tc-1",
                          "content": "雨"}]))
                ch2 = r2.json()["choices"][0]
                self.assertEqual(ch2["finish_reason"], "tool_calls")
                self.assertEqual(
                    ch2["message"]["tool_calls"][0]["id"], "tc-2")
                # 2回目の継続 → 最終応答
                r3 = await c.post("/v1/chat/completions",
                                  headers=self.HEADERS,
                                  json=second_continuation)
                ch3 = r3.json()["choices"][0]
                self.assertEqual(ch3["finish_reason"], "stop")
                self.assertEqual(ch3["message"]["content"], "雨と雪")
        # 保留は最終応答で解放されている
        self.assertIsNone(self.bridge._suspended.get("h-sess"))

    async def test_continuation_does_not_intercept_approval_code(self):
        """継続リクエストでは「承認 1234」横取りを走らせない"""
        with mock.patch.object(self.server.bodyruns,
                               "find_pending_by_code") as fp:
            async with self._client() as c:
                await c.post("/v1/chat/completions",
                             headers=self.HEADERS, json=self._new_turn())
                await c.post(
                    "/v1/chat/completions", headers=self.HEADERS,
                    json=self._continuation(
                        [{"role": "tool", "tool_call_id": "tc-1",
                          "content": "曇り"}],
                        extra_tail=[{"role": "user",
                                     "content": "承認 1234"}]))
        fp.assert_not_called()


class ServerAuthTest(unittest.IsolatedAsyncioTestCase):
    """:8788 入口鍵（COMPANION_API_AUTH = off|observe|enforce）のテスト"""

    def setUp(self):
        self.calls = []

        async def fake_generate(session_key, user_text, **kw):
            self.calls.append({"session": session_key, "text": user_text, **kw})
            return {"reply": "ok", "inner": "", "tools": [], "model": "m"}

        engine_stub = _support.module_stub("companion.engine", generate=fake_generate)
        delegate_stub = _support.module_stub(
            "companion.tools.delegate",
            pending_approvals=lambda: [],
            delegate_approve=lambda jid, who: "ok",
            delegate_deny=lambda jid, who="": "deny",
            find_pending_by_code=lambda code: None,
        )
        self._iso = _support.IsolatedModules(
            stubs={
                "companion.engine": engine_stub,
                "companion.tools.delegate": delegate_stub,
            }
        )
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.server = self._iso.import_module("companion.server")

    def _client(self):
        app = self.server.create_app()
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    def _post(self, c, headers=None):
        return c.post("/v1/chat/completions", headers=headers or {}, json={
            "messages": [{"role": "user", "content": "hi"}],
        })

    async def test_observe_no_key_passes(self):
        """observe: 鍵なしでも素通し（警告ログのみ）"""
        with mock.patch.object(self.server.hermes_conf, "companion_key",
                               return_value=("secret", "env")):
            async with self._client() as c:
                res = await self._post(c)
        self.assertEqual(res.status_code, 200)

    async def test_enforce_no_key_is_401(self):
        with mock.patch.object(self.server, "COMPANION_API_AUTH", "enforce"), \
                mock.patch.object(self.server.hermes_conf, "companion_key",
                                  return_value=("secret", "env")):
            async with self._client() as c:
                res = await self._post(c)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["type"], "unauthorized")
        self.assertEqual(self.calls, [])

    async def test_enforce_wrong_key_is_401(self):
        with mock.patch.object(self.server, "COMPANION_API_AUTH", "enforce"), \
                mock.patch.object(self.server.hermes_conf, "companion_key",
                                  return_value=("secret", "env")):
            async with self._client() as c:
                res = await self._post(c, headers={"x-companion-key": "wrong"})
        self.assertEqual(res.status_code, 401)

    async def test_enforce_right_key_200(self):
        """x-companion-key と Authorization: Bearer の両方を受ける"""
        with mock.patch.object(self.server, "COMPANION_API_AUTH", "enforce"), \
                mock.patch.object(self.server.hermes_conf, "companion_key",
                                  return_value=("secret", "env")):
            async with self._client() as c:
                r1 = await self._post(c, headers={"x-companion-key": "secret"})
                r2 = await self._post(
                    c, headers={"Authorization": "Bearer secret"}
                )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(len(self.calls), 2)

    async def test_unconfigured_key_passes_even_in_enforce(self):
        """入口鍵自体が未設定なら enforce でも素通し（infoログのみ）"""
        with mock.patch.object(self.server, "COMPANION_API_AUTH", "enforce"), \
                mock.patch.object(self.server.hermes_conf, "companion_key",
                                  return_value=("", "")):
            async with self._client() as c:
                res = await self._post(c)
        self.assertEqual(res.status_code, 200)

    async def test_healthz_open_in_enforce(self):
        with mock.patch.object(self.server, "COMPANION_API_AUTH", "enforce"), \
                mock.patch.object(self.server.hermes_conf, "companion_key",
                                  return_value=("secret", "env")):
            async with self._client() as c:
                res = await c.get("/healthz")
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
