"""ステップ3: 失敗復旧・処理記録の永続化のテスト。

- turns テーブル: 圧縮材料がLLM失敗で消えない / 成功した分だけ確定
- mem0保存: 失敗が turns.mem0_status に残り再試行できる / pendingは起動時unknownへ
- notify_outbox: 送信成功まで fired にしない / 一時失敗は再試行・恒久失敗は要対応
- job_runs: 日次処理の実行単位・失敗再試行・legacy引き継ぎ
- reflection_state: 洞察候補の部分保存からの再開
"""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import _support


def _count_turns(db_mod, **where):
    clause = " AND ".join(f"{k}=?" for k in where) or "1=1"
    with db_mod.get_db() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM turns WHERE {clause}", tuple(where.values())
        ).fetchone()[0]


class TurnsTableTest(unittest.TestCase):
    """turns テーブルの基本動作（同期テスト）"""

    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")

    def test_record_and_foldable(self):
        db = self.db
        for i in range(5):
            db.record_turn("s", f"質問{i}", f"返信{i}")
        # 直近3往復は残す → 古い2件が圧縮対象
        self.assertEqual(db.count_foldable_turns(3), 2)
        rows = db.foldable_turns(3)
        self.assertEqual([r[1] for r in rows], ["質問0", "質問1"])

    def test_commit_compressed_atomically(self):
        db = self.db
        ids = [
            db.record_turn("s", f"q{i}", f"a{i}") for i in range(5)
        ]
        rows = db.foldable_turns(3)
        db.commit_compressed_turns([r[0] for r in rows], "要約テキスト")
        self.assertEqual(db.get_recent_context_text(), "要約テキスト")
        self.assertEqual(db.count_foldable_turns(3), 0)
        self.assertEqual(
            _count_turns(db, compressed=0), 3
        )

    def test_seed_recent_context_from_file_once(self):
        db = self.db
        db.seed_recent_context("ファイルの既存テキスト")
        self.assertEqual(db.get_recent_context_text(), "ファイルの既存テキスト")
        # 二度目は上書きしない
        db.seed_recent_context("別のテキスト")
        self.assertEqual(db.get_recent_context_text(), "ファイルの既存テキスト")

    def test_mem0_status_transitions(self):
        db = self.db
        tid = db.record_turn("s", "q", "a")
        db.mark_turn_mem0_failed(tid, "boom")
        rows = db.turns_pending_mem0()
        self.assertEqual([r[0] for r in rows], [tid])
        db.mark_turn_mem0_done(tid)
        self.assertEqual(db.turns_pending_mem0(), [])

    def test_sweep_pending_to_unknown(self):
        db = self.db
        tid = db.record_turn("s", "q", "a")
        n = db.sweep_stale_pending_turns()
        self.assertEqual(n, 1)
        # unknown は自動再試行の対象にしない
        self.assertEqual(db.turns_pending_mem0(), [])
        self.assertEqual(_count_turns(db, mem0_status="unknown"), 1)

    def test_purge_only_processed(self):
        db = self.db
        tid_done = db.record_turn("s", "q1", "a1")
        db.mark_turn_mem0_done(tid_done)
        db.commit_compressed_turns([tid_done], "ctx")
        db.mark_turns_episode_done([tid_done])
        tid_unproc = db.record_turn("s", "q2", "a2")
        # 強制的に古い日付にしてから掃除
        with db.get_db() as conn:
            conn.execute(
                "UPDATE turns SET created_at='2000-01-01 00:00:00'"
            )
        self.assertEqual(db.purge_old_turns(30), 1)
        self.assertEqual(_count_turns(db), 1)


