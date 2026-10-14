"""kernel.canon の golden-byte・受理値域テスト（P8-1）

直列化規則の byte 同一性を fixture で固定する。規則を変えるときは
SERIALIZER_VERSION の bump とこの fixture の意図的更新を伴わせる
（Q-O65: Python/Unicode 版跨ぎの安定性をこの golden で検出する）。
"""
import math
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402


class CanonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._iso = _support.IsolatedModules().__enter__()
        cls.canon = cls._iso.import_module("companion.kernel.canon")

    @classmethod
    def tearDownClass(cls):
        cls._iso.__exit__(None, None, None)

    def test_golden_dump(self):
        """固定 fixture の canonical 直列化を byte 単位で pin する。"""
        c = self.canon
        fix = {
            "b": 1,
            "a": [True, None, -0.0],
            "日本語": "テスト",
            "nested": {"z": 1.5, "a": "x"},
            "t": datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc),
        }
        self.assertEqual(
            c.dumps(fix),
            '{"a":[true,null,0.0],"b":1,"nested":{"a":"x","z":1.5},'
            '"t":"2026-01-02T03:04:05.678901Z","日本語":"テスト"}')
        self.assertEqual(
            c.hash_obj("pcore.test.v1", fix),
            "c55c1c5f4793085bdab809cac9f94f620ac245c3da3b5dda2c3bd0027e78b465")

    def test_dump_is_deterministic_and_roundtrips(self):
        c = self.canon
        obj = {"z": [3, 2, 1], "a": {"k2": "v2", "k1": "v1"}}
        self.assertEqual(c.dumps(obj), c.dumps(dict(reversed(list(obj.items())))))
        self.assertEqual(c.loads(c.dumps(obj)), obj)
        self.assertEqual(c.loads(c.dumps_bytes(obj)), obj)

    def test_nfc_normalization(self):
        """結合文字列は NFC で正準形に畳む（直列化層は内容を変えない）。"""
        c = self.canon
        decomposed = "か\u3099"  # か + 濁点
        self.assertEqual(c.dumps(decomposed), '"が"')
        self.assertEqual(c.loads(c.dumps(decomposed)), "が")

    def test_nfc_key_collision_rejected(self):
        c = self.canon
        with self.assertRaises(c.CanonicalError):
            c.dumps({"が": 1, "か\u3099": 2})

    def test_float_domain(self):
        c = self.canon
        self.assertEqual(c.dumps(-0.0), "0.0")
        for bad in (math.nan, math.inf, -math.inf):
            with self.assertRaises(c.CanonicalError):
                c.dumps(bad)

    def test_rejected_types(self):
        c = self.canon
        for bad in (b"bytes", {1, 2}, object(), {"k": b"v"}, {1: "x"}):
            with self.assertRaises(c.CanonicalError):
                c.dumps(bad)

    def test_tuple_serializes_as_list(self):
        c = self.canon
        self.assertEqual(c.dumps(("a", 1)), '["a",1]')
        self.assertEqual(
            c.hash_tuple("pcore.t.v1", "tbl", "42"),
            c.hash_obj("pcore.t.v1", ["tbl", "42"]))

    def test_datetime_canonicalization(self):
        c = self.canon
        dt = datetime(2026, 3, 8, 1, 2, 3, 456789,
                      tzinfo=timezone(timedelta(hours=9)))
        self.assertEqual(c.dumps({"t": dt}),
                         '{"t":"2026-03-07T16:02:03.456789Z"}')
        with self.assertRaises(c.CanonicalError):
            c.dumps(datetime(2026, 1, 1))  # naive は拒否

    def test_canon_ts_roundtrip(self):
        c = self.canon
        dt = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
        s = c.canon_ts(dt)
        self.assertEqual(s, "2026-01-02T03:04:05.678901Z")
        self.assertEqual(c.parse_ts(s), dt)
        self.assertTrue(c.is_ts(s))
        self.assertFalse(c.is_ts("2026-01-02 03:04:05"))
        self.assertFalse(c.is_ts(123))

    def test_hash_domain_separation(self):
        c = self.canon
        self.assertNotEqual(c.hash_bytes("tag.a", b"x"),
                            c.hash_bytes("tag.b", b"x"))
        self.assertNotEqual(c.hash_bytes("tag.a", b"xy"),
                            c.hash_bytes("tag.ax", b"y"))
        self.assertEqual(len(c.hash_bytes("t", b"")), 64)

    def test_norm_text_nfkc_casefold(self):
        """照合用正規化: NFKC+casefold+空白畳み（Q-O35。直列化の NFC とは別層）。"""
        c = self.canon
        self.assertEqual(c.norm_text("  Ｈｅｌｌｏ　ＷＯＲＬＤ　\n test "),
                         "hello world test")
        self.assertEqual(c.norm_text("ﬁ"), "fi")

    def test_content_and_pattern_hash_domains(self):
        c = self.canon
        self.assertEqual(
            c.content_hash("Hello World"),
            "83abca89a2a870be2b6d3ad11bbc8f8a1870493983996b8fb9beb0068e216469")
        self.assertNotEqual(c.content_hash("x"), c.pattern_hash("x"))

    def test_fingerprint_shape(self):
        c = self.canon
        fp = c.fingerprint("Ｔｅｓｔ　１２３")
        self.assertEqual(fp["algo"], "nfkc-casefold-v1")
        self.assertEqual(fp["norm"], "test 123")


if __name__ == "__main__":
    unittest.main()
