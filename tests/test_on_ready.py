"""discord_bot.on_ready: 再接続で呼ばれても常駐タスクを二重起動しない"""
import asyncio
import unittest
from unittest import mock

import _support


class OnReadyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scheduler_calls = []
        self.serve_calls = []

        async def fake_scheduler(bot):
            self.scheduler_calls.append(1)

        async def fake_serve():
            self.serve_calls.append(1)

        stubs = {
            "companion.jobs": _support.module_stub(
                "companion.jobs", scheduler=fake_scheduler
            ),
            "companion.server": _support.module_stub(
                "companion.server", serve=fake_serve
            ),
            "companion.engine": _support.module_stub(
                "companion.engine",
                generate=mock.AsyncMock(return_value={"reply": ""}),
                reset_session=mock.AsyncMock(),
            ),
            "companion.memory": _support.memory_stub(),
            "companion.tools.docs": _support.module_stub(
                "companion.tools.docs", doc_ingest=lambda *a: "ok"
            ),
            "companion.tools.lists": _support.module_stub(
                "companion.tools.lists",
                list_names=lambda: "-",
                list_show=lambda n: "-",
            ),
            "companion.tools.obsidian": _support.module_stub(
                "companion.tools.obsidian",
                sync_obsidian=mock.AsyncMock(return_value="ok"),
            ),
            "companion.tools.reminders": _support.module_stub(
                "companion.tools.reminders", reminders_list=lambda: "-"
            ),
            "companion.tools.tasks": _support.module_stub(
                "companion.tools.tasks", tasks_list=lambda: "-"
            ),
        }
        self._iso = _support.IsolatedModules(stubs=stubs)
        self._iso.__enter__()
        self.addCleanup(self._iso.close)

    async def test_on_ready_starts_runtime_only_once(self):
        bot_mod = self._iso.import_module("companion.discord_bot")
        await bot_mod.on_ready()
        await asyncio.sleep(0)
        await bot_mod.on_ready()
        await asyncio.sleep(0)
        self.assertEqual(len(self.scheduler_calls), 1)
        self.assertEqual(len(self.serve_calls), 1)


if __name__ == "__main__":
    unittest.main()
