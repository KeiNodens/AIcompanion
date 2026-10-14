"""P8-2 ingress + admission のテスト

カバー: envelope 検証（span/part_count/coverage）・redaction・敵対 Unicode・
declared class 照合・capability・dedup・receipt/watermark・drain→Event Log・
admission 検査列・tombstone/suppression・承認照合器・gate.inputs drift・
conformance。
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class IngestTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        cls.writer = cls._iso.import_module("companion.kernel.writer")
        cls.canon = cls._iso.import_module("companion.kernel.canon")
        cls.envelope = cls._iso.import_module("companion.kernel.envelope")
        cls.admit = cls._iso.import_module("companion.kernel.admit")
        cls.approval = cls._iso.import_module("companion.kernel.approval")
        cls.intake = cls._iso.import_module("companion.adapters.intake")
        cls.admission = cls._iso.import_module("companion.adapters.admission")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ingest_test_"))
        self.t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        self._tick = {"i": 0}

        def clock():
            self._tick["i"] += 1
            return self.t0 + timedelta(seconds=self._tick["i"])

        def sclock():  # adapter 側は ISO 文字列時計を受ける
            self._tick["i"] += 1
            return self.canon.canon_ts(
                self.t0 + timedelta(seconds=self._tick["i"]))

        self.clock = clock
        self.w = self.writer.open_core(self.tmp / "core.db", clock=clock)
        self.ik = self.intake.Intake(self.w, clock=sclock)
        self.adm = self.admission.Admission(self.w, clock=sclock)
        self.ik.register_adapter({
            "adapter_id": "test-adapter",
            "adapter_version": "v1",
            "coverage": {"classes": ["conversation", "tool", "approval"]},
            "capabilities": ["approval_decision"],
        })
        self.ik.register_adapter({
            "adapter_id": "conv-only",
            "adapter_version": "v1",
            "coverage": {"classes": ["conversation"]},
            "capabilities": [],
        })

    def tearDown(self):
        self.w.close()

    def _env(self, parts=None, adapter="test-adapter", stream="s1",
             span=None, **kw):
        if parts is None:
            parts = [
                {"part_id": "m1", "class": "conversation", "origin": "user",
                 "ts": "2026-10-09T10:00:00.000000Z", "body": "こんにちは"},
                {"part_id": "m2", "class": "conversation", "origin": "assistant",
                 "ts": "2026-10-09T10:00:01.000000Z", "body": "はい"},
            ]
        env = {
            "envelope_id": "e1",
            "adapter_id": adapter,
            "format_version": "pcore.envelope.v1",
            "stream_id": stream,
            "span": span if span is not None else
            {"start_cursor": "1", "end_cursor": str(len(parts))},
            "coverage": {"classes": sorted({p["class"] for p in parts})},
            "parts": parts,
        }
        env.update(kw)
        return env

    def _first_message_event(self):
        return self.w._conn.execute(
            "SELECT event_id FROM events WHERE event_type='message'"
            " LIMIT 1").fetchone()["event_id"]

    def _memory_proposal(self, basis_seq=2, rid="mem_t1",
                         content="マスターはラーメンが好き", **kw):
        prop = {
            "record_family": "memory",
            "target_id": None,
            "new_state": {
                "record_id": rid, "record_family": "memory",
                "scope": "user", "status": "active", "record_version": 1,
                "memory_type": "fact",
                "content": content,
                "content_hash": self.canon.content_hash(content),
                "created_at": "2026-10-09T10:01:00.000000Z",
                "updated_at": "2026-10-09T10:01:00.000000Z",
                "importance": 2,
            },
            "basis_events": [{"event_id": self._first_message_event()}],
            "basis_seq": basis_seq,
            "generated_by": "extraction",
            "generator": {"method": "t", "model": "m", "version": "0"},
            "producer": "extraction",
        }
        prop.update(kw)
        return prop

    def _write_tombstone(self, text, tid="tmb_1"):
        st = {
            "record_id": tid, "record_family": "tombstone",
            "scope": "user", "status": "active", "record_version": 1,
            "pattern_hash": self.canon.pattern_hash(text),
            "pattern": text, "pattern_kind": "content",
            "created_at": "2026-10-09T10:02:00.000000Z",
            "updated_at": "2026-10-09T10:02:00.000000Z",
        }
        return self.w.apply_mutation(
            "record_write",
            {"record_family": "tombstone", "record_id": tid,
             "new_state": st, "reason": "test tombstone"},
            actor="system")


class TestEnvelope(IngestTestBase):
    def test_happy_submit_receipt_drain(self):
        r = self.ik.submit_envelope(self._env())
        self.assertTrue(r["accepted"])
        self.assertEqual(r["receipt"]["verdict"], "received")

        res = self.ik.drain()
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["emitted"], 2)
        rows = self.w._conn.execute(
            "SELECT event_type, actor FROM events ORDER BY seq").fetchall()
        self.assertEqual(
            [(r2["event_type"], r2["actor"]) for r2 in rows],
            [("message", "master"), ("response", "companion")])

    def test_validation_error_quarantined(self):
        bad = self._env()
        del bad["span"]
        r = self.ik.submit_envelope(bad)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["receipt"]["verdict"], "quarantined")
        self.assertTrue(any("span" in e
                            for e in r["receipt"]["detail"]["errors"]))

    def test_part_count_mismatch_quarantined(self):
        r = self.ik.submit_envelope(self._env(part_count=99))
        self.assertFalse(r["accepted"])
        self.assertEqual(r["receipt"]["verdict"], "quarantined")

    def test_dedup_resubmit_returns_stored(self):
        env = self._env()
        r1 = self.ik.submit_envelope(env)
        r2 = self.ik.submit_envelope(env)
        self.assertTrue(r2["dedup"])
        self.assertEqual(r2["receipt"]["receipt_id"],
                         r1["receipt"]["receipt_id"])

    def test_secret_redaction_stored(self):
        self.ik.submit_envelope(self._env(parts=[
            {"part_id": "m1", "class": "conversation", "origin": "user",
             "ts": "2026-10-09T10:00:00.000000Z",
             "body": "API_KEY=abcdef0123456789abcdef0123456789"}]))
        row = self.w._conn.execute(
            "SELECT parts_json FROM ingress_envelopes").fetchone()
        parts = self.canon.loads(row["parts_json"])
        self.assertEqual(parts[0]["body"], "API_KEY=***")
        self.assertNotIn("abcdef012345", parts[0]["body"])

    def test_hostile_unicode_flagged(self):
        r = self.ik.submit_envelope(self._env(parts=[
            {"part_id": "m1", "class": "conversation", "origin": "user",
             "ts": "2026-10-09T10:00:00.000000Z",
             "body": "a\u202Eevil\u200Btext"}]))
        row = self.w._conn.execute(
            "SELECT parts_json FROM ingress_envelopes").fetchone()
        parts = self.canon.loads(row["parts_json"])
        self.assertEqual(parts[0]["body"], "aeviltext")
        self.assertIn("hostile_unicode", r["receipt"]["detail"]["flags"])

    def test_undeclared_class_flagged_and_skipped(self):
        r = self.ik.submit_envelope(self._env(adapter="conv-only", parts=[
            {"part_id": "m1", "class": "conversation", "origin": "user",
             "ts": "2026-10-09T10:00:00.000000Z", "body": "hi"},
            {"part_id": "m2", "class": "approval", "origin": "user",
             "ts": "2026-10-09T10:00:01.000000Z", "body": "いいよ"}]))
        self.assertTrue(r["accepted"])
        self.assertEqual(r["receipt"]["detail"]["undeclared_parts"], [1])
        self.ik.drain()
        evs = [r2["event_type"] for r2 in self.w._conn.execute(
            "SELECT event_type FROM events ORDER BY seq")]
        self.assertEqual(evs, ["message", "skipped_part"])

    def test_capability_required_for_approval(self):
        # conv-only は approval class を coverage 宣言していないので
        # 宣言照合で先に落ちる。capability 側は adapter を approval 宣言済み・
        # cap 無しで試す。
        self.ik.register_adapter({
            "adapter_id": "ap-nocap",
            "adapter_version": "v1",
            "coverage": {"classes": ["approval"]},
            "capabilities": [],
        })
        r = self.ik.submit_envelope(self._env(adapter="ap-nocap", parts=[
            {"part_id": "a1", "class": "approval", "origin": "user",
             "ts": "2026-10-09T10:00:00.000000Z",
             "body": {"text": "いいよ", "approval_id": "ap_1",
                      "decision": "approve"}}]))
        self.assertEqual(r["receipt"]["detail"]["undeclared_parts"], [0])
        stored = self.canon.loads(self.w._conn.execute(
            "SELECT parts_json FROM ingress_envelopes").fetchone()[0])
        self.assertIn("missing capability",
                      stored[0].get("detail", ""))

    def test_watermark_advances_atomically(self):
        self.ik.submit_envelope(self._env())
        self.ik.submit_envelope(
            self._env(envelope_id="e2",
                      span={"start_cursor": "3", "end_cursor": "4"}))
        wm = self.w._conn.execute(
            "SELECT cursor_value, last_envelope_key FROM ingress_watermarks"
            " WHERE adapter_id='test-adapter' AND stream_id='s1'"
        ).fetchone()
        self.assertEqual(wm["cursor_value"], "4")
        self.assertIsNotNone(wm["last_envelope_key"])

    def test_conflict_same_key_diff_content(self):
        self.ik.submit_envelope(self._env())
        self.ik.drain()
        # 同一 (adapter, stream, part_id, transform) で別内容を drain 再送
        # → (actor, ingress_key) の内容不一致 → IngressConflictError
        dup_env = self._env()
        dup_env["parts"][0]["body"] = "改ざんされた本文"
        # envelope_key が違うので別 envelope として受理される
        self.ik.submit_envelope(dup_env)
        res = self.ik.drain()
        # poison 隔離: その envelope は failed＋error（operational signal）
        self.assertFalse(res[0]["ok"])
        self.assertIn("conflict", res[0]["error"])


class TestAdmission(IngestTestBase):
    def setUp(self):
        super().setUp()
        self.ik.submit_envelope(self._env())
        self.ik.drain()

    def test_accept_writes_memory(self):
        out = self.adm.submit(self._memory_proposal())
        self.assertEqual(out["verdict"], "accept")
        row = self.w._conn.execute(
            "SELECT record_version FROM records"
            " WHERE record_id='mem_t1'").fetchone()
        self.assertEqual(row["record_version"], 1)
        self.assertTrue(self.w.replay_check()["ok"])

    def test_pending_on_review_importance(self):
        prop = self._memory_proposal()
        prop["new_state"]["importance"] = 5
        out = self.adm.submit(prop)
        self.assertEqual(out["verdict"], "pending")
        cand = self.w._conn.execute(
            "SELECT status FROM candidates").fetchone()
        self.assertEqual(cand["status"], "pending")

    def test_pending_on_relative_time(self):
        prop = self._memory_proposal(
            content="明日マスターと打ち合わせ",
            rid="mem_rel")
        prop["new_state"]["content_hash"] = self.canon.content_hash(
            "明日マスターと打ち合わせ")
        out = self.adm.submit(prop)
        self.assertEqual(out["verdict"], "pending")
        fired = {c["check"] for c in out["checks"]
                 if c["result"] == "pending"}
        self.assertIn("concreteness", fired)

    def test_quarantine_empty_basis(self):
        prop = self._memory_proposal(basis_events=[])
        out = self.adm.submit(prop)
        self.assertEqual(out["verdict"], "quarantine")
        cand = self.w._conn.execute(
            "SELECT status FROM candidates").fetchone()
        self.assertEqual(cand["status"], "quarantined")

    def test_tombstone_exact_rejects(self):
        text = "マスターはラーメンが好き"
        self._write_tombstone(text)
        out = self.adm.submit(self._memory_proposal(basis_seq=3))
        fired = {c["check"]: c["result"] for c in out["checks"]
                 if c["result"] != "pass"}
        self.assertEqual(fired.get("tombstone"), "fail")
        self.assertEqual(out["verdict"], "reject")

    def test_tombstone_similar_pending(self):
        # 同じ主語・属性で文字列が似ているものを tombstone に登録
        # （trigram Jaccard ≥ 0.8 → exact ではなく similar 側）
        self._write_tombstone("マスターはラーメンが好きだ")
        out = self.adm.submit(self._memory_proposal(basis_seq=3))
        tomb = [c for c in out["checks"] if c["check"] == "tombstone"][0]
        self.assertIn(tomb["result"], ("pending", "fail"))

    def test_stale_cas(self):
        self.adm.submit(self._memory_proposal())
        prop = self._memory_proposal(basis_seq=3)
        prop["op"] = "correction"
        prop["target_id"] = "mem_t1"
        prop["new_state"].update(
            {"record_id": "mem_t1", "record_version": 2})
        prop["expected_fingerprint"] = self.admit.fingerprint(None)
        out = self.adm.submit(prop)
        self.assertEqual(out["verdict"], "stale")

    def test_suppression_after_reject(self):
        # secret 混入 → reject → 終端 candidate → 同内容再提案は suppress
        secret_prop = self._memory_proposal(
            content="API_KEY=abcdef0123456789abcdef0123456789",
            rid="mem_sec")
        secret_prop["new_state"]["content_hash"] = \
            self.canon.content_hash(secret_prop["new_state"]["content"])
        out1 = self.adm.submit(secret_prop)
        self.assertEqual(out1["verdict"], "reject")

        prop2 = self._memory_proposal(
            content="API_KEY=abcdef0123456789abcdef0123456789",
            rid="mem_sec2", basis_seq=None)
        prop2["new_state"]["content_hash"] = \
            self.canon.content_hash(prop2["new_state"]["content"])
        out2 = self.adm.submit(prop2)
        fired = {c["check"] for c in out2["checks"]
                 if c["result"] == "fail"}
        self.assertIn("suppression", fired)


class TestApproval(IngestTestBase):
    def setUp(self):
        super().setUp()
        self.ik.submit_envelope(self._env())
        self.ik.drain()

    def _pending_candidate(self):
        prop = self._memory_proposal()
        prop["new_state"]["importance"] = 5  # review_required → pending
        out = self.adm.submit(prop)
        assert out["verdict"] == "pending", out
        return out["candidate"]["candidate_id"]

    def test_conversational_approval_promotes(self):
        cand = self._pending_candidate()
        ar = self.adm.request_approval(
            {"kind": "candidate", "ref": cand}, "x" * 64)
        out = self.adm.on_master_message("いいよ")
        self.assertTrue(out["matched"])
        self.assertEqual(out["decision"]["status"], "approved")
        self.assertTrue(out["promotion"]["ok"])
        cand_row = self.w._conn.execute(
            "SELECT status FROM candidates WHERE candidate_id=?",
            (cand,)).fetchone()
        self.assertEqual(cand_row["status"], "approved")
        # 昇格は memory_write で刻まれ records に現在値がある
        self.assertIsNotNone(self.w._conn.execute(
            "SELECT 1 FROM records WHERE record_id='mem_t1'").fetchone())
        self.assertTrue(self.w.replay_check()["ok"])

    def test_ambiguous_message_stays_pending(self):
        cand = self._pending_candidate()
        self.adm.request_approval(
            {"kind": "candidate", "ref": cand}, "x" * 64)
        out = self.adm.on_master_message("うーんどうしようかな")
        self.assertFalse(out["matched"])
        cand_row = self.w._conn.execute(
            "SELECT status FROM candidates WHERE candidate_id=?",
            (cand,)).fetchone()
        self.assertEqual(cand_row["status"], "pending")

    def test_structured_decide_stale_inputs(self):
        cand = self._pending_candidate()
        ar = self.adm.request_approval(
            {"kind": "candidate", "ref": cand}, "x" * 64)
        # gate.inputs の tombstone_digest を drift させる
        self._write_tombstone("別の禁止パターン", tid="tmb_drift")
        out = self.adm.decide(ar["approval_id"], "approved", "master")
        self.assertEqual(out["status"], "stale")

    def test_non_master_decide_rejected(self):
        cand = self._pending_candidate()
        ar = self.adm.request_approval(
            {"kind": "candidate", "ref": cand}, "x" * 64)
        with self.assertRaises(self.admission.AdmissionError):
            self.adm.decide(ar["approval_id"], "approved", "companion")


class TestConformance(IngestTestBase):
    def test_report(self):
        self.ik.submit_envelope(self._env())
        self.ik.drain()
        rep = self.ik.conformance_report("test-adapter",
                                         {"conversation", "tool"})
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["undeclared_emitted"], [])

    def test_report_flags_undeclared_emits(self):
        rep = self.ik.conformance_report("test-adapter",
                                         {"conversation", "nope"})
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["undeclared_emitted"], ["nope"])


if __name__ == "__main__":
    unittest.main()
