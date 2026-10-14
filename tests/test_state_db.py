"""db.py の state.json 更新APIと通知先解決のテスト（本番 data/ には触れない）"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import _support


class StateDbTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")

    @property
    def state_path(self):
        return self._iso.cfg.STATE_PATH

    def test_temp_dir_isolated_from_production(self):
        self.assertNotEqual(self._iso.cfg.DATA_DIR, _support.ROOT / "data")

    def test_save_state_writes_and_loads(self):
        self.db.save_state(inner={"mood": "元気"}, last_channel_id=42)
        state = self.db.load_state()
        self.assertEqual(state["inner"]["mood"], "元気")
        self.assertEqual(state["last_channel_id"], 42)
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["last_channel_id"], 42)

    def test_update_state_mutator_sees_latest(self):
        self.db.save_state(count=0)
        for _ in range(5):
            self.db.update_state(lambda s: s.__setitem__("count", s["count"] + 1))
        self.assertEqual(self.db.load_state()["count"], 5)

    def test_update_state_remove_keeps_other_keys(self):
        self.db.save_state(llm={"model": "x"}, inner={"mood": "普通"})
        self.db.update_state(remove=["llm"])
        state = self.db.load_state()
        self.assertNotIn("llm", state)
        self.assertEqual(state["inner"]["mood"], "普通")

    def test_update_state_combines_mutator_remove_and_kw(self):
        self.db.save_state(a=1, gone=1)
        self.db.update_state(lambda s: s.__setitem__("a", s["a"] + 1), remove=["gone"], b=2)
        state = self.db.load_state()
        self.assertEqual(state, {"a": 2, "b": 2})

    def test_mutator_exception_does_not_write(self):
        self.db.save_state(a=1)

        def boom(s):
            s["a"] = 999
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            self.db.update_state(boom)
        self.assertEqual(self.db.load_state()["a"], 1)

    def test_concurrent_updates_do_not_lose_keys(self):
        n = 8
        barrier = threading.Barrier(n)
        errors = []

        def write_key(i):
            try:
                barrier.wait(timeout=10)
                self.db.update_state(lambda s: s.__setitem__(f"k{i}", i))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=write_key, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        state = self.db.load_state()
        for i in range(n):
            self.assertEqual(state.get(f"k{i}"), i)

    def test_write_failure_keeps_old_file_and_cleans_tmp(self):
        self.db.save_state(inner={"mood": "普通"})
        before = self.state_path.read_text(encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.db.save_state(inner={"mood": "元気"})
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
        leftovers = [p.name for p in self.state_path.parent.glob("*.tmp")]
        self.assertEqual(leftovers, [])

    def test_corrupt_state_is_preserved_and_recovered(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.db.load_state(), {})
        broken = list(self.state_path.parent.glob("state.json.broken-*"))
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0].read_text(encoding="utf-8"), "{not json")
        # 壊れた後でも保存は動き、退避ファイルは消さない
        self.db.save_state(inner={"mood": "普通"})
        self.assertEqual(self.db.load_state()["inner"]["mood"], "普通")
        self.assertTrue(broken[0].exists())

    def test_non_dict_state_is_treated_as_broken(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("[1,2,3]", encoding="utf-8")
        self.assertEqual(self.db.load_state(), {})
        self.assertTrue(
            list(self.state_path.parent.glob("state.json.broken-*"))
        )

    def test_v3_to_v4_migration(self):
        """v3 のDBが v4 に上がり、既存 approvals 行は kind='delegate' になる"""
        import sqlite3
        # v3 時点の approvals スキーマ（kind/ref/code/summary 列なし）を再現
        conn = sqlite3.connect(self._iso.cfg.DB_PATH)
        conn.execute(
            "CREATE TABLE approvals ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL,"
            " content_hash TEXT NOT NULL, requester TEXT,"
            " status TEXT NOT NULL DEFAULT 'pending',"
            " created_at TEXT DEFAULT (datetime('now','localtime')),"
            " expires_at TEXT, decided_by TEXT, decided_at TEXT)"
        )
        conn.execute(
            "INSERT INTO approvals (job_id, content_hash, requester)"
            " VALUES (3,'hash-x','12345')"
        )
        # v3 実DB には v1 で作った turns も必ずある（v6 の ALTER が当たる先）
        conn.execute(
            "CREATE TABLE turns ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_key TEXT NOT NULL,"
            " user_text TEXT NOT NULL, assistant_text TEXT NOT NULL, ts TEXT,"
            " source TEXT, mem0_status TEXT NOT NULL DEFAULT 'pending',"
            " mem0_attempts INTEGER NOT NULL DEFAULT 0, mem0_error TEXT,"
            " compressed INTEGER NOT NULL DEFAULT 0,"
            " episode_done INTEGER NOT NULL DEFAULT 0, inner_text TEXT,"
            " created_at TEXT DEFAULT (datetime('now','localtime')))"
        )
        # v3 で増えた skills 表も作っておく（v5 以降の ALTER が当たる先）
        conn.execute(
            "CREATE TABLE skills ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,"
            " description TEXT, scopes TEXT,"
            " origin TEXT NOT NULL DEFAULT 'imported',"
            " status TEXT NOT NULL DEFAULT 'staged',"
            " content_hash TEXT NOT NULL, source_path TEXT, rel_path TEXT,"
            " note TEXT, created_at TEXT DEFAULT (datetime('now','localtime')),"
            " decided_by TEXT, decided_at TEXT)"
        )
        conn.execute("PRAGMA user_version=3")
        conn.commit()
        conn.close()
        with self.db.get_db() as conn:
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(ver, self.db._SCHEMA_VERSION)
            row = conn.execute(
                "SELECT kind FROM approvals WHERE job_id=3"
            ).fetchone()
            self.assertEqual(row[0], "delegate")
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("body_runs", tables)
            self.assertIn("turn_tool_calls", tables)
            indexes = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            self.assertIn("idx_ttc_turn", indexes)
            self.assertIn("idx_ttc_ref", indexes)
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(approvals)")
            }
            self.assertTrue({"kind", "ref", "code", "summary"} <= cols)

    def test_purge_cleans_tool_calls_and_body_runs(self):
        """turns の掃除に turn_tool_calls の孤児と、報告済みの古い
        body_runs（とその tool 行）の掃除が追従する"""
        db = self.db
        # 掃除対象になる古い往復 + そのツール記録
        tid = db.record_turn("s", "u", "a")
        db.record_tool_calls(tid, "s", [
            {"tool": "tasks_list", "call_id": "c1", "args": {},
             "status": "ok", "origin": "core", "result": "r"},
        ])
        with db.get_db() as conn:
            conn.execute(
                "UPDATE turns SET compressed=1, mem0_status='done',"
                " episode_done=1,"
                " created_at=datetime('now','localtime','-60 days')"
                " WHERE id=?",
                (tid,),
            )
            # 報告済みの古い body_run + tool 記録 → 消える
            conn.execute(
                "INSERT INTO body_runs (run_id, task, status,"
                " report_status, finished_at)"
                " VALUES ('old-run','t','completed','done',"
                " datetime('now','localtime','-60 days'))"
            )
            conn.execute(
                "INSERT INTO turn_tool_calls"
                " (origin, ref, tool, status)"
                " VALUES ('body_run','old-run','browser','ok')"
            )
            # 未報告の古い body_run と 進行中 run は残す
            conn.execute(
                "INSERT INTO body_runs (run_id, task, status,"
                " report_status, finished_at)"
                " VALUES ('unreported','t','failed','pending',"
                " datetime('now','localtime','-60 days'))"
            )
            conn.execute(
                "INSERT INTO body_runs (run_id, task, status)"
                " VALUES ('live-run','t','running')"
            )
            conn.execute(
                "INSERT INTO turn_tool_calls"
                " (origin, ref, tool, status)"
                " VALUES ('body_run','unreported','browser','ok')"
            )
        self.assertEqual(db.purge_old_turns(30), 1)
        with db.get_db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM turn_tool_calls"
                             " WHERE ref='c1'").fetchone()[0], 0)
            self.assertIsNone(db.body_run_get("old-run"))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM turn_tool_calls"
                             " WHERE ref='old-run'").fetchone()[0], 0)
            self.assertIsNotNone(db.body_run_get("unreported"))
            self.assertIsNotNone(db.body_run_get("live-run"))

    def test_resolve_notify_channel_order(self):
        cfg = self._iso.cfg
        # env > 保存済み > メモリ上の最終チャンネル
        self.db.set_last_channel(777)
        self.db.save_state(last_channel_id=111)
        cfg.NOTIFY_CHANNEL_ID = 555
        self.assertEqual(self.db.resolve_notify_channel(), 555)
        cfg.NOTIFY_CHANNEL_ID = 0
        self.assertEqual(self.db.resolve_notify_channel(), 111)
        self.state_path.unlink()
        self.assertEqual(self.db.resolve_notify_channel(), 777)


if __name__ == "__main__":
    unittest.main()
