"""skill_export.py — §7.2 export 同期と §7.3 upstream 照合のテスト"""
import hashlib
import types
import unittest
from pathlib import Path
from unittest import mock

import _support


def _frontmatter(name: str, desc: str = "説明", requires: str = "") -> str:
    req = f"\nrequires_tools: {requires}" if requires else ""
    return f"---\nname: {name}\ndescription: {desc}{req}\n---\n"


def _pkg_md(name: str, body: str = "手順", desc: str = "説明",
            requires: str = "") -> str:
    return _frontmatter(name, desc, requires) + f"\n# {name}\n{body}\n"


class SkillExportTest(unittest.TestCase):
    """data/skills-export/ への写し・管理印・ずれ復元・改訂提案（§7.2）"""

    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.skills = self._iso.import_module("companion.skills")
        self.se = self._iso.import_module("companion.skill_export")

    @property
    def cfg(self):
        return self._iso.cfg

    def _make_active(self, name: str, body: str = "手順",
                     files: dict | None = None, origin: str = "imported"):
        pkg = self.cfg.SKILLS_DIR / name
        pkg.mkdir(parents=True, exist_ok=True)
        md = pkg / "SKILL.md"
        md.write_text(_pkg_md(name, body), encoding="utf-8")
        for rel, content in (files or {}).items():
            f = pkg / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
        chash = hashlib.sha256(md.read_bytes()).hexdigest()
        self.db.skill_stage(
            name, "説明", "", origin, chash, str(pkg), name
        )
        self.db.skill_set_status(name, "active", decided_by="master")
        return pkg

    def test_active_skill_is_exported_with_marker(self):
        self._make_active("cook", files={"references/ref.md": "参照"})
        staged = self.cfg.SKILLS_DIR / "not-yet"
        staged.mkdir()
        (staged / "SKILL.md").write_text(
            _pkg_md("not-yet", "下書き"), encoding="utf-8"
        )
        h = hashlib.sha256(
            (staged / "SKILL.md").read_bytes()
        ).hexdigest()
        self.db.skill_stage(
            "not-yet", "説明", "", "imported", h, str(staged), "not-yet"
        )
        res = self.se.sync_export()
        self.assertEqual(res["synced"], ["cook"])
        out = self.cfg.SKILLS_EXPORT_DIR / "cook"
        self.assertTrue((out / "SKILL.md").is_file())
        self.assertTrue((out / "references" / "ref.md").is_file())
        self.assertTrue((out / self.se._PACKAGE_MARKER).is_file())
        # ルート管理印
        self.assertTrue(
            (self.cfg.SKILLS_EXPORT_DIR / self.se._ROOT_MARKER).is_file()
        )
        # staged は export されない（active のみ）
        self.assertFalse((self.cfg.SKILLS_EXPORT_DIR / "not-yet").exists())

    def test_disabled_skill_export_is_removed(self):
        self._make_active("cook")
        self.se.sync_export()
        self.db.skill_set_status("cook", "disabled", decided_by="master")
        res = self.se.sync_export()
        self.assertIn("cook", res["removed"])
        self.assertFalse((self.cfg.SKILLS_EXPORT_DIR / "cook").exists())

    def test_foreign_dir_without_marker_is_kept(self):
        stray = self.cfg.SKILLS_EXPORT_DIR / "stray"
        stray.mkdir()
        (stray / "SKILL.md").write_text("よそ物", encoding="utf-8")
        res = self.se.sync_export()
        self.assertTrue(stray.is_dir())
        self.assertNotIn("stray", res["removed"])

    def test_drift_restores_canonical_and_proposes(self):
        pkg = self._make_active("cook")
        self.se.sync_export()
        # 身体側（external_dirs は書込保護ではない）で SKILL.md を書き換えた想定
        drifted_md = self.cfg.SKILLS_EXPORT_DIR / "cook" / "SKILL.md"
        drifted_md.write_text(
            _pkg_md("cook", "改善された手順"), encoding="utf-8"
        )
        res = self.se.sync_export()
        self.assertEqual(res["restored"], ["cook"])
        self.assertEqual(res["proposed"], ["cook"])
        # export は正本に戻る
        self.assertEqual(
            (self.cfg.SKILLS_EXPORT_DIR / "cook" / "SKILL.md").read_text(
                encoding="utf-8"
            ),
            (pkg / "SKILL.md").read_text(encoding="utf-8"),
        )
        # ずれた版は改訂提案として inbox に回る（管理印は除く）
        props = [
            p for p in self.cfg.SKILLS_INBOX_DIR.iterdir()
            if p.name.startswith("cook")
        ]
        self.assertEqual(len(props), 1)
        self.assertIn("export-proposal", props[0].name)
        self.assertIn(
            "改善された手順",
            (props[0] / "SKILL.md").read_text(encoding="utf-8"),
        )
        self.assertFalse((props[0] / self.se._PACKAGE_MARKER).exists())
        # 上書きで消える正本は superseded に退避される
        sup = list(
            (self.cfg.SKILLS_ARCHIVE_DIR / "superseded").glob("cook-*")
        )
        self.assertEqual(len(sup), 1)
        self.assertIn(
            "手順",
            (sup[0] / "SKILL.md").read_text(encoding="utf-8"),
        )
        # 提案は scan_inbox が restaged として拾う（staged→承認レーン）
        res2 = self.skills.scan_inbox()
        self.assertEqual(res2["restaged"], ["cook"])
        self.assertEqual(self.db.skill_get("cook")["status"], "staged")
        self.assertIsNotNone(res2["meta"]["cook"]["diff"])
        # 次の同期では staged なので写しは外れる（自動で再提案はしない）
        res3 = self.se.sync_export()
        self.assertIn("cook", res3["removed"])
        self.assertEqual(res3["proposed"], [])

    def test_no_drift_is_idempotent(self):
        self._make_active("cook")
        self.se.sync_export()
        res = self.se.sync_export()
        self.assertEqual(res["proposed"], [])
        self.assertEqual(res["restored"], [])
        self.assertEqual(res["removed"], [])


