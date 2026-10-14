"""tools/__init__.py の dispatch_tool 境界: ownerゲート・未知ツール・エラー分離"""
import unittest

import _support

# 実 __init__.py を読ませるため、重いサブモジュールはすべてスタブ化する
_TOOL_SUBMODULES = [
    "delegate",
    "docs",
    "inner",
    "lists",
    "media",
    "memory_ops",
    "modelctl",
    "music",
    "pc",
    "reminders",
    "tasks",
]


class DispatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stubs = {
            f"companion.tools.{n}": _support.module_stub(f"companion.tools.{n}")
            for n in _TOOL_SUBMODULES
        }
        # HANDLERS は remind_at だけ直接参照するため、属性が必要
        stubs["companion.tools.reminders"].remind_handler = lambda a: "ok"
        # actor.py は実ファイルを読ませる（is_owner が本物であることが大事）
        self._iso = _support.IsolatedModules(stubs=stubs, stub_tools=False)
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.actor = self._iso.import_module("companion.tools.actor")
        self.tools = self._iso.import_module("companion.tools")
        self.actor.CURRENT_ACTOR.set(None)

    async def test_denies_non_owner(self):
        res = await self.tools.dispatch_tool("tasks_list", {})
        self.assertEqual(res, "それはマスター専用かな")

    async def test_owner_passes_gate(self):
        self.actor.CURRENT_ACTOR.set("desktop")
        self.tools.HANDLERS["echo_test"] = lambda a: "echo:" + a.get("x", "")
        res = await self.tools.dispatch_tool("echo_test", {"x": "hi"})
        self.assertEqual(res, "echo:hi")

    async def test_unknown_tool(self):
        self.actor.CURRENT_ACTOR.set("desktop")
        res = await self.tools.dispatch_tool("nonexistent_tool", {})
        self.assertIn("unknown", res)

    async def test_handler_exception_returns_error_string(self):
        self.actor.CURRENT_ACTOR.set("desktop")

        def boom(a):
            raise ValueError("nope")

        self.tools.HANDLERS["boom_test"] = boom
        res = await self.tools.dispatch_tool("boom_test", {})
        self.assertIn("エラー", res)

    # --- ステップ6B: ToolResult / TOOL_META / dispatch_tool_result ---

    async def test_result_status_unknown_and_ok(self):
        self.actor.CURRENT_ACTOR.set("desktop")
        res = await self.tools.dispatch_tool_result("nonexistent_tool", {})
        self.assertEqual(res.status, "unknown")
        self.tools.HANDLERS["ok_test"] = lambda a: "fine"
        res = await self.tools.dispatch_tool_result("ok_test", {})
        self.assertEqual((res.status, res.text), ("ok", "fine"))

    async def test_toolresult_passes_through(self):
        self.actor.CURRENT_ACTOR.set("desktop")
        self.tools.HANDLERS["pending_test"] = lambda a: self.tools.ToolResult(
            "pending_approval", "承認待ち"
        )
        res = await self.tools.dispatch_tool_result("pending_test", {})
        self.assertEqual(res.status, "pending_approval")
        # 互換の文字列経路は text を返す
        res2 = await self.tools.dispatch_tool("pending_test", {})
        self.assertEqual(res2, "承認待ち")

    async def test_timeout_returns_error(self):
        import asyncio as _a

        self.actor.CURRENT_ACTOR.set("desktop")

        def slow(a):
            import time
            time.sleep(0.3)
            return "done"

        self.tools.HANDLERS["slow_test"] = slow
        self.tools.TOOL_META["slow_test"] = {"timeout": 0.05}
        res = await self.tools.dispatch_tool_result("slow_test", {})
        self.assertEqual(res.status, "error")
        self.assertIn("時間内", res.text)

    async def test_deny_status_is_error(self):
        res = await self.tools.dispatch_tool_result("tasks_list", {})
        self.assertEqual(res.status, "error")

    def test_schema_handler_pairs_complete(self):
        """TOOLSのスキーマ名とHANDLERSは1:1"""
        names = {t["function"]["name"] for t in self.tools.TOOLS}
        self.assertEqual(names, set(self.tools.HANDLERS))


if __name__ == "__main__":
    unittest.main()
