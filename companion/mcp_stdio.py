"""stdio ↔ /mcp 中継 shim（research/design/codex-body.md、Phase 1）。

Codex（CLI/TUI/Desktop app）の mcp_servers に stdio server として登録し、
streamable HTTP の /mcp 面（mcp_server.py）へ JSON-RPC を中継する薄い
プロキシ。

- 認証は Codex 側の環境変数に頼らない。COMPANION_MCP_KEY は
  hermes_conf.mcp_key() が自前で解決（env → Hermes config → Hermes .env）
  し、Authorization: Bearer で付ける
- 実処理は必ず :8788 の bot プロセス内で行われる（PLUGIN-ROADMAP の注意:
  companion.memory 等を別プロセスで直 import すると Qdrant ロックが
  衝突するため、ここは HTTP 中継だけに留める）
- 鍵が解決できない・companion 未起動では initialize 前に落ちる。
  Codex 側には server 起動失敗として見える（通常の Codex 利用は止まらない）

登録例:
    codex mcp add companion -- ".venv/Scripts/python.exe" ^
        -m companion.mcp_stdio
（-m で動かすので cwd が repo 外なら PYTHONPATH に repo root が要る。
plugin 側の .mcp.json では env で供給する）
"""
import asyncio
import logging
import os
import sys
from datetime import timedelta

log = logging.getLogger("companion.mcp_stdio")

_DEFAULT_URL = "http://127.0.0.1:{port}/mcp"

# 中継対象の request 型 → upstream ClientSession の呼び出し。
# companion /mcp は tools しか出さないが、Codex が initialize 後に
# prompts/resources も聞いてくる場合に備えて空フォールバックを付ける
_TOOL_TIMEOUT = 120.0  # core_report は generate を通すので猶予を取る


def _upstream_url() -> str:
    url = (os.getenv("COMPANION_MCP_URL") or "").strip()
    if url:
        return url.rstrip("/")
    from . import config  # 遅延（env 解決が走る）

    return _DEFAULT_URL.format(port=config.API_PORT)


async def _serve() -> int:
    from mcp import ClientSession, types
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    from . import hermes_conf  # 遅延import（起動コストと失敗切り分け）

    url = _upstream_url()
    key, _src = hermes_conf.mcp_key()
    if not key:
        print(
            "companion.mcp_stdio: COMPANION_MCP_KEY が解決できない"
            "（env・Hermes config・Hermes .env のいずれにも無い）",
            file=sys.stderr,
        )
        return 2

    server: Server = Server("companion-stdio-bridge")

    try:
        async with streamablehttp_client(
            url, headers={"Authorization": f"Bearer {key}"}
        ) as (read, write, _get_sid):
            async with ClientSession(read, write) as upstream:
                await upstream.initialize()

                async def _call(req):
                    # 上流の MCP エラー・輸送断はそのまま投げて JSON-RPC
                    # error として Codex に返す（isError の tool 結果には
                    # 潰さない）
                    res = await upstream.call_tool(
                        req.params.name,
                        req.params.arguments or {},
                        read_timeout_seconds=timedelta(
                            seconds=_TOOL_TIMEOUT
                        ),
                    )
                    return types.ServerResult(res)

                async def _list(req):
                    res = await upstream.list_tools()
                    return types.ServerResult(res)

                async def _empty(_req, result_type):
                    return types.ServerResult(result_type())

                server.request_handlers[types.CallToolRequest] = _call
                server.request_handlers[types.ListToolsRequest] = _list
                server.request_handlers[types.ListPromptsRequest] = (
                    lambda req: _empty(req, types.ListPromptsResult)
                )
                server.request_handlers[types.ListResourcesRequest] = (
                    lambda req: _empty(req, types.ListResourcesResult)
                )
                server.request_handlers[
                    types.ListResourceTemplatesRequest
                ] = lambda req: _empty(req, types.ListResourceTemplatesResult)

                async with stdio_server() as (stdin, stdout):
                    await server.run(
                        stdin,
                        stdout,
                        server.create_initialization_options(),
                    )
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        # 未起動・鍵違い・404（/mcp 面 off）をここでまとめて切り分ける
        print(
            f"companion.mcp_stdio: {url} への接続に失敗: {e!r}",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(name)s %(levelname)s %(message)s",
    )
    try:
        return asyncio.run(_serve())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
