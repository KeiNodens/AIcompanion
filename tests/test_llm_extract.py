"""C-1 LLM 抽出器（adapters/llm_extract.py）のテスト

カバー: message+response ペアからの型付き抽出・basis にペア response・
valid_from/importance の写像・JSON 不成立の poison・相対時刻残渣の
concreteness pending・非 message event の no_lesson・
default_extractor の設定解決・turn_id 無しペア解決。
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class ExtractTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        cls.writer = cls._iso.import_module("companion.kernel.writer")
        cls.canon = cls._iso.import_module("companion.kernel.canon")
        cls.admission = cls._iso.import_module("companion.adapters.admission")
        cls.extraction = cls._iso.import_module(
            "companion.adapters.extraction")
        cls.llm_extract = cls._iso.import_module(
            "companion.adapters.llm_extract")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llx_test_"))
        self.t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        self._tick = {"i": 0}

        def clock():
            self._tick["i"] += 1
            return self.t0 + timedelta(seconds=self._tick["i"])

        def sclock():
            self._tick["i"] += 1
            return self.canon.canon_ts(
                self.t0 + timedelta(seconds=self._tick["i"]))

        self.w = self.writer.open_core(self.tmp / "core.db", clock=clock)
        self.adm = self.admission.Admission(self.w, clock=sclock)

    def tearDown(self):
        self.w.close()

    def _turn(self, user_body, resp_body, turn_id=1):
        """message+response の2 event を同じ turn_id で append。"""
        m = self.w.append_event(
            "message", {"body": user_body}, actor="master",
            occurred_at="2026-10-09T10:00:00.000000Z",
            correlation={"turn_id": turn_id})
        r = self.w.append_event(
            "response", {"body": resp_body}, actor="companion",
            occurred_at="2026-10-09T10:00:01.000000Z",
            correlation={"turn_id": turn_id})
        return m, r

    def _extractor(self, items):
        """固定 JSON を返す ask_fn 注入の抽出器。"""
        return self.llm_extract.LLMExtractor(
            self.w, model="test-model",
            ask_fn=lambda _p: json.dumps(items, ensure_ascii=False),
            clock=lambda: "2026-10-09T10:00:02.000000Z")


class TestLLMExtractor(ExtractTestBase):
    def test_extracts_typed_fact_through_admission(self):
        self._turn("私はラーメンが好き", "ラーメンいいよね！")
        ex = self.extraction.Extraction(
            self.w, self.adm, extractor=self._extractor([
                {"text": "マスターはラーメンが好き",
                 "type": "preference", "importance": 2}]))
        out = ex.run()
        self.assertEqual(out["proposed"], 1)
        row = self.w._conn.execute(
            "SELECT kind, valid_from, sensitivity, tier, state_json"
            " FROM records").fetchone()
        self.assertEqual(row["kind"], "preference")
        self.assertEqual(
            self.canon.loads(row["state_json"])["content"],
            "マスターはラーメンが好き")
        # 出来事の時刻 = message event の occurred_at
        self.assertEqual(row["valid_from"], "2026-10-09T10:00:00.000000Z")
        self.assertEqual(row["tier"], "hot")
        self.assertTrue(self.w.replay_check()["ok"])

    def test_basis_includes_pair_response(self):
        m, r = self._turn("映画見た", "どうだった？")
        props = self._extractor([
            {"text": "マスターは映画を見た", "type": "episode",
             "importance": 3}])({
            "seq": m["seq"], "event_id": m["event_id"],
            "event_type": "message", "actor": "master",
            "occurred_at": "2026-10-09T10:00:00.000000Z",
            "payload": {"body": "映画見た"}, "source_meta": {}})
        self.assertEqual(len(props), 1)
        basis_ids = {b["event_id"] for b in props[0]["basis_events"]}
        self.assertEqual(basis_ids, {m["event_id"], r["event_id"]})
        self.assertEqual(props[0]["producer"], "extraction")
        self.assertEqual(props[0]["generator"]["method"], "llm")

    def test_mixed_actor_basis_grades_inferred(self):
        # basis が master+companion 混在 → evidence_basis=inferred（正直格付け）
        self._turn("私はラーメンが好き", "わかったよ")
        ex = self.extraction.Extraction(
            self.w, self.adm, extractor=self._extractor([
                {"text": "マスターはラーメンが好き",
                 "type": "fact", "importance": 2}]))
        out = ex.run()
        self.assertEqual(out["verdicts"].get("accept"), 1)
        st = self.canon.loads(self.w._conn.execute(
            "SELECT state_json FROM records").fetchone()["state_json"])
        self.assertEqual(
            (st.get("provenance") or {}).get("evidence_basis"), "inferred")

    def test_empty_items_give_no_lesson(self):
        self._turn("おはよう", "おはよう！")
        ex = self.extraction.Extraction(
            self.w, self.adm, extractor=self._extractor([]))
        out = ex.run()
        self.assertEqual(out["proposed"], 0)
        # response event も [] で処理されるので claims は両方 no_lesson
        st = {r["status"] for r in self.w._conn.execute(
            "SELECT status FROM claims")}
        self.assertEqual(st, {"no_lesson"})

    def test_non_message_events_extract_nothing(self):
        ext = self._extractor([
            {"text": "抽出されるべきでない", "type": "fact",
             "importance": 1}])
        for et in ("response", "inner", "tool_result"):
            self.assertEqual(ext({"event_type": et, "payload": {}}), [])

    def test_high_importance_pends_review(self):
        # importance 4+ は review.required → pending（設計通りの門）
        self._turn("結婚が決まったんだ", "おめでとう！")
        ex = self.extraction.Extraction(
            self.w, self.adm, extractor=self._extractor([
                {"text": "マスターの結婚が決まった",
                 "type": "episode", "importance": 5}]))
        out = ex.run()
        self.assertEqual(out["verdicts"].get("pending"), 1)
        cand = self.w._conn.execute(
            "SELECT status FROM candidates").fetchone()
        self.assertEqual(cand["status"], "pending")

    def test_relative_time_residue_pends(self):
        # LLM が絶対日付化を怠ると concreteness が pending にする（門が働く）
        self._turn("明日映画を見に行く", "楽しんでね")
        ex = self.extraction.Extraction(
            self.w, self.adm, extractor=self._extractor([
                {"text": "マスターは明日映画を見に行く",
                 "type": "intention", "importance": 3}]))
        out = ex.run()
        self.assertEqual(out["verdicts"].get("pending"), 1)

    def test_bad_json_is_poison_path(self):
        def boom(_p):
            return "json ではない出力"
        ex = self.extraction.Extraction(
            self.w, self.adm,
            extractor=self.llm_extract.LLMExtractor(
                self.w, ask_fn=boom))
        self.w.append_event("message", {"body": "x"}, actor="master")
        total = 0
        for _ in range(4):  # POISON_MAX_ATTEMPTS=3 を超えるまで
            total += ex.run()["poisoned"]
        self.assertEqual(total, 1)
        self.assertIsNotNone(self.w._conn.execute(
            "SELECT 1 FROM events WHERE event_type='extraction_poisoned'"
        ).fetchone())

    def test_max_facts_cap(self):
        items = [{"text": f"記憶{i}", "type": "fact", "importance": 1}
                 for i in range(10)]
        ext = self.llm_extract.LLMExtractor(
            self.w, ask_fn=lambda _p: json.dumps(items), max_facts=3)
        m, _ = self._turn("いろいろ話した", "うん")
        props = ext({"seq": m["seq"], "event_id": m["event_id"],
                     "event_type": "message", "actor": "master",
                     "occurred_at": "2026-10-09T10:00:00.000000Z",
                     "payload": {"body": "いろいろ話した"},
                     "source_meta": {}})
        self.assertEqual(len(props), 3)

    def test_unknown_type_falls_back_to_fact(self):
        ext = self.llm_extract.LLMExtractor(
            self.w, ask_fn=lambda _p: json.dumps([
                {"text": "なにか", "type": "bogus_type", "importance": 1}]))
        m, _ = self._turn("なにか話した", "うん")
        props = ext({"seq": m["seq"], "event_id": m["event_id"],
                     "event_type": "message", "actor": "master",
                     "occurred_at": "2026-10-09T10:00:00.000000Z",
                     "payload": {"body": "x"}, "source_meta": {}})
        self.assertEqual(props[0]["new_state"]["memory_type"], "fact")

    def test_no_turn_id_still_extracts(self):
        # turn_id が無い（直接 append 等）でも message 単体で抽出は動く
        m = self.w.append_event(
            "message", {"body": "単独発言"}, actor="master",
            occurred_at="2026-10-09T10:00:00.000000Z")
        ext = self._extractor([
            {"text": "マスターは単独で話した", "type": "episode",
             "importance": 1}])
        props = ext({"seq": m["seq"], "event_id": m["event_id"],
                     "event_type": "message", "actor": "master",
                     "occurred_at": "2026-10-09T10:00:00.000000Z",
                     "payload": {"body": "単独発言"}, "source_meta": {}})
        self.assertEqual(len(props), 1)
        # ペア無し → basis は message のみ
        self.assertEqual(len(props[0]["basis_events"]), 1)

    def test_occurred_at_lands_in_state(self):
        # event-time: 出来事の日付が occurred_at に乗る
        m, _ = self._turn("先週花火見た", "いいなあ")
        props = self._extractor([
            {"text": "マスターは2026-10-03に花火を見た",
             "type": "episode", "importance": 3,
             "occurred_at": "2026-10-03"}])(
            {"seq": m["seq"], "event_id": m["event_id"],
             "event_type": "message", "actor": "master",
             "occurred_at": "2026-10-09T10:00:00.000000Z",
             "payload": {"body": "先週花火見た"}, "source_meta": {}})
        st = props[0]["new_state"]
        # 記録時刻（発言日）とは別に、出来事の時刻が保持される
        self.assertEqual(st["occurred_at"], "2026-10-03T00:00:00.000000Z")
        self.assertEqual(st["valid_from"], "2026-10-09T10:00:00.000000Z")

    def test_occurred_at_granularity(self):
        # 荒い粒度は期間先頭に畳まれる（自身の月・年の窓に必ず乗る）
        norm = self.llm_extract._norm_occurred
        self.assertEqual(norm("2026"), "2026-01-01T00:00:00.000000Z")
        self.assertEqual(norm("2026-08"), "2026-08-01T00:00:00.000000Z")
        self.assertEqual(norm("2026-08-15"),
                         "2026-08-15T00:00:00.000000Z")
        self.assertIsNone(norm(None))
        self.assertIsNone(norm("null"))
        self.assertIsNone(norm("昨日"))       # 相対語のままは受理しない
        self.assertIsNone(norm("2026-13"))    # 不正な月
        self.assertIsNone(norm("garbage"))

    def test_occurred_at_null_omitted_from_state(self):
        # null なら state にキー自体を置かない（fallback が効く）
        m, _ = self._turn("ラーメン好き", "だよね")
        props = self._extractor([
            {"text": "マスターはラーメンが好き",
             "type": "preference", "importance": 2,
             "occurred_at": None}])(
            {"seq": m["seq"], "event_id": m["event_id"],
             "event_type": "message", "actor": "master",
             "occurred_at": "2026-10-09T10:00:00.000000Z",
             "payload": {"body": "ラーメン好き"}, "source_meta": {}})
        self.assertNotIn("occurred_at", props[0]["new_state"])


class TestDefaultResolution(ExtractTestBase):
    def test_default_is_verbatim(self):
        import os
        os.environ.pop("CANONICAL_EXTRACTOR", None)
        ext = self.llm_extract.default_extractor(self.w)
        from companion.adapters import dualrun
        self.assertIs(ext, dualrun.verbatim_extractor)

    def test_llm_flag_resolves_llm_extractor(self):
        import os
        os.environ["CANONICAL_EXTRACTOR"] = "llm"
        try:
            ext = self.llm_extract.default_extractor(self.w)
            self.assertIsInstance(ext, self.llm_extract.LLMExtractor)
        finally:
            os.environ.pop("CANONICAL_EXTRACTOR", None)

    def test_llm_flag_without_writer_falls_back(self):
        import os
        os.environ["CANONICAL_EXTRACTOR"] = "llm"
        try:
            ext = self.llm_extract.default_extractor(None)
            from companion.adapters import dualrun
            self.assertIs(ext, dualrun.verbatim_extractor)
        finally:
            os.environ.pop("CANONICAL_EXTRACTOR", None)


if __name__ == "__main__":
    unittest.main()
