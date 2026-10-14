"""db.search_turns + tools/history.py: 会話履歴の文字列検索"""
import unittest

from _support import IsolatedModules


def _seed(db):
    db.record_turn("s1", "昨日カレーを作った", "おいしそう", profile="cookbot")
    db.record_turn("s2", "カレーは苦手", "そうなんだ", profile="otherbot")
    db.record_turn("s3", "今日は蕎麦にした", "いいね", profile=None)


class SearchTurnsTest(unittest.TestCase):
    def test_substring_match_both_sides(self):
        with IsolatedModules() as iso:
            db = iso.import_module("companion.db")
            _seed(db)
            rows = db.search_turns("カレー")
            self.assertEqual(len(rows), 2)
            rows = db.search_turns("いいね")  # assistant_text 側にもヒット
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][2], "今日は蕎麦にした")

    def test_profile_filter(self):
        with IsolatedModules() as iso:
            db = iso.import_module("companion.db")
            _seed(db)
            rows = db.search_turns("カレー", profile="cookbot")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][6], "cookbot")

    def test_limit_and_order(self):
        with IsolatedModules() as iso:
            db = iso.import_module("companion.db")
            for i in range(5):
                db.record_turn("s", f"メモ{i}", "ok")
            rows = db.search_turns("メモ", limit=2)
            self.assertEqual([r[2] for r in rows], ["メモ4", "メモ3"])  # 新しい順


class HistoryToolTest(unittest.TestCase):
    def _import(self, iso):
        iso.import_module("companion.db")
        return iso.import_module("companion.tools.history"), \
            iso.import_module("companion.tools.actor")

    def test_default_scopes_to_current_profile(self):
        with IsolatedModules() as iso:
            db = iso.import_module("companion.db")
            _seed(db)
            hist, actor = self._import(iso)
            tok = actor.CURRENT_PROFILE.set("cookbot")
            try:
                out = hist.history_search("カレー")
            finally:
                actor.CURRENT_PROFILE.reset(tok)
            self.assertIn("作った", out)
            self.assertNotIn("苦手", out)  # 他profileの往復は出ない

    def test_all_and_no_profile(self):
        with IsolatedModules() as iso:
            db = iso.import_module("companion.db")
            _seed(db)
            hist, actor = self._import(iso)
            out = hist.history_search("カレー", profile="all")
            self.assertIn("苦手", out)          # 全件検索に切り替わる
            out = hist.history_search("蕎麦")  # profileなし→全件
            self.assertIn("蕎麦", out)

    def test_not_found_and_bad_query(self):
        with IsolatedModules() as iso:
            db = iso.import_module("companion.db")
            _seed(db)
            hist, _ = self._import(iso)
            self.assertIn("見つからなかった", hist.history_search("存在しない語"))
            self.assertIn("指定してください", hist.history_search("  "))


if __name__ == "__main__":
    unittest.main()
