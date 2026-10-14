"""inner.py / modelctl.py / music.py の state.json 更新が他キーを壊さないことを確認"""
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import _support


class _StateToolsBase(unittest.TestCase):
    """tools サブモジュールを実ファイルから読むための共通セットアップ"""

    # modelctl は実 companion.llm（current_params が state.json を読む）を使う。
    # AsyncOpenAI クライアント生成のみで通信は発生しない。接続先は 127.0.0.1:1。
    extra_stubs: dict = {}

    def setUp(self):
        self._iso = _support.IsolatedModules(stubs=dict(self.extra_stubs))
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.actor = self._iso.import_module("companion.tools.actor")

    def set_actor(self, value):
        # ContextVar はテスト間で漏れるので毎回正規化する
        self.actor.CURRENT_ACTOR.set(value)


class InnerStateTest(_StateToolsBase):
    def setUp(self):
        super().setUp()
        self.inner = self._iso.import_module("companion.tools.inner")

    def test_set_mood_persists_without_clobbering_other_keys(self):
        self.db.save_state(last_channel_id=42, llm={"model": "x"})
        self.inner.set_mood("元気", valence=2, energy=90)
        state = self.db.load_state()
        self.assertEqual(state["inner"]["mood"], "元気")
        self.assertEqual(state["inner"]["valence"], 2)
        self.assertEqual(state["last_channel_id"], 42)
        self.assertEqual(state["llm"]["model"], "x")

    def test_get_inner_decays_toward_neutral(self):
        self.inner.set_mood("元気", valence=2, energy=90)
        real_dt = datetime

        class FakeDT:
            @classmethod
            def now(cls):
                return real_dt.now() + timedelta(hours=48)

            fromisoformat = staticmethod(real_dt.fromisoformat)

        with mock.patch.object(self.inner, "datetime", FakeDT):
            got = self.inner.get_inner()
        self.assertEqual(got["valence"], 0)
        self.assertEqual(got["energy"], 60)


class ModelCtlTest(_StateToolsBase):
    def setUp(self):
        super().setUp()
        self.modelctl = self._iso.import_module("companion.tools.modelctl")

    def test_model_set_denies_non_owner(self):
        res = self.modelctl.model_set("gpt-x", "low")
        self.assertIn("マスター", res)
        self.assertNotIn("llm", self.db.load_state())

    def test_model_set_and_reset_roundtrip(self):
        self.set_actor("desktop")
        self.db.save_state(inner={"mood": "普通"})
        res = self.modelctl.model_set("gpt-x", "low")
        self.assertIn("gpt-x", res)
        self.assertEqual(
            self.db.load_state()["llm"], {"model": "gpt-x", "effort": "low"}
        )
        res = self.modelctl.model_reset()
        self.assertIn("env既定", res)
        state = self.db.load_state()
        self.assertNotIn("llm", state)
        self.assertEqual(state["inner"]["mood"], "普通")

    def test_model_set_invalid_effort_rejected(self):
        self.set_actor("desktop")
        res = self.modelctl.model_set("gpt-x", "bogus")
        self.assertIn("effort", res)
        self.assertNotIn("llm", self.db.load_state())


class ConcurrentStateToolsTest(_StateToolsBase):
    """気分・モデル・音楽ルールの並行更新が互いのキーを消さない"""

    extra_stubs = {
        "companion.memory": _support.memory_stub(),
        "companion.tools.pc": _support.module_stub("companion.tools.pc"),
    }

    def setUp(self):
        super().setUp()
        self.inner = self._iso.import_module("companion.tools.inner")
        self.modelctl = self._iso.import_module("companion.tools.modelctl")
        self.music = self._iso.import_module("companion.tools.music")

    def test_parallel_writes_survive(self):
        # ContextVar は新規スレッドに伝播しないため、スレッド内でownerを名乗る
        barrier = threading.Barrier(3)
        errors = []

        def run(fn, *a):
            try:
                self.actor.CURRENT_ACTOR.set("desktop")
                barrier.wait(timeout=10)
                fn(*a)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [
            threading.Thread(target=run, args=(self.inner.set_mood, "集中")),
            threading.Thread(target=run, args=(self.modelctl.model_set, "gpt-x")),
            threading.Thread(
                target=run, args=(self.music.music_route, "add", "A-50", "apple")
            ),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        state = self.db.load_state()
        self.assertEqual(state["inner"]["mood"], "集中")
        self.assertEqual(state["llm"]["model"], "gpt-x")
        routes = state["music_routes"]
        self.assertEqual(routes, [{"match": "A-50", "backend": "apple"}])

    def test_music_route_add_remove(self):
        self.set_actor("desktop")
        res = self.music.music_route("add", "A-50", "apple")
        self.assertIn("ルール追加", res)
        self.assertEqual(
            self.db.load_state()["music_routes"],
            [{"match": "A-50", "backend": "apple"}],
        )
        res = self.music.music_route("remove", "A-50")
        self.assertIn("ルール削除", res)
        self.assertEqual(self.db.load_state().get("music_routes"), [])


if __name__ == "__main__":
    unittest.main()
