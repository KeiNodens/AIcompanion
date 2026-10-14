"""C-2 時間腕（adapters/temporal.py + supply 配線）のテスト

カバー: 決定的パーサ（明示日付・M月・相対語・週/月/年・季節・曖昧語・
未検出 None）・recall 統合（窓内のみ候補化・lexical 信号・stats 刻印・
CANONICAL_TEMPORAL=0 で無効・valid_from 優先）。
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

NOW = "2026-10-09T12:00:00.000000Z"   # 2026-10-09 (金) 基準


class TemporalTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        cls.writer = cls._iso.import_module("companion.kernel.writer")
        cls.canon = cls._iso.import_module("companion.kernel.canon")
        cls.derived = cls._iso.import_module("companion.adapters.derived")
        cls.supply = cls._iso.import_module("companion.adapters.supply")
        cls.embedder = cls._iso.import_module("companion.adapters.embedder")
        cls.temporal = cls._iso.import_module(
            "companion.adapters.temporal")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="temporal_test_"))
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
        self.ds = self.derived.DerivedStore(self.w, dir=self.tmp,
                                          clock=sclock)
        self.emb = self.embedder.TrigramHashEmbedder(dims=384)
        self.sup = self.supply.Supply(self.w, self.ds, embedder=self.emb,
                                      clock=lambda: NOW)

    def tearDown(self):
        self.w.close()

    def _write_mem(self, rid, content, scope="user", **kw):
        st = {
            "record_id": rid, "record_family": "memory", "scope": scope,
            "status": "active", "record_version": 1, "memory_type": "fact",
            "content": content,
            "content_hash": self.canon.content_hash(content),
            "importance": 2, "retrieval_strength": 0.5,
            "created_at": "2026-10-09T10:00:00.000000Z",
            "updated_at": "2026-10-09T10:00:00.000000Z",
            **kw}
        return self.w.apply_mutation(
            "memory_write",
            {"memory_id": rid, "new_state": st, "reason": "test"},
            actor="companion")


class TestParser(TemporalTestBase):
    def _tw(self, q):
        return self.temporal.parse_time_window(q, NOW)

    def test_explicit_full_date(self):
        tw = self._tw("2024年5月3日に何してた？")
        self.assertEqual(tw["start"], "2024-05-03T00:00:00.000000Z")
        self.assertEqual(tw["end"], "2024-05-04T00:00:00.000000Z")

    def test_explicit_year_month(self):
        tw = self._tw("2024年5月の出来事")
        self.assertEqual(tw["start"], "2024-05-01T00:00:00.000000Z")
        self.assertEqual(tw["end"], "2024-06-01T00:00:00.000000Z")

    def test_explicit_year(self):
        tw = self._tw("2024年はどんな年だった？")
        self.assertEqual(tw["start"], "2024-01-01T00:00:00.000000Z")
        self.assertEqual(tw["end"], "2025-01-01T00:00:00.000000Z")

    def test_month_only_past_interpretation(self):
        # 10月時点で「11月」は未来 → 前年の11月
        tw = self._tw("11月に何かあったっけ")
        self.assertEqual(tw["start"], "2025-11-01T00:00:00.000000Z")
        self.assertEqual(tw["end"], "2025-12-01T00:00:00.000000Z")
        # 「5月」は今年の過去
        tw = self._tw("5月の話")
        self.assertEqual(tw["start"], "2026-05-01T00:00:00.000000Z")

    def test_relative_days(self):
        self.assertEqual(self._tw("昨日の話")["start"],
                         "2026-10-08T00:00:00.000000Z")
        self.assertEqual(self._tw("一昨日")["start"],
                         "2026-10-07T00:00:00.000000Z")
        self.assertEqual(self._tw("3日前に言ったこと")["start"],
                         "2026-10-06T00:00:00.000000Z")

    def test_week_month_year_words(self):
        tw = self._tw("先週やったこと")
        self.assertEqual(tw["start"], "2026-09-28T00:00:00.000000Z")
        self.assertEqual(tw["end"], "2026-10-05T00:00:00.000000Z")
        tw = self._tw("先月の記憶")
        self.assertEqual(tw["start"], "2026-09-01T00:00:00.000000Z")
        self.assertEqual(tw["end"], "2026-10-01T00:00:00.000000Z")
        tw = self._tw("去年行った場所")
        self.assertEqual(tw["start"], "2025-01-01T00:00:00.000000Z")
        self.assertEqual(tw["end"], "2026-01-01T00:00:00.000000Z")
        tw = self._tw("3年前のこと")
        self.assertEqual(tw["start"], "2023-01-01T00:00:00.000000Z")

    def test_season(self):
        # 10月時点で「春」→ 今年の3-5月
        tw = self._tw("春に何してた？")
        self.assertEqual(tw["start"], "2026-03-01T00:00:00.000000Z")
        self.assertEqual(tw["end"], "2026-06-01T00:00:00.000000Z")
        # 「冬」→ 直前の冬 = 前年12月〜当年2月
        tw = self._tw("冬の思い出")
        self.assertEqual(tw["start"], "2025-12-01T00:00:00.000000Z")
        self.assertEqual(tw["end"], "2026-03-01T00:00:00.000000Z")
        # 年指定季節
        tw = self._tw("2024年の夏")
        self.assertEqual(tw["start"], "2024-06-01T00:00:00.000000Z")
        self.assertEqual(tw["end"], "2024-09-01T00:00:00.000000Z")

    def test_vague_recent(self):
        tw = self._tw("最近どう？")
        self.assertEqual(tw["label"], "最近30日")
        self.assertEqual(tw["end"], NOW)

    def test_no_temporal_returns_none(self):
        self.assertIsNone(self._tw("ラーメン食べたい"))
        self.assertIsNone(self._tw(""))
        self.assertIsNone(self._tw(None))

    def test_fullwidth_digits(self):
        tw = self._tw("２０２４年の出来事")
        self.assertEqual(tw["start"], "2024-01-01T00:00:00.000000Z")


class TestTemporalRecall(TemporalTestBase):
    def test_window_surfaces_old_memory(self):
        # 去年の記憶（本文に「去年」は含まない → FTS/dense では拾えない）
        self._write_mem("mem_old", "沖縄旅行に行った",
                        valid_from="2025-06-15T10:00:00.000000Z")
        self._write_mem("mem_new", "最近の話",
                        valid_from="2026-10-01T10:00:00.000000Z")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "去年どこか行った？", 5)
        ids = [i["record_id"] for i in r["items"]]
        self.assertIn("mem_old", ids)
        # union 腕なので窓外の記憶は dense 経路で同乗しうるが、
        # 時間腕が効いた mem_old は lexical 信号を持ち順位が上がる
        old = [i for i in r["items"] if i["record_id"] == "mem_old"][0]
        self.assertTrue(old["lexical"])
        new = [i for i in r["items"] if i["record_id"] == "mem_new"][0]
        self.assertFalse(new["lexical"])
        self.assertGreater(old["score"], new["score"])
        self.assertEqual(r["stats"]["temporal"]["hits"], 1)
        self.assertEqual(r["stats"]["temporal"]["label"], "去年")

    def test_temporal_marks_lexical_signal(self):
        self._write_mem("mem_old", "海に行った",
                        valid_from="2025-08-10T10:00:00.000000Z")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "去年の夏に何してた？", 5)
        item = [i for i in r["items"] if i["record_id"] == "mem_old"][0]
        self.assertTrue(item["lexical"])

    def test_valid_from_preferred_over_created(self):
        # created_at は現在、valid_from は過去 → 去年窓に乗る
        self._write_mem("mem_vf", "昔の登録情報",
                        valid_from="2025-03-01T00:00:00.000000Z")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "去年の記録", 5)
        ids = [i["record_id"] for i in r["items"]]
        self.assertIn("mem_vf", ids)

    def test_created_at_fallback(self):
        # valid_from 無し → created_at 列で窓判定
        self._write_mem("mem_ca", "今日の記憶")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "今日の記憶", 5)
        ids = [i["record_id"] for i in r["items"]]
        self.assertIn("mem_ca", ids)

    def test_occurred_at_overrides_valid_from(self):
        # 出来事は先月・記録は今日 → occurred_at で先月窓に乗る
        # （「先週の話」を今日記録しても取りこぼさない event-time）
        self._write_mem("mem_ev", "先月の花火大会に行った",
                        valid_from="2026-10-09T10:00:00.000000Z",
                        occurred_at="2026-09-12T18:00:00.000000Z")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "先月の出来事", 5)
        ev = [i for i in r["items"] if i["record_id"] == "mem_ev"]
        self.assertTrue(ev)
        self.assertTrue(ev[0]["temporal"])
        self.assertEqual(r["stats"]["temporal"]["label"], "先月")

    def test_occurred_at_null_falls_back(self):
        # occurred_at 無しの記憶は従来通り valid_from で窓判定される
        # （null 行を黙って落とさない fallback 連結の検証）
        self._write_mem("mem_nf", "昔の記憶",
                        valid_from="2025-06-15T10:00:00.000000Z")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "去年の記憶", 5)
        item = [i for i in r["items"] if i["record_id"] == "mem_nf"]
        self.assertTrue(item and item[0]["temporal"])

    def test_coverage_buckets_spread_window(self):
        # 1年窓に月別記憶（クエリと trigram が被らない本文）。
        # 新しい順 LIMIT なら古い端は切り捨てられるが、8バケットの
        # round-robin で窓の古い側（1月）も候補化される
        for i in range(10):
            m = i + 1
            self._write_mem(f"mem_{m:02d}", f"{m}月の記録",
                            valid_from=f"2026-{m:02d}-15T10:00:00"
                                       ".000000Z")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "今年の出来事", 6)
        picked = {i["record_id"] for i in r["items"]
                  if i.get("temporal")}
        self.assertIn("mem_01", picked)

    def test_coverage_disabled_by_env(self):
        import os
        os.environ["CANONICAL_TEMPORAL_COVERAGE"] = "0"
        try:
            for i in range(10):
                m = i + 1
                self._write_mem(f"mem_{m:02d}", f"{m}月の記録",
                                valid_from=f"2026-{m:02d}-15T10:00:00"
                                           ".000000Z")
            self.ds.rebuild(embedder=self.emb)
            # k*3=18 > 10 件なのでここでは全件候補化される。
            # 代わりに小さい cap で新旧の取りこぼし差を見る
            r = self.sup._recall("user", "今年の出来事", 1)
            picked = {i["record_id"] for i in r["items"]
                      if i.get("temporal")}
            # newest-only LIMIT 3 → 1月（mem_01）は temporal 未選出
            self.assertNotIn("mem_01", picked)
        finally:
            os.environ.pop("CANONICAL_TEMPORAL_COVERAGE", None)

    def test_disabled_by_env(self):
        import os
        os.environ["CANONICAL_TEMPORAL"] = "0"
        try:
            self._write_mem("mem_old", "沖縄旅行に行った",
                            valid_from="2025-06-15T10:00:00.000000Z")
            self.ds.rebuild(embedder=self.emb)
            r = self.sup._recall("user", "去年どこか行った？", 5)
            self.assertNotIn("temporal", r["stats"])
        finally:
            os.environ.pop("CANONICAL_TEMPORAL", None)

    def test_no_temporal_stats_without_window(self):
        self._write_mem("mem_a", "普通の記憶")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "普通の記憶", 5)
        self.assertNotIn("temporal", r["stats"])


if __name__ == "__main__":
    unittest.main()
