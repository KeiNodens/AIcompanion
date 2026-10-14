"""mcp_stdio.py — stdio↔/mcp 中継 shim の配線テスト。

実 stdio・実 HTTP は使わない。stdio_server を anyio の memory stream
に差し替えてテスト側から本物の MCP ClientSession で JSON-RPC を投げ、
上流（companion の /mcp）側の ClientSession をモックにして
list_tools/call_tool の中継・Bearer 付与・error 透過を見る。
"""
import asyncio
import contextlib
import unittest
from unittest import mock

import _support


class McpStdioBridgeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.cfg = self._iso.cfg
        self.cfg.COMPANION_MCP_KEY = "mcp-key-x"
        self.mod = self._iso.import_module("companion.mcp_stdio")
        self.http_calls = []

    def _fake_upstream(self, tools=None, call_result=None, call_error=None):
        """上流 ClientSession（偽）と streamablehttp_client の差し替え物"""
        import mcp.types as mt

        tools = tools if tools is not None else [
            mt.Tool(name="core_context", description="ctx",
                    inputSchema={"type": "object", "properties": {}}),
        ]
        call_result = call_result or mt.CallToolResult(
            content=[mt.TextContent(type="text", text="upstream result")])

        upstream = mock.MagicMock()
        upstream.initialize = mock.AsyncMock(return_value=None)
        upstream.list_tools = mock.AsyncMock(
            return_value=mt.ListToolsResult(tools=tools))
        if call_error is not None:
            upstream.call_tool = mock.AsyncMock(side_effect=call_error)
        else:
            upstream.call_tool = mock.AsyncMock(return_value=call_result)
        # `async with ClientSession(...) as upstream:` が同じ mock を返すよう設定
        upstream.__aenter__.return_value = upstream
        upstream.__aexit__.return_value = None

        @contextlib.asynccontextmanager
        async def fake_http_client(url, headers=None, **kw):
            self.http_calls.append({"url": url, "headers": headers or {}})
            yield (mock.MagicMock(), mock.MagicMock(), mock.MagicMock())

        return upstream, fake_http_client

    @contextlib.asynccontextmanager
    async def _drive(self, upstream, http_factory):
        """_serve を memory stream 越しに動かし、client 側 session を yield"""
        import anyio

        # client 側は本物の ClientSession を使う。_serve 内の
        # `from mcp import ClientSession` だけを偽物に差し替えるため、
        # パッケージ属性（mcp.ClientSession）を patch し、こちらは
        # mcp.client.session 直下から取る
        from mcp.client.session import ClientSession

        c2s_send, c2s_recv = anyio.create_memory_object_stream(0)
        s2c_send, s2c_recv = anyio.create_memory_object_stream(0)

        @contextlib.asynccontextmanager
        async def fake_stdio():
            # Server 側から見た (read=c2s_recv, write=s2c_send)
            yield c2s_recv, s2c_send

        patches = [
            mock.patch(
                "mcp.client.streamable_http.streamablehttp_client",
                http_factory),
            mock.patch("mcp.server.stdio.stdio_server", fake_stdio),
            mock.patch("mcp.ClientSession",
                       mock.MagicMock(return_value=upstream)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        serve_task = asyncio.ensure_future(self.mod._serve())
        self.addCleanup(serve_task.cancel)
        try:
            async with ClientSession(s2c_recv, c2s_send) as client:
                await client.initialize()
                yield client
        finally:
            await c2s_send.aclose()
            await s2c_send.aclose()
            try:
                await asyncio.wait_for(serve_task, timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    async def test_bearer_and_initialize(self):
        upstream, factory = self._fake_upstream()
        async with self._drive(upstream, factory) as client:
            tools = (await client.list_tools()).tools
            self.assertEqual([t.name for t in tools], ["core_context"])
        upstream.list_tools.assert_awaited_once()
        self.assertEqual(len(self.http_calls), 1)
        auth = self.http_calls[0]["headers"].get("Authorization", "")
        self.assertEqual(auth, "Bearer mcp-key-x")

    async def test_call_tool_forwards_name_and_args(self):
        upstream, factory = self._fake_upstream()
        async with self._drive(upstream, factory) as client:
            res = await client.call_tool(
                "core_report",
                {"text": "終わった", "kind": "result", "ref": "r1"},
            )
            self.assertFalse(res.isError)
            self.assertEqual(res.content[0].text, "upstream result")
        upstream.call_tool.assert_awaited_once()
        args, kw = upstream.call_tool.call_args
        self.assertEqual(args[0], "core_report")
        self.assertEqual(
            args[1], {"text": "終わった", "kind": "result", "ref": "r1"})

    async def test_upstream_tool_error_becomes_jsonrpc_error(self):
        import mcp.types as mt
        from mcp.shared.exceptions import McpError

        err = McpError(mt.ErrorData(code=-32000, message="body is down"))
        upstream, factory = self._fake_upstream(call_error=err)
        async with self._drive(upstream, factory) as client:
            with self.assertRaises(McpError):
                await client.call_tool("core_report", {"text": "x"})

    async def test_key_missing_returns_2(self):
        """鍵が解決できなければ upstream に触れず終了コード2"""
        self.cfg.COMPANION_MCP_KEY = ""
        rc = await self.mod._serve()
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