class CompressionDurabilityTest(unittest.IsolatedAsyncioTestCase):
    """_compress_recent が失敗しても材料が残ること"""

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
        self.cfg = self._iso.cfg

    async def test_llm_failure_keeps_turns(self):
        db, engine = self.db, self.engine
        for i in range(6):
            db.record_turn("s", f"q{i}", f"a{i}")
        # LLM が失敗 → 行が残るので次回も圧縮対象になる
        self.llm_mod.fake.queue.append(RuntimeError("llm down"))
        await engine._compress_recent()
        self.assertEqual(db.count_foldable_turns(engine._KEEP_TURNS), 3)
        self.assertEqual(db.get_recent_context_text(), "")
        # 次回は成功して同じ行が消費される
        self.llm_mod.fake.queue.append(_support.make_resp("圧縮された流れ"))
        await engine._compress_recent()
        self.assertEqual(db.get_recent_context_text(), "圧縮された流れ")
        self.assertEqual(db.count_foldable_turns(engine._KEEP_TURNS), 0)
        # DB確定済み本文がファイルにも出ている
        path = Path(self.cfg.RECENT_CONTEXT_PATH)
        self.assertEqual(path.read_text(encoding="utf-8").strip(),
                         "圧縮された流れ")

    async def test_empty_response_not_committed(self):
        db, engine = self.db, self.engine
        for i in range(6):
            db.record_turn("s", f"q{i}", f"a{i}")
        self.llm_mod.fake.queue.append(_support.make_resp(""))
        await engine._compress_recent()
        self.assertEqual(db.count_foldable_turns(engine._KEEP_TURNS), 3)

    async def test_file_seed_and_file_divergence_repair(self):
        """既存 recent_context.md を一度だけ種にし、DB確定済み本文でファイルを修復"""
        db, engine = self.db, self.engine
        path = Path(self.cfg.RECENT_CONTEXT_PATH)
        path.write_text("古いファイル本文", encoding="utf-8")
        engine.sync_recent_context_file()
        # 初回はファイルの内容をDBに取り込む
        self.assertEqual(db.get_recent_context_text(), "古いファイル本文")
        # DB側が新しい状態でファイルがずれていたら再出力する
        db.commit_compressed_turns([], "DB側の新しい本文")
        engine.sync_recent_context_file()
        self.assertEqual(
            path.read_text(encoding="utf-8").strip(), "DB側の新しい本文"
        )

    async def test_trigger_after_generate(self):
        """往復が閾値を超えたら圧縮タスクが起きる"""
        db, engine = self.db, self.engine
        # 直近3往復（_KEEP_TURNS）以外が RECENT_COMPRESS_EVERY(8)件以上
        for i in range(11):
            db.record_turn("s", f"q{i}", f"a{i}")
        self.llm_mod.fake.queue.extend(
            [_support.make_resp("返信"), _support.make_resp("圧縮テキスト")]
        )
        await engine.generate("s", "質問")
        await engine.drain_background(timeout=2)
        self.assertEqual(db.get_recent_context_text(), "圧縮テキスト")


class Mem0DurabilityTest(unittest.IsolatedAsyncioTestCase):
    """mem0保存の失敗追跡と再試行"""

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

    async def test_store_turn_failure_marked_and_retried(self):
        self.mem.store_turn_hook = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("mem0 down")
        )
        self.llm_mod.fake.queue.append(_support.make_resp("返信"))
        res = await self.engine.generate("s", "質問")
        await self.engine.drain_background(timeout=2)
        # 返信自体は成功し、失敗は turns に記録される
        self.assertEqual(res["reply"], "返信")
        self.assertEqual(_count_turns(self.db, mem0_status="failed"), 1)

        # 直したら再試行で回収される
        self.mem.store_turn_hook = None
        n = await self.engine.retry_turn_mem0()
        self.assertEqual(n, 1)
        self.assertEqual(_count_turns(self.db, mem0_status="done"), 1)


class NotifyOutboxTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")

    def test_dedupe_key_idempotent(self):
        self.assertTrue(self.db.enqueue_notification("k1", 100, "hi"))
        self.assertFalse(self.db.enqueue_notification("k1", 100, "hi"))
        rows = self.db.due_notifications()
        self.assertEqual(len(rows), 1)

    def test_retry_backoff_hides_until_due(self):
        self.db.enqueue_notification("k1", 100, "hi")
        nid = self.db.due_notifications()[0][0]
        self.db.mark_notification_failed(nid, "err", give_up=False)
        self.assertEqual(self.db.due_notifications(), [])
        with self.db.get_db() as conn:
            row = conn.execute(
                "SELECT status, attempts, next_retry_at FROM notify_outbox"
            ).fetchone()
        self.assertEqual(row[0], "pending")
        self.assertEqual(row[1], 1)
        self.assertGreater(row[2], datetime.now().strftime("%Y-%m-%d %H:%M"))

    def test_give_up_marks_needs_attention(self):
        self.db.enqueue_notification("k1", 100, "hi")
        nid = self.db.due_notifications()[0][0]
        self.db.mark_notification_failed(nid, "forbidden", give_up=True)
        self.assertEqual(self.db.due_notifications(), [])
        self.assertEqual(self.db.pending_outbox_summary()["notify_needs_attention"], 1)

    def test_sent_fires_reminder(self):
        db = self.db
        with db.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO reminders (channel_id, text, due_at) VALUES (100,'x','2000-01-01 00:00')"
            )
            rid = cur.lastrowid
        db.enqueue_notification(f"reminder:{rid}", 100, "hi")
        nid = db.due_notifications()[0][0]
        db.mark_notification_sent(nid)
        with db.get_db() as conn:
            fired = conn.execute(
                "SELECT fired FROM reminders WHERE id=?", (rid,)
            ).fetchone()[0]
        self.assertEqual(fired, 1)


class JobRunsTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")

    def test_begin_finish_cycle(self):
        db = self.db
        today = "2026-01-01"
        self.assertTrue(db.job_run_begin("digest", today))
        self.assertFalse(db.job_run_begin("digest", today))  # running中は不可
        db.job_run_finish("digest", today, "done")
        self.assertFalse(db.job_run_begin("digest", today))  # 完了済みは再実行しない

    def test_failed_retries_with_cap(self):
        db = self.db
        today = "2026-01-01"
        self.assertTrue(db.job_run_begin("digest", today))
        db.job_run_finish("digest", today, "failed")
        self.assertTrue(db.job_run_begin("digest", today))   # 再試行OK
        db.job_run_finish("digest", today, "failed")
        self.assertTrue(db.job_run_begin("digest", today))   # 3回目
        db.job_run_finish("digest", today, "failed")
        self.assertFalse(db.job_run_begin("digest", today))  # 上限到達

    def test_legacy_done_carries_over(self):
        db = self.db
        today = "2026-01-01"
        self.assertFalse(db.job_run_begin("digest", today, legacy_done=True))
        self.assertFalse(db.job_run_begin("digest", today))

    def test_sweep_running_to_failed(self):
        db = self.db
        today = "2026-01-01"
        db.job_run_begin("digest", today)
        self.assertEqual(db.sweep_running_jobs(), 1)
        # running→failed 掃き替え後は再試行できる
        self.assertTrue(db.job_run_begin("digest", today))


