"""bridge.py（形式橋 §3）— build_schemas / Suspension / engine の
body__* 呼出し分岐のテスト。ネットワークは使わない"""
import asyncio
import unittest
from unittest import mock

import _support


def _tool(name, desc="d", params=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": params or {"type": "object"},
        },
    }


class BuildSchemasTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.bridge = self._iso.import_module("companion.bridge")

    def test_glob_match_and_prefix(self):
        schemas, names = self.bridge.build_schemas(
            [_tool("web_search"), _tool("terminal"), _tool("browser_open")],
            ["web_*", "browser_*"],
        )
        self.assertEqual(
            [s["function"]["name"] for s in schemas],
            ["body__browser_open", "body__web_search"],
        )
        self.assertEqual(names["body__web_search"], "web_search")
        self.assertEqual(names["body__browser_open"], "browser_open")

    def test_description_prefixed_and_params_passthrough(self):
        params = {"type": "object", "properties": {"q": {"type": "string"}}}
        schemas, _ = self.bridge.build_schemas(
            [_tool("web_search", desc="検索する", params=params)], ["web_*"]
        )
        fn = schemas[0]["function"]
        self.assertEqual(fn["description"], "【身体】検索する")
        self.assertEqual(fn["parameters"], params)

    def test_mcp_companion_excluded(self):
        """自分の /mcp 面（mcp__companion__*）は橋に載せない（ループ防止）"""
        schemas, names = self.bridge.build_schemas(
            [_tool("mcp__companion__core_report"),
             _tool("mcp_companion_chat"),
             _tool("web_search")], ["*"]
        )
        self.assertEqual([s["function"]["name"] for s in schemas],
                         ["body__web_search"])
        self.assertNotIn("body__mcp__companion__core_report", names)
        self.assertNotIn("body__mcp_companion_chat", names)

    def test_non_function_type_skipped(self):
        schemas, _ = self.bridge.build_schemas(
            [{"type": "custom", "function": {"name": "web_search"}}], ["*"]
        )
        self.assertEqual(schemas, [])

    def test_over_64_char_name_dropped(self):
        long_name = "x" * 60   # body__ + 60 = 66 > 64
        schemas, names = self.bridge.build_schemas([_tool(long_name)], ["*"])
        self.assertEqual(schemas, [])
        self.assertEqual(names, {})

    def test_sorted_by_shown_name(self):
        schemas, _ = self.bridge.build_schemas(
            [_tool("z_tool"), _tool("a_tool"), _tool("m_tool")], ["*"]
        )
        self.assertEqual(
            [s["function"]["name"] for s in schemas],
            ["body__a_tool", "body__m_tool", "body__z_tool"],
        )


class SuspensionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.bridge = self._iso.import_module("companion.bridge")

    async def test_emit_queues_calls_and_waits_results(self):
        susp = self.bridge.Suspension("s")
        calls = [{"id": "c1", "name": "web_search", "arguments": "{}"},
                 {"id": "c2", "name": "web_extract", "arguments": "{}"}]

        async def engine_side():
            return await susp.emit(calls)

        t = asyncio.create_task(engine_side())
        kind, got = await asyncio.wait_for(susp.events.get(), 1)
        self.assertEqual(kind, "calls")
        self.assertEqual(got, calls)
        self.assertEqual(susp.order,
                         [("c1", "web_search"), ("c2", "web_extract")])
        susp.pending["c1"].set_result("r1")
        susp.pending["c2"].set_result("r2")
        self.assertEqual(await asyncio.wait_for(t, 1), ["r1", "r2"])

    async def test_emit_replaces_pending_each_round(self):
        susp = self.bridge.Suspension("s")

        async def one(cid):
            t = asyncio.create_task(
                susp.emit([{"id": cid, "name": "web_search",
                            "arguments": "{}"}]))
            await susp.events.get()
            susp.pending[cid].set_result("ok")
            await t

        await one("c1")
        self.assertEqual(list(susp.pending), ["c1"])
        await one("c9")
        self.assertEqual(list(susp.pending), ["c9"])

    async def test_abort_resolves_pending_with_bridge_aborted(self):
        susp = self.bridge.Suspension("s")

        async def engine_side():
            return await susp.emit(
                [{"id": "c1", "name": "web_search", "arguments": "{}"}])

        t = asyncio.create_task(engine_side())
        await susp.events.get()
        susp.abort()
        with self.assertRaises(self.bridge.BridgeAborted):
            await asyncio.wait_for(t, 1)
        self.assertTrue(susp.aborted)

    async def test_done_callback_pushes_done_and_error(self):
        susp = self.bridge.Suspension("s")
        susp.task = asyncio.create_task(asyncio.sleep(0, result={"x": 1}))
        susp.task.add_done_callback(susp.on_task_done)
        kind, payload = await asyncio.wait_for(susp.events.get(), 1)
        self.assertEqual((kind, payload), ("done", {"x": 1}))

        susp2 = self.bridge.Suspension("s2")

        async def boom():
            raise ValueError("nope")

        susp2.task = asyncio.create_task(boom())
        susp2.task.add_done_callback(susp2.on_task_done)
        kind, payload = await asyncio.wait_for(susp2.events.get(), 1)
        self.assertEqual(kind, "error")
        self.assertIsInstance(payload, ValueError)

    async def test_done_callback_drops_aborted_from_map(self):
        """abort 済みの保留はタスク完了時に _suspended から外れる"""
        susp = self.bridge.Suspension("s")
        self.bridge._suspended["s"] = susp
        susp.abort()
        susp.task = asyncio.create_task(asyncio.sleep(0, result="ok"))
        susp.task.add_done_callback(susp.on_task_done)
        await susp.events.get()
        self.assertNotIn("s", self.bridge._suspended)