class SkillUpstreamTest(unittest.TestCase):
    """§7.3: Hermes /v1/skills との照合（upstream_gone 記録＋通知）"""

    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.cfg.NOTIFY_CHANNEL_ID = 42
        self.db = self._iso.import_module("companion.db")
        self.skills = self._iso.import_module("companion.skills")
        self.se = self._iso.import_module("companion.skill_export")
        # 承認直後の猶予でスキップされないよう grace を 0 に
        self.se.SKILL_UPSTREAM_GRACE_MIN = 0

    @property
    def cfg(self):
        return self._iso.cfg

    def _active_imported(self, name: str):
        pkg = self.cfg.SKILLS_DIR / name
        pkg.mkdir(parents=True)
        md = pkg / "SKILL.md"
        md.write_text(_pkg_md(name), encoding="utf-8")
        h = hashlib.sha256(md.read_bytes()).hexdigest()
        self.db.skill_stage(name, "説明", "", "imported", h, str(pkg), name)
        self.db.skill_set_status(name, "active", decided_by="master")

    def _patch_body_skills(self, names):
        return mock.patch.object(
            self.se.body, "skills",
            return_value=[{"name": n} for n in names],
        )

    def test_gone_skill_is_flagged_and_notified_once(self):
        self._active_imported("cook")
        self.se.sync_export()  # export 済みのものだけが照合対象
        with self._patch_body_skills([]):
            res = self.se.check_upstream(force=True)
        self.assertEqual(res["flagged"], ["cook"])
        row = self.db.skill_get("cook")
        self.assertTrue(row["upstream_gone_at"])
        outs = self.db.due_notifications()
        self.assertEqual(len(outs), 1)
        self.assertIn("upstream_gone", outs[0][3])
        self.assertEqual(row["status"], "active")  # 自動 disable はしない
        # 旗が立ったまま再照合しても通知は増えない（遷移の1回だけ）
        with self._patch_body_skills([]):
            res2 = self.se.check_upstream(force=True)
        self.assertEqual(res2["flagged"], [])
        self.assertEqual(len(self.db.due_notifications()), 1)

    def test_flag_clears_when_skill_is_back(self):
        self._active_imported("cook")
        self.se.sync_export()
        with self._patch_body_skills([]):
            self.se.check_upstream(force=True)
        with self._patch_body_skills(["cook"]):
            res = self.se.check_upstream(force=True)
        self.assertEqual(res["cleared"], ["cook"])
        self.assertIsNone(self.db.skill_get("cook")["upstream_gone_at"])

    def test_unreachable_body_skips_check(self):
        self._active_imported("cook")
        self.se.sync_export()
        with mock.patch.object(
            self.se.body, "skills",
            side_effect=self.se.hermes.BodyUnreachableError("down"),
        ):
            res = self.se.check_upstream(force=True)
        self.assertIn("unreachable", res["skipped"])
        self.assertIsNone(self.db.skill_get("cook")["upstream_gone_at"])

    def test_not_exported_or_non_active_is_not_checked(self):
        # export 無し（まだ写せてない）状態では誤検知しない
        self._active_imported("cook")
        with self._patch_body_skills([]):
            res = self.se.check_upstream(force=True)
        self.assertEqual(res["flagged"], [])
        # staged のものも対象外（inbox から掃き出されるのは設計上当然）
        pkg = self.cfg.SKILLS_DIR / "draft"
        pkg.mkdir()
        (pkg / "SKILL.md").write_text(_pkg_md("draft"), encoding="utf-8")
        h = hashlib.sha256((pkg / "SKILL.md").read_bytes()).hexdigest()
        self.db.skill_stage("draft", "説明", "", "imported", h, str(pkg), "draft")
        with self._patch_body_skills([]):
            res2 = self.se.check_upstream(force=True)
        self.assertEqual(res2["flagged"], [])

    def test_first_check_is_not_throttled_on_recent_boot(self):
        self._active_imported("cook")
        self.se.sync_export()
        with mock.patch.object(self.se.time, "monotonic", return_value=10), self._patch_body_skills([]):
            res = self.se.check_upstream()
        self.assertEqual(res["flagged"], ["cook"])

    def test_interval_throttle(self):
        res = self.se.check_upstream()
        res2 = self.se.check_upstream()
        self.assertEqual(res2.get("skipped"), "interval")
        res3 = self.se.check_upstream(force=True)
        self.assertNotEqual(res3.get("skipped"), "interval")


if __name__ == "__main__":
    unittest.main()
