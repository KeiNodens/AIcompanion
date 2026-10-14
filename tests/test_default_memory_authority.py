"""Public snapshot: canonical by default, explicit migration modes still work."""
import asyncio
import os
import sys
import unittest
from unittest import mock

import _support


class DefaultMemoryAuthorityTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop("MEMORY_AUTHORITY", None)
        self._iso = _support.IsolatedModules().__enter__()
        self.addCleanup(self._iso.close)
        self._iso.cfg.DEFAULT_MEMORY_AUTHORITY = "canonical"
        self._iso.cfg.CANONICAL_RECORD = False
        self.legacy_constructor = mock.Mock(
            side_effect=AssertionError("default mode must not construct mem0"))
        self._mem0_patch = mock.patch.dict(
            sys.modules,
            {"mem0": _support.module_stub("mem0", Memory=self.legacy_constructor)})
        self._mem0_patch.start()
        self.addCleanup(self._mem0_patch.stop)
        self.memory = self._iso.import_module("companion.memory")
        self.live = self._iso.import_module("companion.adapters.live")
        self.db = self._iso.import_module("companion.db")

    def test_fresh_state_uses_canonical_and_enables_recording(self):
        with mock.patch.object(self.db, "load_state", return_value={}):
            self.assertEqual(self.memory.authority(), "canonical")
            self.assertTrue(self.live._enabled())
        self.legacy_constructor.assert_not_called()

    def test_state_read_error_does_not_switch_to_mem0(self):
        with mock.patch.object(self.db, "load_state", side_effect=OSError("unreadable")):
            self.assertEqual(self.memory.authority(), "canonical")
            self.assertTrue(self.live._enabled())
        self.legacy_constructor.assert_not_called()

    def test_explicit_persisted_migration_modes_are_preserved(self):
        for mode, recording in (("mem0", False), ("dual_run", True),
                                ("canonical", True)):
            with self.subTest(mode=mode):
                with mock.patch.object(
                        self.db, "load_state",
                        return_value={"memory_authority": mode}):
                    self.assertEqual(self.memory.authority(), mode)
                    self.assertEqual(self.live._enabled(), recording)

    def test_environment_override_wins_over_persisted_state(self):
        with mock.patch.dict(os.environ, {"MEMORY_AUTHORITY": "mem0"}):
            with mock.patch.object(
                    self.db, "load_state",
                    return_value={"memory_authority": "canonical"}):
                self.assertEqual(self.memory.authority(), "mem0")
                self.assertFalse(self.live._enabled())

    def test_fresh_recall_uses_canonical_without_constructing_mem0(self):
        with mock.patch.object(self.db, "load_state", return_value={}):
            with mock.patch.object(
                    self.memory, "_canonical_recall",
                    return_value="canonical result") as recall:
                self.assertEqual(
                    asyncio.run(self.memory.recall("test query")), "canonical result")
                recall.assert_called_once()
        self.legacy_constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
