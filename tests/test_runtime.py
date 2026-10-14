"""ステップ6A: runtime による常駐処理の起動・終了管理。

- ensure_started は二重呼び出しで重複起動しない
- shutdown は常駐タスクをキャンセルして再起動可能にする
"""
import asyncio
import unittest
from unittest import mock

import _support


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sched_calls = []
        self.serve_calls = []

        async def fake_scheduler(bot):
            self.sched_calls.append(bot)
            await asyncio.Event().wait()

        async def fake_serve():
            self.serve_calls.append(1)
            await asyncio.Event().wait()

        self.jobs_stub = _support.module_stub(
            "companion.jobs", scheduler=fake_scheduler
        )
        self.server_stub = _support.module_stub(
            "companion.server", serve=fake_serve
        )
        self._iso = _support.IsolatedModules(
            stubs={
                "companion.jobs": self.jobs_stub,
                "companion.server": self.server_stub,
            }
        )
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.rt = self._iso.import_module("companion.runtime")

    async def test_ensure_started_is_idempotent(self):
        bot = mock.MagicMock()
        await self.rt.ensure_started(bot)
        await asyncio.sleep(0)
        self.assertEqual(self.sched_calls, [bot])
        self.assertEqual(self.serve_calls, [1])
        # 再呼び出し（再接続相当）では増えない
        await self.rt.ensure_started(bot)
        await asyncio.sleep(0)
        self.assertEqual(self.sched_calls, [bot])
        self.assertEqual(self.serve_calls, [1])
        await self.rt.shutdown()

    async def test_shutdown_cancels_and_allows_restart(self):
        bot = mock.MagicMock()
        await self.rt.ensure_started(bot)
        await asyncio.sleep(0)
        await self.rt.shutdown(timeout=1.0)
        # タスクは全て止まり、再度起動できる
        await self.rt.ensure_started(bot)
        await asyncio.sleep(0)
        self.assertEqual(len(self.sched_calls), 2)
        await self.rt.shutdown()

    async def test_missing_api_import_is_tolerated(self):
        # server の import 失敗（fastapi未導入環境）でもschedulerは起きる
        def _raise():
            raise ImportError("no fastapi")

        self.server_stub.serve = _raise
        bot = mock.MagicMock()
        await self.rt.ensure_started(bot)
        await asyncio.sleep(0)
        self.assertEqual(self.sched_calls, [bot])
        await self.rt.shutdown()


if __name__ == "__main__":
    unittest.main()
