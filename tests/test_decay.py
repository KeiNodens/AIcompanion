"""ステップ5B: 自己記憶の減衰ポリシー（最終想起起点・猶予・増分減衰）のテスト。

companion.memory を偽mem0でimportし、metadata の last_recalled_at /
policy_started_at / last_decay_at に基づく判定を確認する。
本番のmem0/Qdrantには触れない。
"""
import asyncio
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support


class FakeMem0:
    """mem0 の Memory 相当品の最小スタブ。metadata 更新だけ記録する"""

    def __init__(self, items=None):
        self.items = items or []
        self.updated = []
        self.added = []

    def get_all(self, filters=None, top_k=100):
        return {"results": list(self.items)}

    def update(self, mid, data=None, metadata=None):
        self.updated.append({"id": mid, "metadata": dict(metadata or {})})
        for it in self.items:
            if it.get("id") == mid:
                it["metadata"] = {**(it.get("metadata") or {}), **(metadata or {})}

    def add(self, messages, agent_id=None, infer=False, metadata=None):
        self.added.append({"messages": messages, "metadata": dict(metadata or {})})
        return {"id": "new"}


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _item(mid, confidence=0.6, importance=3, **meta):
    m = {"importance": importance, "confidence": confidence}
    m.update(meta)
    return {"id": mid, "memory": f"記憶{mid}", "metadata": m}


def _mem0_stub():
    """from mem0 import Memory を通すための最小モジュール"""
    mod = types.ModuleType("mem0")

    class Memory:
        @classmethod
        def from_config(cls, cfg):
            raise AssertionError("import時に実体が作られてはいけない")

    mod.Memory = Memory
    return mod


class DecayTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="companion_mem_")
        self.store = FakeMem0()
        self._iso = _support.IsolatedModules(
            self._tmp.name, stubs={"mem0": _mem0_stub()}
        ).__enter__()
        self.mem = self._iso.import_module("companion.memory")
        # 遅延プロキシの実体を偽ストアに差し替え（Qdrant/mem0を開かない）
        self.mem.memory._impl = self.store

    def tearDown(self):
        self._iso.__exit__(None, None, None)
        self._tmp.cleanup()

    # --- _decay_targets の判定 ---

    def test_recently_recalled_is_untouched(self):
        items = [_item("a", last_recalled_at=_iso(2))]
        targets = self.mem._decay_targets(items)
        self.assertEqual(targets, [])

    def test_importance_extends_grace(self):
        # importance 1 → 猶予7日 / importance 5 → 35日
        low = _item("low", importance=1, last_recalled_at=_iso(10))
        high = _item("high", importance=5, last_recalled_at=_iso(10))
        targets = {t["id"]: t for t in self.mem._decay_targets([low, high])}
        self.assertIn("low", targets)
        self.assertNotIn("high", targets)
        self.assertLess(targets["low"]["new_confidence"], 0.6)

    def test_unknown_recall_time_is_not_punished(self):
        # last_recalled_at も policy_started_at も無い既存記憶は対象外
        # （作成日を想起日と誤認して一気に減衰させないための保護）
        items = [_item("legacy", confidence=0.8, importance=1)]
        self.assertEqual(self.mem._decay_targets(items), [])

    def test_policy_start_is_anchor_for_migrated(self):
        items = [_item("m", importance=1, policy_started_at=_iso(10))]
        targets = self.mem._decay_targets(items)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["id"], "m")

    def test_floor_limits_decay(self):
        items = [_item("f", confidence=0.06, importance=1, last_recalled_at=_iso(200))]
        targets = self.mem._decay_targets(items)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["new_confidence"], self.mem._DECAY_FLOOR)

    # --- demote_stale の増分性（二重減衰しない） ---

    def test_demote_is_incremental(self):
        self.store.items = [_item("d", importance=1, last_recalled_at=_iso(20))]
        n1 = asyncio.run(self.mem.demote_stale())
        self.assertEqual(n1, 1)
        conf_after_1 = self.store.items[0]["metadata"]["confidence"]
        self.assertLess(conf_after_1, 0.6)
        self.assertIn("last_decay_at", self.store.items[0]["metadata"])
        # 直後に再実行しても last_decay_at からの経過が≈0なので追加減衰しない
        n2 = asyncio.run(self.mem.demote_stale())
        self.assertEqual(n2, 0)
        self.assertEqual(self.store.items[0]["metadata"]["confidence"], conf_after_1)

    def test_recall_stops_decay(self):
        # 減衰後に想起された記憶は猶予が再び効く
        item = _item("r", importance=1, last_recalled_at=_iso(20))
        self.store.items = [item]
        asyncio.run(self.mem.demote_stale())
        conf = item["metadata"]["confidence"]
        item["metadata"]["last_recalled_at"] = _iso(1)
        asyncio.run(self.mem.demote_stale())
        self.assertEqual(item["metadata"]["confidence"], conf)

    def test_simulate_writes_nothing(self):
        self.store.items = [_item("s", importance=1, last_recalled_at=_iso(30))]
        report = asyncio.run(self.mem.simulate_decay())
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["id"], "s")
        self.assertIn("new_confidence", report[0])
        self.assertEqual(self.store.updated, [])
        self.assertNotIn("last_decay_at", self.store.items[0]["metadata"])

    def test_migrate_stamps_policy_start_once(self):
        self.store.items = [
            _item("x"),
            _item("y", policy_started_at=_iso(5)),
        ]
        res = self.mem.migrate_decay_policy()
        self.assertEqual(res, {"stamped": 1, "total": 2})
        self.assertIn("policy_started_at", self.store.items[0]["metadata"])
        self.assertNotIn("last_recalled_at", self.store.items[0]["metadata"])
        res2 = self.mem.migrate_decay_policy()
        self.assertEqual(res2["stamped"], 0)

    def test_new_note_sets_last_recalled(self):
        self.mem.add_self_note("新しい経験", importance=3)
        meta = self.store.added[0]["metadata"]
        self.assertIn("last_recalled_at", meta)
        self.assertEqual(meta["importance"], 3)

    def test_boost_sets_last_recalled(self):
        item = _item("b", confidence=0.5, last_recalled_at=_iso(50))
        self.mem._boost_confidence(item)
        meta = self.store.updated[0]["metadata"]
        self.assertGreater(meta["confidence"], 0.5)
        # 想起日時が更新されて古いまま残らない
        recalled = datetime.fromisoformat(meta["last_recalled_at"])
        self.assertLess((datetime.now(timezone.utc) - recalled).days, 1)


if __name__ == "__main__":
    unittest.main()
