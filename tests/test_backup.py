"""backup.py: data/ 日次スナップショットと世代ローテーション"""
import json
import os
import sqlite3
import stat
import unittest
from pathlib import Path

from _support import IsolatedModules


def _seed_data(cfg):
    """最小の data/ 環境を作る（db1件・dir1件・ファイル2件）"""
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.DATA_DIR / "companion.db")
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('hello')")
    conn.commit()
    conn.close()
    (cfg.DATA_DIR / "qdrant").mkdir(exist_ok=True)
    (cfg.DATA_DIR / "qdrant" / "vec.bin").write_bytes(b"\x00" * 10)
    (cfg.DATA_DIR / "state.json").write_text('{"a": 1}', encoding="utf-8")
    (cfg.DATA_DIR / "persona_notes.md").write_text("memo", encoding="utf-8")
    # ルート側人格ファイルの偽物
    cfg.ROOT = cfg.DATA_DIR / "root"
    cfg.ROOT.mkdir(exist_ok=True)
    (cfg.ROOT / "persona.md").write_text("persona", encoding="utf-8")


class BackupTest(unittest.TestCase):
    def test_snapshot_contents(self):
        with IsolatedModules() as iso:
            _seed_data(iso.cfg)
            backup = iso.import_module("companion.backup")
            res = backup.run_backup()

            dest = Path(res["dir"])
            self.assertTrue(dest.name.startswith("daily-"))
            # sqlite 写しは開けて中身が同じ
            conn = sqlite3.connect(dest / "db" / "companion.db")
            self.assertEqual(conn.execute("SELECT x FROM t").fetchone()[0], "hello")
            conn.close()
            self.assertTrue((dest / "qdrant" / "vec.bin").exists())
            self.assertTrue((dest / "state.json").exists())
            self.assertTrue((dest / "persona.md").exists())
            manifest = json.loads((dest / "manifest.json").read_text("utf-8"))
            self.assertIn("db/companion.db", manifest["files"])
            self.assertEqual(res["errors"], [])

    def test_missing_files_tolerated(self):
        with IsolatedModules() as iso:
            iso.cfg.ROOT = iso.cfg.DATA_DIR / "root"  # 空（ファイル無し）
            backup = iso.import_module("companion.backup")
            res = backup.run_backup()
            self.assertEqual(res["errors"], [])
            # db・ファイル類は無いので写さない（空dir写しは入る）
            self.assertFalse(any(f.startswith("db/") for f in res["files"]))
            self.assertFalse(any(f.endswith(".json") for f in res["files"]))
            self.assertTrue(
                (Path(res["dir"]) / "manifest.json").exists())

    def test_rotation_keeps_n_and_spares_other_dirs(self):
        with IsolatedModules() as iso:
            _seed_data(iso.cfg)
            backup = iso.import_module("companion.backup")
            bdir = iso.cfg.BACKUP_DIR
            # 既存世代を手で作る（migrate バックアップ風 + daily-*）
            for i in range(9):
                (bdir / f"daily-2026010{i}-000000").mkdir(parents=True)
            keep_dir = bdir / "pre-lmig-20260101"
            keep_dir.mkdir(parents=True)
            (keep_dir / "RESTORE.md").write_text("x", encoding="utf-8")

            res = backup.run_backup()
            self.assertTrue(Path(res["dir"]).exists())
            remaining = sorted(p.name for p in bdir.glob("daily-*"))
            self.assertEqual(len(remaining), iso.cfg.BACKUP_KEEP)
            self.assertTrue(keep_dir.exists())  # pre-lmig-* は消えない
            # 最新世代が残っている
            self.assertIn(Path(res["dir"]).name, remaining)

    def test_fix_readonly_clears_attr(self):
        with IsolatedModules() as iso:
            backup = iso.import_module("companion.backup")
            f = iso.cfg.DATA_DIR / "a.txt"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x", encoding="utf-8")
            sub = iso.cfg.DATA_DIR / "d" / "b.txt"
            sub.parent.mkdir(parents=True)
            sub.write_text("y", encoding="utf-8")
            for p in (f, sub):
                os.chmod(p, stat.S_IREAD)

            fixed = backup.fix_readonly(iso.cfg.DATA_DIR)
            self.assertEqual(sorted(fixed),
                             ["a.txt", str(Path("d") / "b.txt")])
            for p in (f, sub):
                self.assertTrue(p.stat().st_mode & stat.S_IWRITE)

    def test_backup_copies_are_writable(self):
        """コピー元が読み取り専用でも写し先は R 属性を引かない"""
        with IsolatedModules() as iso:
            _seed_data(iso.cfg)
            backup = iso.import_module("companion.backup")
            src = iso.cfg.DATA_DIR / "qdrant" / "vec.bin"
            os.chmod(src, stat.S_IREAD)

            res = backup.run_backup()
            copied = Path(res["dir"]) / "qdrant" / "vec.bin"
            self.assertTrue(copied.stat().st_mode & stat.S_IWRITE)

    def test_rotation_removes_dirs_with_readonly_files(self):
        """R属性ファイルを含む世代でも rotate で消せる"""
        with IsolatedModules() as iso:
            _seed_data(iso.cfg)
            backup = iso.import_module("companion.backup")
            bdir = iso.cfg.BACKUP_DIR
            stale = bdir / "daily-20260101-000000"
            (stale / "qdrant").mkdir(parents=True)
            ro = stale / "qdrant" / "meta.json"
            ro.write_text("{}", encoding="utf-8")
            os.chmod(ro, stat.S_IREAD)

            removed = backup.rotate(keep=0)
            self.assertEqual(removed, [stale])
            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