class JobsFlowTest(unittest.IsolatedAsyncioTestCase):
    """jobs.py の outbox 連携（リマインダー・配送・日次ジョブ）"""

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
        self.jobs = self._iso.import_module("companion.jobs")

    def _sender(self, side_effect=None):
        """通知配送 callable のスタブ。side_effect で失敗を注入"""
        return mock.AsyncMock(side_effect=side_effect)

    async def test_reminder_fires_only_on_delivery(self):
        db, jobs = self.db, self.jobs
        past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
        with db.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO reminders (channel_id, text, due_at) VALUES (100,'テスト',?)",
                (past,),
            )
            rid = cur.lastrowid

        await jobs._enqueue_due_reminders()
        # 送信前なので fired はまだ0
        with db.get_db() as conn:
            fired = conn.execute(
                "SELECT fired FROM reminders WHERE id=?", (rid,)
            ).fetchone()[0]
        self.assertEqual(fired, 0)
        self.assertEqual(len(db.due_notifications()), 1)
        # 再評価しても重複しない
        await jobs._enqueue_due_reminders()
        self.assertEqual(len(db.due_notifications()), 1)

        send = self._sender()
        await jobs._deliver_notifications(send)
        send.assert_awaited_once()
        with db.get_db() as conn:
            fired = conn.execute(
                "SELECT fired FROM reminders WHERE id=?", (rid,)
            ).fetchone()[0]
        self.assertEqual(fired, 1)

    async def test_reminder_send_failure_keeps_unfired(self):
        db, jobs = self.db, self.jobs
        discord_out = self._iso.import_module("companion.discord_out")
        past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO reminders (channel_id, text, due_at) VALUES (100,'テスト',?)",
                (past,),
            )
        await jobs._enqueue_due_reminders()
        # Undeliverable（権限なし等）→ 要対応。fired は0のまま残る
        send = self._sender(
            side_effect=discord_out.UndeliverableError("no access")
        )
        await jobs._deliver_notifications(send)
        self.assertEqual(db.pending_outbox_summary()["notify_needs_attention"], 1)
        with db.get_db() as conn:
            fired = conn.execute("SELECT fired FROM reminders").fetchone()[0]
        self.assertEqual(fired, 0)

    async def test_transient_failure_retries_later(self):
        db, jobs = self.db, self.jobs
        past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO reminders (channel_id, text, due_at) VALUES (100,'テスト',?)",
                (past,),
            )
        await jobs._enqueue_due_reminders()
        send = self._sender(side_effect=RuntimeError("503"))
        await jobs._deliver_notifications(send)
        # 再試行待ちに入ったので今は送対象外
        self.assertEqual(db.due_notifications(), [])
        # 時刻を戻せば再送され、成功する
        with db.get_db() as conn:
            conn.execute("UPDATE notify_outbox SET next_retry_at=NULL")
        send.side_effect = None
        await jobs._deliver_notifications(send)
        self.assertEqual(
            db.pending_outbox_summary()["notify_pending"], 0
        )

    async def test_reminder_cancel_removes_pending_notification(self):
        db = self.db
        reminders = self._iso.import_module("companion.tools.reminders")
        past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
        with db.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO reminders (channel_id, text, due_at) VALUES (100,'キャンセル対象',?)",
                (past,),
            )
            rid = cur.lastrowid
        db.enqueue_notification(f"reminder:{rid}", 100, "x")
        res = reminders.reminder_cancel(reminder_id=rid)
        self.assertIn("削除", res)
        with db.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0]
            r = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        self.assertEqual((n, r), (0, 0))

    async def test_run_daily_lifecycle(self):
        jobs, db = self.jobs, self.db
        fn = mock.AsyncMock(return_value="done")
        await jobs._run_daily("digest", "00:00", "last_digest", fn)
        fn.assert_awaited_once()
        # 同日は二度と実行しない
        fn2 = mock.AsyncMock(return_value="done")
        await jobs._run_daily("digest", "00:00", "last_digest", fn2)
        fn2.assert_not_awaited()

    async def test_run_daily_failure_retries(self):
        jobs, db = self.jobs, self.db
        bad = mock.AsyncMock(side_effect=RuntimeError("boom"))
        await jobs._run_daily("digest", "00:00", "last_digest", bad)
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT status FROM job_runs WHERE job='digest'"
            ).fetchone()
        self.assertEqual(row[0], "failed")
        # failed は同日内で再試行される
        ok = mock.AsyncMock(return_value="done")
        await jobs._run_daily("digest", "00:00", "last_digest", ok)
        ok.assert_awaited_once()
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT status FROM job_runs WHERE job='digest'"
            ).fetchone()
        self.assertEqual(row[0], "done")

    async def test_run_daily_not_before_time(self):
        """予定時刻前は実行しない。過ぎていれば同日未実行分として実行する"""
        jobs = self.jobs

        class _FixedDT:
            @classmethod
            def now(cls):
                return datetime(2026, 1, 5, 8, 0)

        fn = mock.AsyncMock(return_value="done")
        with mock.patch.object(jobs, "datetime", _FixedDT):
            await jobs._run_daily("digest", "09:00", "last_digest", fn)
            fn.assert_not_awaited()
            await jobs._run_daily("digest", "07:00", "last_digest", fn)
            fn.assert_awaited_once()

    async def test_delegate_notify_via_outbox(self):
        """完了ジョブは notify_status 経由で送信待ちに載り、送信成功まで残る"""
        db, jobs = self.db, self.jobs
        delegate = self._iso.import_module("companion.tools.delegate")
        # 完了済み・未通知のジョブを直接作る（_set_status 経由で pending になる）
        with db.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO delegate_jobs (prompt, request, workdir, status)"
                " VALUES ('p','テスト依頼','/tmp','running')"
            )
            jid = cur.lastrowid
        delegate._set_status(jid, "done", finished_at="2026-01-01 00:00:00")
        self._iso.cfg.NOTIFY_CHANNEL_ID = 555
        await jobs._record_delegate_results()
        rows = db.due_notifications()
        self.assertEqual(len(rows), 1)
        self.assertIn(f"依頼 #{jid}", rows[0][3])
        self.assertEqual(rows[0][2], 555)
        # 送信成功まで notify_status は 'queued'
        with db.get_db() as conn:
            ns = conn.execute(
                "SELECT notify_status FROM delegate_jobs WHERE id=?", (jid,)
            ).fetchone()[0]
        self.assertEqual(ns, "queued")
        await jobs._deliver_notifications(self._sender())
        with db.get_db() as conn:
            ns = conn.execute(
                "SELECT notify_status FROM delegate_jobs WHERE id=?", (jid,)
            ).fetchone()[0]
        self.assertEqual(ns, "sent")
        # 自己メモも記録された
        self.assertEqual(len(self.mem.calls["add_self_note"]), 1)

    async def test_delegate_notify_waits_for_channel(self):
        db, jobs = self.db, self.jobs
        delegate = self._iso.import_module("companion.tools.delegate")
        with db.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO delegate_jobs (prompt, request, workdir, status)"
                " VALUES ('p','x','/tmp','running')"
            )
            jid = cur.lastrowid
        delegate._set_status(jid, "done")
        self._iso.cfg.NOTIFY_CHANNEL_ID = 0
        await jobs._record_delegate_results()
        # 通知先なし → enqueueされず pending のまま
        self.assertEqual(db.due_notifications(), [])
        with db.get_db() as conn:
            ns = conn.execute(
                "SELECT notify_status FROM delegate_jobs WHERE id=?", (jid,)
            ).fetchone()[0]
        self.assertEqual(ns, "pending")
        # チャンネルが分かった次回で拾われる
        self._iso.cfg.NOTIFY_CHANNEL_ID = 777
        await jobs._record_delegate_results()
        self.assertEqual(len(db.due_notifications()), 1)


