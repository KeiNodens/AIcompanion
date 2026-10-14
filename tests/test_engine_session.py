"""engine.py の同一セッション直列化・reset・記憶フック・独白分離のテスト"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support


class EngineSessionTest(unittest.IsolatedAsyncioTestCase):
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
        self.actor_mod = self._iso.import_module("companion.tools.actor")

    async def _drain_tasks(self):
        await self.engine.drain_background(timeout=2)

    async def test_generate_pushes_user_and_assistant_turn(self):
        self.llm_mod.fake.queue.append(_support.make_resp("こんにちは！"))
        res = await self.engine.generate("s1", "やあ")
        self.assertEqual(res["reply"], "こんにちは！")
        hist = list(self.engine._recent["s1"])
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        self.assertEqual(hist[0]["content"], "やあ")
        self.assertEqual(hist[1]["content"], "こんにちは！")

    async def test_same_session_generates_serialize(self):
        """先に着いた生成が終わるまで、同じセッションの後続は履歴を確定しない"""
        fake = self.llm_mod.fake
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def first_call(kw):
            entered.set()
            await gate.wait()
            return _support.make_resp("返信1")

        fake.queue.extend([first_call, _support.make_resp("返信2")])
        t1 = asyncio.create_task(self.engine.generate("s", "質問1"))
        await entered.wait()
        t2 = asyncio.create_task(self.engine.generate("s", "質問2"))
        await asyncio.sleep(0.02)
        gate.set()
        r1, r2 = await asyncio.gather(t1, t2)
        self.assertEqual(r1["reply"], "返信1")
        self.assertEqual(r2["reply"], "返信2")
        hist = [m["content"] for m in self.engine._recent["s"]]
        self.assertEqual(hist, ["質問1", "返信1", "質問2", "返信2"])
        # 2回目のプロンプトには1往復目の履歴が見えている
        contents = [m.get("content") for m in fake.calls[1]["messages"]]
        self.assertIn("質問1", contents)
        self.assertIn("返信1", contents)

    async def test_other_session_not_blocked(self):
        fake = self.llm_mod.fake
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def first_call(kw):
            entered.set()
            await gate.wait()
            return _support.make_resp("返信1")

        fake.queue.extend([first_call, _support.make_resp("返信2")])
        t1 = asyncio.create_task(self.engine.generate("s1", "質問1"))
        await entered.wait()
        r2 = await asyncio.wait_for(self.engine.generate("s2", "質問2"), timeout=5)
        self.assertEqual(r2["reply"], "返信2")
        gate.set()
        await t1

    async def test_reset_session_waits_for_inflight_then_clears(self):
        fake = self.llm_mod.fake
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def first_call(kw):
            entered.set()
            await gate.wait()
            return _support.make_resp("返信1")

        fake.queue.append(first_call)
        t1 = asyncio.create_task(self.engine.generate("s", "質問1"))
        await entered.wait()
        reset_done = asyncio.Event()

        async def do_reset():
            await self.engine.reset_session("s")
            reset_done.set()

        t2 = asyncio.create_task(do_reset())
        await asyncio.sleep(0.05)
        # 生成中の会話がある間はresetは完了しない（古い履歴での上書きを防ぐ）
        self.assertFalse(reset_done.is_set())
        gate.set()
        await asyncio.gather(t1, t2)
        self.assertTrue(reset_done.is_set())
        self.assertEqual(list(self.engine._recent.get("s", [])), [])

    async def test_memory_and_episode_hooks_receive_snapshot(self):
        self.llm_mod.fake.queue.append(_support.make_resp("返信"))
        await self.engine.generate("s", "質問")
        await self._drain_tasks()
        self.assertEqual(self.mem.calls["store_turn"], [("質問", "返信")])
        self.assertEqual(len(self.refl.calls), 1)
        self.assertEqual(self.refl.calls[0]["session"], "s")
        # 往復は turns テーブルに永続化されている
        db = self._iso.import_module("companion.db")
        rows = db.turns_pending_episode()
        self.assertEqual((rows[0][2], rows[0][3]), ("質問", "返信"))

    async def test_inner_monologue_split(self):
        self.llm_mod.fake.queue.append(
            _support.make_resp("<inner>ドキドキ</inner><reply>うん！</reply>")
        )
        res = await self.engine.generate("s", "質問")
        self.assertEqual(res["inner"], "ドキドキ")
        self.assertEqual(res["reply"], "うん！")

    async def test_inner_leak_fails_closed(self):
        """未閉鎖の <inner> は独白漏洩を防ぐためフォールバック文だけを返す"""
        self.llm_mod.fake.queue.append(_support.make_resp("<inner>秘密の独白"))
        res = await self.engine.generate("s", "質問")
        self.assertNotIn("秘密", res["reply"])

    async def test_think_block_stripped_from_reply(self):
        """推論タグは最終 reply・永続化 turns の両方から剥がす"""
        self.llm_mod.fake.queue.append(
            _support.make_resp("<think>深く考えた</think>答えだよ")
        )
        res = await self.engine.generate("s", "質問")
        self.assertEqual(res["reply"], "答えだよ")
        db = self._iso.import_module("companion.db")
        row = db.turns_pending_episode()[0]
        self.assertNotIn("深く考えた", row[3])   # assistant_text

    async def test_unclosed_think_fails_closed(self):
        """未閉鎖の <think> は以降を全部落とす（途中で切れた推論漏洩防止）"""
        self.llm_mod.fake.queue.append(
            _support.make_resp("前置き<think>延々と考える")
        )
        res = await self.engine.generate("s", "質問")
        self.assertEqual(res["reply"], "前置き")
        # 全体が think だけならフォールバック文に倒れる
        self.llm_mod.fake.queue.append(
            _support.make_resp("<think>だけの推論")
        )
        res2 = await self.engine.generate("s2", "質問")
        self.assertNotIn("だけの推論", res2["reply"])

    async def test_think_stripped_when_inner_monologue_off(self):
        """INNER_MONOLOGUE=0 でも <think> は剥がす"""
        with mock.patch.object(self.engine, "INNER_MONOLOGUE", False):
            self.llm_mod.fake.queue.append(
                _support.make_resp("<think>r</think>見える返信")
            )
            res = await self.engine.generate("s", "質問")
        self.assertEqual(res["reply"], "見える返信")

    async def test_notify_channel_contextvar_set_during_tool_loop(self):
        """generate の notify_channel がツール実行中に CURRENT_CHANNEL へ伝わる"""
        seen = []

        async def dispatch(name, args):
            seen.append(self.actor_mod.CURRENT_CHANNEL.get())
            return "ok"

        self.tpkg.hook["fn"] = dispatch
        tc = _support.mock.MagicMock()
        tc.id = "call1"
        tc.function.name = "remind_at"
        tc.function.arguments = '{"when": "2030-01-01 10:00", "text": "x"}'
        self.llm_mod.fake.queue.extend(
            [_support.make_resp(tool_calls=[tc]), _support.make_resp("done")]
        )
        await self.engine.generate("s", "リマインドして", notify_channel=4321)
        self.assertEqual(seen, [4321])

    async def test_session_contextvar_set_during_tool_loop(self):
        """ツール実行中に CURRENT_SESSION が会話セッションキーになる"""
        seen = []

        async def dispatch(name, args):
            seen.append(self.actor_mod.CURRENT_SESSION.get())
            return "ok"

        self.tpkg.hook["fn"] = dispatch
        tc = _support.mock.MagicMock()
        tc.id = "call1"
        tc.function.name = "remind_at"
        tc.function.arguments = '{}'
        self.llm_mod.fake.queue.extend(
            [_support.make_resp(tool_calls=[tc]), _support.make_resp("done")]
        )
        await self.engine.generate("sess-x", "やって")
        self.assertEqual(seen, ["sess-x"])
        # ツールループを抜けたら元に戻る
        self.assertIsNone(self.actor_mod.CURRENT_SESSION.get())

    async def test_tool_calls_recorded_to_turn_tool_calls(self):
        """engine のツール呼出しが turn_tool_calls に origin='core' で残る"""
        db = self._iso.import_module("companion.db")
        tc = _support.mock.MagicMock()
        tc.id = "call42"
        tc.function.name = "tasks_list"
        tc.function.arguments = '{"include_done": true}'
        self.tpkg.hook["fn"] = None   # 既定の "stub:<name>" 応答
        self.llm_mod.fake.queue.extend(
            [_support.make_resp(tool_calls=[tc]), _support.make_resp("done")]
        )
        await self.engine.generate("s", "一覧して")
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT turn_id, session_key, origin, ref, seq, tool,"
                " args_json, status, result_excerpt"
                " FROM turn_tool_calls WHERE origin='core'"
            ).fetchall()
            tid = conn.execute(
                "SELECT id FROM turns ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(len(rows), 1)
        (turn_id, session_key, origin, ref, seq, tool,
         args_json, status, excerpt) = rows[0]
        self.assertEqual(turn_id, tid)
        self.assertEqual(session_key, "s")
        self.assertEqual(ref, "call42")
        self.assertEqual(seq, 0)
        self.assertEqual(tool, "tasks_list")
        self.assertEqual(status, "ok")
        self.assertIn("include_done", args_json)
        self.assertIn("stub:tasks_list", excerpt)


if __name__ == "__main__":
    unittest.main()