class EngineBridgeTest(unittest.IsolatedAsyncioTestCase):
    """engine + BodyBridge: 内部/外部の分岐・emit・trace・中断・ラウンド上限"""

    def setUp(self):
        self.mem = _support.memory_stub()
        self.llm_mod = _support.llm_stub()
        self.refl = _support.reflect_stub()
        self.tpkg = _support.tools_pkg_stub()
        self._iso = _support.IsolatedModules(
            stubs={
                "companion.memory": self.mem,
                "companion.llm": self.llm_mod,
                "companion.reflect": self.refl,
                "companion.tools": self.tpkg,
            }
        )
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.engine = self._iso.import_module("companion.engine")
        self.bridge = self._iso.import_module("companion.bridge")
        self.db = self._iso.import_module("companion.db")

    def _tc(self, name, call_id, arguments="{}"):
        tc = mock.MagicMock()
        tc.id = call_id
        tc.function.name = name
        tc.function.arguments = arguments
        return tc

    def _bridge(self, emit, tools=None):
        schemas, names = self.bridge.build_schemas(
            tools if tools is not None
            else [_tool("web_search"), _tool("browser_open")],
            ["web_search", "browser_open"],
        )
        return self.bridge.BodyBridge(schemas=schemas, names=names, emit=emit)

    async def test_body_tool_emits_hermes_name_and_records(self):
        emitted = []

        async def emit(calls):
            emitted.append(calls)
            return ["検索結果だよ"]

        tc = self._tc("body__web_search", "c1", '{"q": "天気"}')
        self.llm_mod.fake.queue.extend(
            [_support.make_resp(tool_calls=[tc]),
             _support.make_resp("調べたよ")])
        res = await self.engine.generate(
            "s", "天気調べて", bridge=self._bridge(emit))
        self.assertEqual(res["reply"], "調べたよ")
        self.assertFalse(res["aborted"])
        # emit には Hermes 元名と元の JSON 文字列が渡る
        self.assertEqual(
            emitted,
            [[{"id": "c1", "name": "web_search",
               "arguments": '{"q": "天気"}'}]],
        )
        # 次ラウンドには tool メッセージが積まれている
        msgs = self.llm_mod.fake.calls[1]["messages"]
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
        self.assertEqual(tool_msgs[0]["content"], "検索結果だよ")
        # turn と tool trace は正本に残る（origin=body_bridge, Hermes 元名）
        with self.db.get_db() as conn:
            row = conn.execute(
                "SELECT origin, tool, ref, status, result_excerpt"
                " FROM turn_tool_calls"
            ).fetchone()
            turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        self.assertEqual((row[0], row[1], row[2], row[3]),
                         ("body_bridge", "web_search", "c1", "ok"))
        self.assertIn("検索結果", row[4])
        self.assertEqual(turns, 1)

    async def test_mixed_round_internal_first_then_emit(self):
        """内部と外部が混在するラウンド: 内部が先に走り、
        tool メッセージは元の tool_calls 順で積まれる"""
        order = []
        emitted = []

        def dispatch(name, args):
            order.append(f"internal:{name}")
            return "内部結果"

        async def emit(calls):
            order.append("external")
            emitted.append(calls)
            return ["外部結果"]

        self.tpkg.hook["fn"] = dispatch
        tc_ext = self._tc("body__web_search", "c1")
        tc_int = self._tc("tasks_list", "c0")
        # 外部が先に書かれていても内部を先に実行する
        self.llm_mod.fake.queue.extend(
            [_support.make_resp(tool_calls=[tc_ext, tc_int]),
             _support.make_resp("done")])
        await self.engine.generate("s", "やって", bridge=self._bridge(emit))
        self.assertEqual(order, ["internal:tasks_list", "external"])
        msgs = self.llm_mod.fake.calls[1]["messages"]
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs],
                         ["c1", "c0"])
        self.assertEqual([m["content"] for m in tool_msgs],
                         ["外部結果", "内部結果"])

    async def test_bridge_schemas_added_to_tools(self):
        captured = []

        async def create(kw):
            captured.append(kw)
            return _support.make_resp("ok")

        self.llm_mod.fake.queue.append(create)
        await self.engine.generate("s", "x", bridge=self._bridge(lambda c: []))
        names = [t["function"]["name"] for t in captured[0]["tools"]]
        self.assertIn("body__web_search", names)
        self.assertIn("body__browser_open", names)

    async def test_abort_writes_interrupted_reply_and_keeps_records(self):
        async def emit(calls):
            raise self.bridge.BridgeAborted()

        tc = self._tc("body__web_search", "c1")
        self.llm_mod.fake.queue.append(_support.make_resp(tool_calls=[tc]))
        res = await self.engine.generate(
            "s", "調べて", bridge=self._bridge(emit))
        self.assertTrue(res["aborted"])
        self.assertIn("中断", res["reply"])
        with self.db.get_db() as conn:
            row = conn.execute(
                "SELECT status, origin, result_excerpt"
                " FROM turn_tool_calls").fetchone()
            turns = conn.execute(
                "SELECT COUNT(*) FROM turns").fetchone()[0]
        self.assertEqual(row[0], "aborted")
        self.assertEqual(row[1], "body_bridge")
        # 中断でも往復・ツール記録は残る
        self.assertEqual(turns, 1)

    async def test_external_rounds_can_exceed_max_tool_rounds(self):
        """外部呼出しを含むラウンドは MAX_TOOL_ROUNDS(5) を超えて続く"""
        n = {"i": 0}

        async def emit(calls):
            n["i"] += 1
            return [f"res{n['i']}"]

        for i in range(7):
            self.llm_mod.fake.queue.append(_support.make_resp(
                tool_calls=[self._tc("body__web_search", f"c{i}")]))
        self.llm_mod.fake.queue.append(_support.make_resp("終わり"))
        res = await self.engine.generate("s", "x", bridge=self._bridge(emit))
        self.assertEqual(res["reply"], "終わり")
        self.assertEqual(n["i"], 7)

    async def test_inner_only_rounds_stop_at_max(self):
        """外部を含まないラウンドだけなら MAX_TOOL_ROUNDS(5) で打ち切る"""
        for i in range(8):
            self.llm_mod.fake.queue.append(_support.make_resp(
                tool_calls=[self._tc("tasks_list", f"c{i}")]))
        res = await self.engine.generate(
            "s", "x", bridge=self._bridge(lambda c: asyncio.sleep(0)))
        self.assertIn("多すぎ", res["reply"])
        self.assertEqual(len(self.llm_mod.fake.calls), 5)


if __name__ == "__main__":
    unittest.main()