class ReflectionResumeTest(unittest.IsolatedAsyncioTestCase):
    """run_reflection の再開可能化"""

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
        self.reflect = self._iso.import_module("companion.reflect")

    def _seed_memories(self):
        self.mem.self_memories_recent.return_value = [
            {"created_at": "2026-01-01", "memory": "楽しい会話をした"}
        ]

    async def test_insights_generated_once_then_resumed(self):
        self._seed_memories()
        self.llm_mod.fake.queue.append(
            _support.make_resp('[{"text":"深い洞察","importance":4}]')
        )
        res = await self.reflect.run_reflection("2026-01-01")
        self.assertEqual(res["insights"], ["深い洞察"])
        self.assertEqual(len(self.mem.calls["add_self_note"]), 1)
        # 再実行しても LLM は呼ばず、保存済みなので追加書き込みもない
        calls_before = len(self.llm_mod.fake.calls)
        res2 = await self.reflect.run_reflection("2026-01-01")
        self.assertEqual(len(self.llm_mod.fake.calls), calls_before)
        self.assertEqual(len(self.mem.calls["add_self_note"]), 1)
        self.assertEqual(res2["insights"], ["深い洞察"])

    async def test_parse_failure_raises_and_does_not_persist(self):
        self._seed_memories()
        self.llm_mod.fake.queue.append(_support.make_resp("not json at all"))
        with self.assertRaises(RuntimeError):
            await self.reflect.run_reflection("2026-01-01")
        # 失敗なので reflection_state は無く、次回は生成からやり直せる
        self.assertIsNone(self.db.get_reflection_insights("2026-01-01"))

    async def test_partial_save_resumes_from_unsaved(self):
        self._seed_memories()
        self.llm_mod.fake.queue.append(
            _support.make_resp(
                '[{"text":"洞察A","importance":4},'
                '{"text":"洞察B","importance":5}]'
            )
        )
        # 2件目の保存で落ちる
        state = {"n": 0}

        def flaky(*a, **k):
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("mem0 write fail")

        self.mem.self_note_hook = flaky
        with self.assertRaises(RuntimeError):
            await self.reflect.run_reflection("2026-01-01")
        self.assertEqual(len(self.mem.calls["add_self_note"]), 1)  # Aのみ保存済み

        # 復旧後の再実行: LLMを呼ばずにBだけ保存される
        self.mem.self_note_hook = None
        calls_before = len(self.llm_mod.fake.calls)
        res = await self.reflect.run_reflection("2026-01-01")
        self.assertEqual(len(self.llm_mod.fake.calls), calls_before)
        self.assertEqual(len(self.mem.calls["add_self_note"]), 2)
        self.assertEqual(res["insights"], ["洞察A", "洞察B"])

    async def test_empty_insights_is_legitimate(self):
        self._seed_memories()
        self.llm_mod.fake.queue.append(_support.make_resp("[]"))
        res = await self.reflect.run_reflection("2026-01-01")
        self.assertEqual(res["insights"], [])
        # 空でも確定済みとして残る（再生成しない）
        self.assertEqual(self.db.get_reflection_insights("2026-01-01"), [])


class ShutdownDrainTest(unittest.IsolatedAsyncioTestCase):
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
        self.engine = self._iso.import_module("companion.engine")

    async def test_drain_waits_for_background_tasks(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow():
            started.set()
            await release.wait()

        t = self.engine._spawn(slow())
        await started.wait()
        drain = asyncio.create_task(self.engine.drain_background(timeout=2))
        await asyncio.sleep(0.01)
        self.assertFalse(drain.done())
        release.set()
        await drain
        self.assertTrue(t.done())


if __name__ == "__main__":
    unittest.main()
