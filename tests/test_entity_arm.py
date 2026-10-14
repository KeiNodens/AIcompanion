"""C-4 entity 腕（adapters/entities.py + derived entities 索引 +
supply 配線）のテスト

カバー: 明示 entities フィールド・表面抽出（カタカナ/「」）の索引化・
クエリ entity 一致 → 候補化・edges 1ホップ展開・cap→link_expansion_capped・
index_pending 追従・CANONICAL_ENTITY=0 で無効・subject 索引化。
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class EntityTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        cls.writer = cls._iso.import_module("companion.kernel.writer")
        cls.canon = cls._iso.import_module("companion.kernel.canon")
        cls.derived = cls._iso.import_module("companion.adapters.derived")
        cls.supply = cls._iso.import_module("companion.adapters.supply")
        cls.embedder = cls._iso.import_module("companion.adapters.embedder")
        cls.entities = cls._iso.import_module(
            "companion.adapters.entities")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="entity_test_"))
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
        self.sup = self.supply.Supply(self.w, self.ds, embedder=self.emb)

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

    def _write_link(self, lid, src, tgt, scope="user"):
        st = {
            "record_id": lid, "record_family": "link", "scope": scope,
            "status": "active", "record_version": 1,
            "source_id": src, "target_id": tgt, "link_type": "related",
            "created_at": "2026-10-09T10:00:00.000000Z",
            "updated_at": "2026-10-09T10:00:00.000000Z"}
        return self.w.apply_mutation(
            "record_write",
            {"record_family": "link", "record_id": lid,
             "new_state": st, "reason": "test link"},
            actor="companion")

    def _entity_rows(self):
        acon, _ = self.ds.open_active()
        rows = acon.execute(
            "SELECT scope, entity_norm, record_id FROM entities"
            " ORDER BY entity_norm").fetchall()
        acon.close()
        return [(r["entity_norm"], r["record_id"]) for r in rows]


class TestEntityIndex(EntityTestBase):
    def test_surface_extraction_indexed(self):
        self._write_mem("mem_a", "マスターはラーメンが好き")
        self.ds.rebuild(embedder=self.emb)
        ents = dict(self._entity_rows() and
                    [(e, r) for e, r in self._entity_rows()])
        self.assertIn("ラーメン", ents)
        self.assertEqual(ents["ラーメン"], "mem_a")

    def test_explicit_entities_field_indexed(self):
        # 漢字の人名など表面抽出が拾えないものは明示フィールドで入る
        self._write_mem("mem_a", "上司の田中さんに報告した",
                        entities=["田中さん", "上司"])
        self.ds.rebuild(embedder=self.emb)
        ents = {e: r for e, r in self._entity_rows()}
        self.assertEqual(ents.get("田中さん"), "mem_a")

    def test_quoted_and_ascii_indexed(self):
        self._write_mem("mem_a", "「もののけ姫」を見返した")
        self._write_mem("mem_b", "VSCode の設定を直した")
        self.ds.rebuild(embedder=self.emb)
        ents = {e: r for e, r in self._entity_rows()}
        self.assertEqual(ents.get("もののけ姫"), "mem_a")
        self.assertIn("vscode", ents)   # norm_text で小文字化

    def test_subject_indexed(self):
        self._write_mem("mem_a", "アリスの誕生日",
                        subject={"kind": "person", "name": "アリス"})
        self.ds.rebuild(embedder=self.emb)
        ents = {e: r for e, r in self._entity_rows()}
        self.assertEqual(ents.get("アリス"), "mem_a")

    def test_index_pending_updates_entities(self):
        self._write_mem("mem_a", "猫を飼っている")
        self.ds.rebuild(embedder=self.emb)
        self._write_mem("mem_b", "ジローという犬もいる")
        out = self.ds.index_pending(embedder=self.emb)
        self.assertEqual(out["indexed"], 1)
        ents = {e: r for e, r in self._entity_rows()}
        self.assertEqual(ents.get("ジロー"), "mem_b")


class TestEntityRecall(EntityTestBase):
    def test_entity_match_surfaces_memory(self):
        # 「アリス」は mem_a だけが持つ。クエリに含めると entity 腕が候補化
        self._write_mem("mem_a", "アリスは出版会社に勤めている",
                        entities=["アリス"])
        self._write_mem("mem_b", "マスターは映画が好き")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "アリスの知り合いって誰だっけ", 5)
        by_id = {i["record_id"]: i for i in r["items"]}
        self.assertIn("mem_a", by_id)
        self.assertTrue(by_id["mem_a"]["lexical"])
        self.assertEqual(r["stats"]["entity"]["matched"], ["アリス"])
        self.assertEqual(r["stats"]["entity"]["hop1"], 1)

    def test_hop2_via_edges(self):
        # mem_a が entity 一致 → edges で mem_b（entity 未含有）に展開
        self._write_mem("mem_a", "アリスとカフェに行った",
                        entities=["アリス"])
        self._write_mem("mem_b", "カフェのチーズケーキが絶品だった")
        self._write_link("lnk1", "mem_a", "mem_b")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "アリスと何をしたっけ", 5)
        by_id = {i["record_id"]: i for i in r["items"]}
        self.assertIn("mem_a", by_id)
        self.assertIn("mem_b", by_id)
        self.assertEqual(by_id["mem_b"].get("entity_hop"), 2)
        self.assertEqual(r["stats"]["entity"]["hop2"], 1)

    def test_expansion_cap_counts_omitted(self):
        self._write_mem("mem_a", "アリスの話", entities=["アリス"])
        for i in range(12):
            self._write_mem(f"mem_n{i}", f"関係メモ{i}")
            self._write_link(f"lnk{i}", "mem_a", f"mem_n{i}")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "アリスの関連って？", 5)
        self.assertGreaterEqual(
            r["omitted"].get("link_expansion_capped", 0), 1)
        self.assertEqual(r["stats"]["entity"]["hop2"], 10)

    def test_no_match_no_stats(self):
        self._write_mem("mem_a", "普通の記憶")
        self.ds.rebuild(embedder=self.emb)
        r = self.sup._recall("user", "えんたいていの語", 5)
        self.assertNotIn("entity", r["stats"])

    def test_disabled_by_env(self):
        import os
        os.environ["CANONICAL_ENTITY"] = "0"
        try:
            self._write_mem("mem_a", "アリスの話", entities=["アリス"])
            self.ds.rebuild(embedder=self.emb)
            r = self.sup._recall("user", "アリスの話", 5)
            self.assertNotIn("entity", r["stats"])
        finally:
            os.environ.pop("CANONICAL_ENTITY", None)


class TestExtractorEntities(EntityTestBase):
    """C-1 抽出器が entities を proposal に乗せる（索引材料の本線）。"""

    def test_llm_extract_carries_entities(self):
        import json
        llm_extract = self._iso.import_module(
            "companion.adapters.llm_extract")
        m = self.w.append_event(
            "message", {"body": "アリスとランチした"}, actor="master",
            occurred_at="2026-10-09T10:00:00.000000Z",
            correlation={"turn_id": 1})
        ext = llm_extract.LLMExtractor(
            self.w, ask_fn=lambda _p: json.dumps([
                {"text": "マスターはアリスとランチした",
                 "type": "episode", "importance": 2,
                 "entities": ["アリス"]}]))
        props = ext({"seq": m["seq"], "event_id": m["event_id"],
                     "event_type": "message", "actor": "master",
                     "occurred_at": "2026-10-09T10:00:00.000000Z",
                     "payload": {"body": "アリスとランチした"},
                     "source_meta": {}})
        self.assertEqual(props[0]["new_state"]["entities"], ["アリス"])


if __name__ == "__main__":
    unittest.main()
