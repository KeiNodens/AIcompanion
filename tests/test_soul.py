"""soul.py — persona/persona_notes の SOUL.md 写し配布のテスト"""
import unittest

import _support


class SoulTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.soul = self._iso.import_module("companion.soul")

    @property
    def cfg(self):
        return self._iso.cfg

    def _make_hermes_home(self):
        self.cfg.HERMES_HOME.mkdir(parents=True, exist_ok=True)

    def test_render_combines_persona_and_notes(self):
        self.cfg.PERSONA_PATH.write_text("人格の核", encoding="utf-8")
        self.cfg.PERSONA_NOTES_PATH.write_text("- 自分メモ", encoding="utf-8")
        text = self.soul.render_soul()
        self.assertIn(self.soul.MARKER, text)
        self.assertIn("人格の核", text)
        self.assertIn("自分メモ", text)

    def test_render_works_without_sources(self):
        text = self.soul.render_soul()
        self.assertIn(self.soul.MARKER, text)

    def test_sync_skips_without_hermes_home(self):
        # HERMES_HOME 自体がない環境では何もしない
        self.assertEqual(self.soul.sync_soul(), "skipped")
        self.assertFalse(self.cfg.SOUL_PATH.exists())

    def test_sync_writes_generated_soul(self):
        self._make_hermes_home()
        self.cfg.PERSONA_PATH.write_text("人格の核", encoding="utf-8")
        self.assertEqual(self.soul.sync_soul(), "updated")
        body = self.cfg.SOUL_PATH.read_text(encoding="utf-8")
        self.assertIn(self.soul.MARKER, body)
        self.assertIn("人格の核", body)
        # 2回目は差分なし
        self.assertEqual(self.soul.sync_soul(), "noop")

    def test_sync_backs_up_foreign_soul(self):
        self._make_hermes_home()
        self.cfg.SOUL_PATH.write_text("Hermes既定のSOUL", encoding="utf-8")
        self.assertEqual(self.soul.sync_soul(), "updated")
        baks = list(self.cfg.HERMES_HOME.glob("SOUL.md.bak-*"))
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_text(encoding="utf-8"), "Hermes既定のSOUL")
        self.assertIn(self.soul.MARKER,
                      self.cfg.SOUL_PATH.read_text(encoding="utf-8"))

    def test_sync_reflects_persona_edits(self):
        self._make_hermes_home()
        self.cfg.PERSONA_PATH.write_text("v1", encoding="utf-8")
        self.soul.sync_soul()
        self.cfg.PERSONA_PATH.write_text("v2", encoding="utf-8")
        self.assertEqual(self.soul.sync_soul(), "updated")
        self.assertIn("v2", self.cfg.SOUL_PATH.read_text(encoding="utf-8"))

    def test_sync_disabled(self):
        self._make_hermes_home()
        self.cfg.SOUL_SYNC = False
        # module は import 時に値を束縛するので再importが必要
        import importlib, sys
        sys.modules.pop("companion.soul", None)
        soul = self._iso.import_module("companion.soul")
        self.assertEqual(soul.sync_soul(), "skipped")
        self.assertFalse(self.cfg.SOUL_PATH.exists())


if __name__ == "__main__":
    unittest.main()
