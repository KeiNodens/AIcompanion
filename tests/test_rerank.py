"""C-3 cross-encoder 後段再採点（adapters/rerank.py + supply 配線）のテスト

カバー: CE スコアによる上位プール再整列・rerank_score/stats の刻印・
reranker 障害時の score-v1 fallback・reranker=None の無変換・
abstention 判定の非依存・supply_context receipt 整合。
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class StubReranker:
    """scripted スコアを返す reranker スタブ。"""

    def __init__(self, scores=None, fail=False):
        self.scores = scores or []
        self.fail = fail
        self.calls = []

    def model_stamp(self):
        return {"model": "stub-ce", "type": "cross-encoder"}

    def rerank(self, query, texts):
        self.calls.append((query, list(texts)))
        if self.fail:
            raise RuntimeError("ce down")
        # texts 順に scores を割り当てる（足りなければ 0 で埋める）
        return [self.scores[i] if i < len(self.scores) else 0.0
                for i in range(len(texts))]


class RerankTestBase(unittest.TestCase):
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
        self.tmp = Path(tempfile.mkdtemp(prefix="rerank_test_"))
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

    def _supply(self, reranker=None):
        return self.supply.Supply(self.w, self.ds, embedder=self.emb,
                                  reranker=reranker)


class TestRerank(RerankTestBase):
    def setUp(self):
        super().setUp()
        # lexical に全部ヒットさせて候補 union に乗せる
        self._write_mem("mem_a", "マスターはラーメンが好き")
        self._write_mem("mem_b", "マスターはラーメンを週3食べる")
        self._write_mem("mem_c", "マスターはラーメン二郎派")
        self.ds.rebuild(embedder=self.emb)

    def _order(self, items):
        return [i["record_id"] for i in items]

    def test_ce_reorders_top_pool(self):
        # score-v1 順を CE が逆順に組み替える（b が最上位に）
        rk = StubReranker(scores=[0.1, 0.9, 0.5])
        r = self._supply(rk)._recall("user", "ラーメン", 3)
        ids = self._order(r["items"])
        # CE は score-v1 順プール [?] に scores を割当てる。
        # プール先頭 (=score-v1 1位) が 0.1、2番目が 0.9 → CE 後は
        # 2番目の record が先頭に来る
        first = rk.calls[0][1]
        self.assertEqual(len(first), 3)
        self.assertEqual(ids[0],
                         # 0.9 を取ったのは score-v1 2位の record
                         rk.calls and self._pre_ce_second(rk) or None)

    def _pre_ce_second(self, rk):
        # CE に渡された texts[1] に対応する record を state から逆引き
        sup_norank = self._supply(None)._recall("user", "ラーメン", 3)
        return self._order(sup_norank["items"])[1]

    def test_rerank_score_and_stats_stamped(self):
        rk = StubReranker(scores=[0.8, 0.7, 0.6])
        r = self._supply(rk)._recall("user", "ラーメン", 3)
        self.assertIn("rerank", r["stats"])
        self.assertEqual(r["stats"]["rerank"]["model"]["model"], "stub-ce")
        self.assertEqual(r["stats"]["rerank"]["pool"], 3)
        self.assertTrue(all("rerank_score" in i for i in r["items"]))

    def test_reranker_failure_keeps_score_v1_order(self):
        rk = StubReranker(fail=True)
        r_fail = self._supply(rk)._recall("user", "ラーメン", 3)
        r_none = self._supply(None)._recall("user", "ラーメン", 3)
        self.assertEqual(self._order(r_fail["items"]),
                         self._order(r_none["items"]))
        self.assertNotIn("rerank", r_fail["stats"])

    def test_no_reranker_no_marks(self):
        r = self._supply(None)._recall("user", "ラーメン", 3)
        self.assertFalse(any("rerank_score" in i for i in r["items"]))
        self.assertNotIn("rerank", r["stats"])

    def test_abstain_unaffected_by_reranker(self):
        # dense floor 未満の候補は棄権 —— CE は棄権判定を変えず、
        # 棄権候補に CE 計算も掛けない
        rk = StubReranker(scores=[0.9])
        r = self._supply(rk)._recall("user", "qqqqqq", 3)
        self.assertEqual(r["items"], [])
        self.assertEqual(r["abstain"], "below_threshold")
        self.assertEqual(rk.calls, [])

    def test_supply_context_receipt_with_rerank(self):
        rk = StubReranker(scores=[0.9, 0.8, 0.7])
        out = self._supply(rk).supply_context(
            scopes=["user"], query="ラーメン", purpose="turn")
        self.assertTrue(out["receipt"]["accepted"])
        ev = self.w._conn.execute(
            "SELECT payload_json FROM events WHERE event_type="
            "'context_assembly' ORDER BY seq DESC LIMIT 1").fetchone()
        pl = self.canon.loads(ev["payload_json"])
        injected = [i["id"] for i in pl["injected"]]
        # score-v1 順位を CE が保った場合の先頭は score-v1 1位相当
        self.assertTrue(any(i in injected for i in
                            ("mem_a", "mem_b", "mem_c")))


if __name__ == "__main__":
    unittest.main()
