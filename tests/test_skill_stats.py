"""skill_stats — active 技能の成果追跡（読まれ×往復失敗の相関）

- 材料は turn_tool_calls(origin='core', tool='skill_read') の turn 紐づき行
- 「読めた」呼出しだけを reads に数え、拒否応答は read_errors 側
- active 技能で読まれた往復の半分以上に tool 失敗があれば flagged
- 自動 disable しない（レポート通知に留める）
"""
import unittest

import _support


def _call(tool, args, status="ok", result="r", call_id="c"):
    return {"tool": tool, "call_id": call_id, "args": args,
            "status": status, "origin": "core", "result": result}


def _read(name, status="ok", result="# 手順", call_id="r"):
    return _call("skill_read", {"name": name}, status=status,
                 result=result, call_id=call_id)


class SkillStatsTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.ss = self._iso.import_module("companion.skill_stats")

    def _seed_turn(self, calls, user="u", assistant="a"):
        tid = self.db.record_turn("s", user, assistant)
        self.db.record_tool_calls(tid, "s", calls)
        return tid

    def _stage(self, name, status="active"):
        self.db.skill_stage(name, "説明", "", "imported", "h-" + name, "", name)
        if status != "staged":
            self.db.skill_set_status(name, status, decided_by="master")

    def test_counts_reads_and_failure_turns(self):
        self._stage("cook")
        # 読まれ + その往復は全部 ok
        self._seed_turn([_read("cook"), _call("web_search", {"q": 1})])
        # 読まれ + 同じ往復で web_search が失敗
        self._seed_turn([_read("cook", call_id="r2"),
                         _call("web_search", {"q": 2}, status="error",
                               result="boom", call_id="w2")])
        rep = self.ss.collect()
        e = rep["per_skill"]["cook"]
        self.assertEqual(e["reads"], 2)
        self.assertEqual(e["turns"], 2)
        self.assertEqual(e["fail_turns"], 1)
        self.assertEqual(e["read_errors"], 0)
        # 失敗率 1/2 は FLAG_MIN_TURNS 未満なので flagged にならない
        self.assertEqual(rep["flagged"], [])
        self.assertEqual(rep["unused_active"], [])

    def test_refusal_reads_counted_as_errors(self):
        self._stage("ghost")
        # 読もうとしたが見つからない（status=ok だが応答は拒否文）
        self._seed_turn([_read(
            "ghost", result="スキル ghost は見つからない（skill_list で確認して）")])
        # tool 自体の失敗
        self._seed_turn([_read("ghost", status="error", result="timeout")])
        rep = self.ss.collect()
        e = rep["per_skill"]["ghost"]
        self.assertEqual(e["reads"], 0)
        self.assertEqual(e["read_errors"], 2)
        # reads=0 なので unused_active 側に出る
        self.assertEqual(rep["unused_active"], ["ghost"])

    def test_flagged_when_majority_turns_fail(self):
        self._stage("flaky")
        # 4往復読まれて3往復で tool 失敗 → 75% >= 50% かつ >=3往復
        for i in range(3):
            self._seed_turn([_read("flaky", call_id=f"r{i}"),
                             _call("web_search", {"q": i}, status="error",
                                   result="boom", call_id=f"w{i}")])
        self._seed_turn([_read("flaky", call_id="r9"),
                         _call("web_search", {"q": 9}, status="ok")])
        rep = self.ss.collect()
        self.assertEqual(rep["flagged"], ["flaky"])
        e = rep["per_skill"]["flaky"]
        self.assertEqual((e["turns"], e["fail_turns"]), (4, 3))

    def test_staged_skill_not_flagged(self):
        """staged（承認待ち）は active 扱いしないので旗は立たない"""
        self._stage("cand", status="staged")
        for i in range(3):
            self._seed_turn([_read("cand", call_id=f"r{i}"),
                             _call("web_search", {"q": i}, status="error",
                                   result="boom", call_id=f"w{i}")])
        rep = self.ss.collect()
        self.assertEqual(rep["flagged"], [])
        # staged は unused_active にも出ない
        self.assertEqual(rep["unused_active"], [])

    def test_unread_skill_reads_as_unused(self):
        self._stage("lonely")
        self._stage("used")
        self._seed_turn([_read("used")])
        rep = self.ss.collect()
        self.assertEqual(rep["unused_active"], ["lonely"])

    def test_format_report_content(self):
        self._stage("flaky")
        self._stage("lonely")
        for i in range(3):
            self._seed_turn([_read("flaky", call_id=f"r{i}"),
                             _call("web_search", {"q": i}, status="error",
                                   result="boom", call_id=f"w{i}")])
        rep = self.ss.collect()
        body = self.ss.format_report(rep)
        self.assertIn("flaky: 読まれ3回 / 往復3件", body)
        self.assertIn("3/3件", body)
        self.assertIn("skill_disable", body)
        self.assertIn("lonely", body)

    def test_no_data_means_no_report(self):
        rep = self.ss.collect()
        self.assertIsNone(self.ss.format_report(rep))

    def test_body_run_calls_not_counted(self):
        """body_run 起点の tool 行（turn_id=NULL）は集計しない"""
        self.db.record_tool_calls(None, "s", [
            _call("skill_read", {"name": "cook"})])
        self._stage("cook")
        rep = self.ss.collect()
        self.assertEqual(rep["per_skill"], {})


if __name__ == "__main__":
    unittest.main()
