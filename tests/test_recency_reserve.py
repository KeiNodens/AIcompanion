"""recency reserve（adapters/supply.py）のテスト

Mem++ event-reserve 相当: rank で負けても scope 内最新の記憶が
予約枠で必ず供給面に残る機械的保証。

カバー: 新記憶が予約枠で供給される・admit フィルタを潜る・
canonical フォールバックで枠が埋まる・CANONICAL_RECENCY_RESERVE=0
で無効・abstain 時は蘇らせない。
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

NOW = "2026-10-09T12:00:00.000000Z"


class RecencyReserveBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        cls.writer = cls._iso.import_module("companion.kernel.writer")
        cls.canon = cls._iso.import_module("companion.kernel.canon")
        cls.derived = cls._iso.import_module("companion.adapters.derived")
        cls.supply = cls._iso.import_module("companion.adapters.supply")
        cls.embedder = cls._iso.import_module("companion.adapters.embedder")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="reserve_test_"))
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
        os.environ.pop("CANONICAL_RECENCY_RESERVE", None)

    def _write_mem(self, rid, content, scope="user", **kw):
        st = {
            "record_id": rid, "record_family": "memory", "scope": scope,
            "status": "active", "record_version": 1,
            "memory_type": "fact", "content": content,
            "content_hash": self.canon.content_hash(content),
            "importance": 2, "retrieval_strength": 0.5,
            "created_at": "2026-10-09T10:00:00.000000Z",
            "updated_at": "2026-10-09T10:00:00.000000Z",
            **kw}
        return self.w.apply_mutation(
            "memory_write",
            {"memory_id": rid, "new_state": st, "reason": "test"},
            actor="companion")


class TestRecencyReserve(RecencyReserveBase):
    def test_newest_supplied_despite_score_loss(self):
        # クエリに強く合う古い記憶 5 件 + クエリと無関係な最新記憶。
        # k=3 では最新記憶は rank 負けするが、予約枠で供給される
        for i in range(5):
            self._write_mem(f"mem_old{i}", "珈琲の好みを聞かれた",
                            valid_from="2026-09-01T10:00:00.000000Z")
        self._write_mem("mem_new", "傘を新調した",
                        valid_from="2026-10-09T10:00:00.000000Z")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "珈琲の好みは？", 3)
        ids = [i["record_id"] for i in r["items"]]
        self.assertIn("mem_new", ids)
        new = [i for i in r["items"]
               if i["record_id"] == "mem_new"][0]
        self.assertTrue(new["recency_reserve"])
        # 予約枠は枠数まで新しい順に埋まる（rank 負けの残りも載る）
        self.assertEqual(r["stats"]["recency_reserve"], 3)

    def test_reserve_skips_non_visible(self):
        # supersede 済みの記憶は admit を通らず、次に新しいものが拾われる
        self._write_mem("mem_sup", "古い住所",
                        valid_from="2026-10-09T11:00:00.000000Z",
                        status="superseded")
        self._write_mem("mem_alive", "新しい住所",
                        valid_from="2026-10-09T10:00:00.000000Z")
        self._write_mem("mem_rel", "珈琲の好みを聞かれた",
                        valid_from="2026-09-01T10:00:00.000000Z")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "珈琲の好みを聞かれた", 1)
        ids = [i["record_id"] for i in r["items"]]
        self.assertNotIn("mem_sup", ids)
        # 生きている最新記憶が予約枠に入る
        self.assertIn("mem_alive", ids)

    def test_reserve_disabled_by_env(self):
        os.environ["CANONICAL_RECENCY_RESERVE"] = "0"
        for i in range(5):
            self._write_mem(f"mem_old{i}", "珈琲の好みを聞かれた",
                            valid_from="2026-09-01T10:00:00.000000Z")
        self._write_mem("mem_new", "傘を新調した",
                        valid_from="2026-10-09T10:00:00.000000Z")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "珈琲の好みは？", 3)
        ids = [i["record_id"] for i in r["items"]]
        self.assertNotIn("mem_new", ids)
        self.assertNotIn("recency_reserve", r["stats"])

    def test_no_reserve_on_abstain(self):
        # 無関係クエリで棄権なら、予約枠で記憶を蘇らせない
        self._write_mem("mem_a", " unrelated zzz ",
                        valid_from="2026-10-09T10:00:00.000000Z")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "qqq", 3)
        if r["abstain"] is not None:
            self.assertFalse(any(i.get("recency_reserve")
                                 for i in r["items"]))


class TestEmbedDatePrefix(RecencyReserveBase):
    """embed 入力文への日付前置（Hindsight temporal awareness）。"""

    class _SpyEmb:
        dims = 384
        space = "spy"

        def __init__(self):
            self.seen = []

        def embed(self, texts):
            self.seen += list(texts)
            return [[0.1] * 384 for _ in texts]

        def model_stamp(self):
            return "spy-v1"

    def test_embed_text_has_date_prefix(self):
        spy = self._SpyEmb()
        self._write_mem("mem_d", "花火大会に行った",
                        occurred_at="2026-09-12T18:00:00.000000Z")
        self.ds.rebuild(embedder=spy)
        self.assertTrue(spy.seen)
        self.assertTrue(all(t.startswith("2026-09-12 ")
                            for t in spy.seen))

    def test_embed_prefix_disabled(self):
        os.environ["CANONICAL_EMBED_DATES"] = "0"
        try:
            spy = self._SpyEmb()
            self._write_mem("mem_d", "花火大会に行った",
                            occurred_at="2026-09-12T18:00:00.000000Z")
            self.ds.rebuild(embedder=spy)
            self.assertTrue(all(t == "花火大会に行った"
                                for t in spy.seen))
        finally:
            os.environ.pop("CANONICAL_EMBED_DATES", None)


if __name__ == "__main__":
    unittest.main()
