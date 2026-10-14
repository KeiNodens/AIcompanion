"""ステップ5A: 返答履歴と経験材料の分離。

- generate() が inner_text を turns 行へ別列で永続化する
- process_episode_material() が turns の未処理行から経験メモを作る
- LLM失敗（パース不能）では材料を残し、成功したら episode_done を確定する
- 正当な空 [] は「処理済み・記憶なし」として確定する（失敗と区別）
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

import _support


class EpisodeMaterialTest(unittest.IsolatedAsyncioTestCase):
    """実 reflect.py + 実 db + スタブ llm/memory で経験材料の流れを追う"""

    def setUp(self):
        self.mem = _support.memory_stub()
        self.llm_mod = _support.llm_stub()
        self._iso = _support.IsolatedModules(
            stubs={
                "companion.memory": self.mem,
                "companion.llm": self.llm_mod,
            }
        )
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.refl = self._iso.import_module("companion.reflect")

    async def test_episode_from_durable_turns(self):
        db = self.db
        db.record_turn("s", "今日は疲れた", "おつかれさま", ts="2026-01-01T20:00:00",
                       inner_text="心配だな")
        db.record_turn("s", "でも楽しかった", "よかったね", ts="2026-01-01T20:05:00")
        self.llm_mod.fake.queue.append(
            _support.make_resp('[{"text": "マスターが疲れているようで心配になった", "importance": 3}]')
        )
        await self.refl.process_episode_material()
        # 経験メモが保存され、材料行は確定
        self.assertEqual(len(self.mem.calls["add_self_note"]), 1)
        args, kw = self.mem.calls["add_self_note"][0]
        self.assertIn("心配", args[0])
        self.assertEqual(kw.get("importance"), 3)
        self.assertEqual(db.turns_pending_episode(), [])
        # LLMへのプロンプトに inner_text（心の中）が材料として含まれている
        prompt = self.llm_mod.fake.calls[0]["messages"][0]["content"]
        self.assertIn("心配だな", prompt)
        self.assertIn("心の中", prompt)

    async def test_llm_failure_keeps_material(self):
        db = self.db
        db.record_turn("s", "q", "a")
        self.llm_mod.fake.queue.append(_support.make_resp("壊れた応答{{{"))
        await self.refl.process_episode_material()
        self.assertEqual(len(self.mem.calls["add_self_note"]), 0)
        # 行は残っている → 次回再試行できる
        self.assertEqual(len(db.turns_pending_episode()), 1)
        self.llm_mod.fake.queue.append(
            _support.make_resp('[{"text": "楽しい会話だった", "importance": 2}]')
        )
        await self.refl.process_episode_material()
        self.assertEqual(len(self.mem.calls["add_self_note"]), 1)
        self.assertEqual(db.turns_pending_episode(), [])

    async def test_empty_result_is_done_not_failure(self):
        """[] は正当な空（記憶に残す価値なし）= 確定して再処理しない"""
        db = self.db
        db.record_turn("s", "うん", "うん")
        self.llm_mod.fake.queue.append(_support.make_resp("[]"))
        await self.refl.process_episode_material()
        self.assertEqual(db.turns_pending_episode(), [])
        self.assertEqual(len(self.mem.calls["add_self_note"]), 0)

    async def test_no_duplicate_after_done(self):
        db = self.db
        db.record_turn("s", "q", "a")
        self.llm_mod.fake.queue.append(
            _support.make_resp('[{"text": "経験", "importance": 2}]')
        )
        await self.refl.process_episode_material()
        n_calls = len(self.llm_mod.fake.calls)
        await self.refl.process_episode_material()
        # 処理済み行は再送しない（LLMを呼ばない）
        self.assertEqual(len(self.llm_mod.fake.calls), n_calls)

    async def test_add_self_note_failure_keeps_material(self):
        """保存自体の失敗でも行は残り、別の機会に回収できる"""
        db = self.db
        db.record_turn("s", "q", "a")
        self.mem.self_note_hook = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("mem0 down")
        )
        self.llm_mod.fake.queue.append(
            _support.make_resp('[{"text": "経験", "importance": 2}]')
        )
        await self.refl.process_episode_material()
        self.assertEqual(len(db.turns_pending_episode()), 1)


class InnerTextDurabilityTest(unittest.IsolatedAsyncioTestCase):
    """engine が inner_text を turns 行へ保存すること（見せる返信と分離）"""

    def setUp(self):
        self.mem = _support.memory_stub()
        self.llm_mod = _support.llm_stub()
        self.refl = _support.reflect_stub()
        self._iso = _support.IsolatedModules(
            stubs={
                "companion.memory": self.mem,
                "companion.llm": self.llm_mod,
                "companion.reflect": self.refl,
            }
        )
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.engine = self._iso.import_module("companion.engine")

    async def test_inner_text_persisted_with_turn(self):
        self.llm_mod.fake.queue.append(
            _support.make_resp("<inner>緊張するな</inner><reply>うん！</reply>")
        )
        res = await self.engine.generate("s", "話そう")
        self.assertEqual(res["reply"], "うん！")
        self.assertEqual(res["inner"], "緊張するな")
        rows = self.db.turns_pending_episode()
        self.assertEqual(len(rows), 1)
        # (id, session, user_text, assistant_text, inner_text, ts)
        self.assertEqual(rows[0][3], "うん！")
        self.assertEqual(rows[0][4], "緊張するな")

    async def test_no_inner_when_disabled(self):
        self.llm_mod.fake.queue.append(_support.make_resp("ただの返信"))
        await self.engine.generate("s", "質問")
        rows = self.db.turns_pending_episode()
        self.assertIsNone(rows[0][4])
        # 返信は inner タグを含まない
        self.assertEqual(rows[0][3], "ただの返信")


if __name__ == "__main__":
    unittest.main()
