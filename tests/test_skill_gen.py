"""R5f: skill_gen — 成功/失敗 trace からの SKILL.md 起票（§7.6＋失敗レーン）

- 成功レーンの材料は turn_tool_calls が MIN_CALLS 回以上・全て ok の
  往復だけ。失敗レーンは FAIL_MIN_CALLS 回以上・1件以上が非 ok の往復
- LLM の出力は JSON 素材のみ。SKILL.md は決定的レンダで origin: self
- 投函は data/skills-inbox → scan_inbox の通常レーンで staged（自動
  有効化しない）。処理水位は state.json の skill_gen_last_turn_id /
  skill_gen_fail_last_turn_id（レーン別）
"""
import json
import unittest

import _support


class SkillGenTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mem = _support.memory_stub()
        self.llm = _support.llm_stub()
        self._iso = _support.IsolatedModules(stubs={
            "companion.llm": self.llm,
            "companion.memory": self.mem,
        })
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.skills = self._iso.import_module("companion.skills")
        self.sg = self._iso.import_module("companion.skill_gen")
        self.cfg = self._iso.cfg

    def _seed_turn(self, calls, user="手伝って", assistant="やったよ"):
        tid = self.db.record_turn("s", user, assistant)
        self.db.record_tool_calls(tid, "s", calls)
        return tid

    def _ok_calls(self, n=3, tool="web_search"):
        return [{"tool": tool, "call_id": f"c{i}", "args": {"q": i},
                 "status": "ok", "origin": "core", "result": f"r{i}"}
                for i in range(n)]

    def _queue_skill_json(self, name="search-and-summarize", **kw):
        payload = {
            "name": name,
            "description": "調べてまとめる手順",
            "scopes": "web",
            "requires_tools": "web_search",
            "when": "調べ物を頼まれたとき",
            "steps": ["web_search で検索する", "結果を3件に絞る",
                      "要点をまとめて返す"],
            "cautions": ["出典を書く"],
        }
        payload.update(kw)
        self.llm.fake.queue.append(
            _support.make_resp(content=json.dumps(payload)))

    def _inbox_files(self):
        return sorted(self.cfg.SKILLS_INBOX_DIR.glob("*/SKILL.md"))

    async def test_generates_skill_md_into_inbox(self):
        self._seed_turn(self._ok_calls(3))
        self._queue_skill_json()
        self.assertEqual(await self.sg.run(), "done")
        files = self._inbox_files()
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("origin: self", text)
        self.assertIn("name: search-and-summarize", text)
        self.assertIn("## 手順", text)
        self.assertIn("web_search で検索する", text)

    async def test_inbox_package_stages_as_self_origin(self):
        """投函された生成物は scan_inbox の通常レーンで staged になる"""
        self._seed_turn(self._ok_calls(3))
        self._queue_skill_json()
        await self.sg.run()
        res = self.skills.scan_inbox()
        self.assertEqual(res["staged"], ["search-and-summarize"])
        row = self.db.skill_get("search-and-summarize")
        self.assertEqual(row["status"], "staged")
        self.assertEqual(row["origin"], "self")
        # 通知用メタにも origin が乗る
        self.assertEqual(
            res["meta"]["search-and-summarize"]["origin"], "self")

    async def test_skip_response_produces_nothing_but_advances(self):
        self._seed_turn(self._ok_calls(3))
        self.llm.fake.queue.append(
            _support.make_resp(content='{"skip": true}'))
        await self.sg.run()
        self.assertEqual(self._inbox_files(), [])
        # 水位は進む（同じ材料を翌日掘り直さない）
        self.assertGreater(
            self.db.load_state()["skill_gen_last_turn_id"], 0)
        # 再実行しても材料なし → skipped
        self.assertEqual(await self.sg.run(), "skipped")

    async def test_turn_with_failed_call_is_not_success_candidate(self):
        """失敗を含む往復は成功レーンの材料にしない（失敗レーンへ回る）。

        失敗レーンがその往復を処理するので LLM は1回呼ばれるが、
        成功レーンの水位は動かない。"""
        tid = self._seed_turn(self._ok_calls(2) + [
            {"tool": "web_search", "call_id": "cx", "args": {},
             "status": "error", "origin": "core", "result": "boom"}])
        # 失敗レーンの1呼出し分（起票対象なしと判断 → skip）
        self.llm.fake.queue.append(
            _support.make_resp(content='{"skip": true}'))
        self.assertEqual(await self.sg.run(), "done")
        self.assertEqual(self._inbox_files(), [])
        # 成功レーンの水位は 0 のまま、失敗レーンの水位だけ進む
        state = self.db.load_state()
        self.assertEqual(state.get("skill_gen_last_turn_id", 0), 0)
        self.assertEqual(state["skill_gen_fail_last_turn_id"], tid)
        self.assertEqual(len(self.llm.fake.calls), 1)

    async def test_too_few_calls_not_candidate(self):
        self._seed_turn(self._ok_calls(2))
        self.assertEqual(await self.sg.run(), "skipped")

    async def test_malformed_llm_output_skips_and_advances(self):
        tid = self._seed_turn(self._ok_calls(3))
        self.llm.fake.queue.append(_support.make_resp(content="no json"))
        await self.sg.run()
        self.assertEqual(self._inbox_files(), [])
        self.assertEqual(
            self.db.load_state()["skill_gen_last_turn_id"], tid)

    async def test_invalid_render_fields_skip(self):
        """name 不正・steps 不足など要件を満たさない JSON は投函しない"""
        self._seed_turn(self._ok_calls(3))
        self._queue_skill_json(name="bad name!", steps=["only one"])
        await self.sg.run()
        self.assertEqual(self._inbox_files(), [])

    async def test_existing_skill_name_not_overwritten(self):
        """同名の既存技能がいるときは投函しない（上書き提案を作らない）"""
        self.db.skill_stage(
            "search-and-summarize", "既存", "", "imported", "h", "", "n")
        self._seed_turn(self._ok_calls(3))
        self._queue_skill_json(name="search-and-summarize")
        await self.sg.run()
        self.assertEqual(self._inbox_files(), [])
        row = self.db.skill_get("search-and-summarize")
        self.assertEqual(row["description"], "既存")

    async def test_secrets_in_trace_are_scrubbed_before_llm(self):
        tid = self._seed_turn([
            {"tool": "terminal", "call_id": "c0",
             "args": {"command": "export API_KEY=sk-live-abcdef1234567890"},
             "status": "ok", "origin": "core", "result": "done"},
            *self._ok_calls(2),
        ])
        self._queue_skill_json()
        await self.sg.run()
        prompt = self.llm.fake.calls[0]["messages"][0]["content"]
        self.assertNotIn("sk-live-abcdef1234567890", prompt)
        self.assertIn("API_KEY=***", prompt)

    async def test_llm_failure_keeps_watermark_for_retry(self):
        tid1 = self._seed_turn(self._ok_calls(3))
        tid2 = self._seed_turn(self._ok_calls(3))
        self.llm.fake.queue.append(
            _support.make_resp(content='{"skip": true}'))
        self.llm.fake.queue.append(RuntimeError("llm down"))
        await self.sg.run()
        # tid1 は判断済みで水位が進み、tid2（障害）は次回に持ち越す
        self.assertEqual(
            self.db.load_state()["skill_gen_last_turn_id"], tid1)
        self._queue_skill_json()
        await self.sg.run()
        self.assertEqual(
            self.db.load_state()["skill_gen_last_turn_id"], tid2)
        self.assertEqual(len(self._inbox_files()), 1)


class SkillGenFailLaneTest(unittest.IsolatedAsyncioTestCase):
    """失敗レーン — 非 ok trace からガードレール形 SKILL.md を起票する。

    - 材料は「FAIL_MIN_CALLS 回以上の呼出し・うち1件以上が非 ok」の往復
    - 生成物は「## 注意・やってはいけないこと」が本体の SKILL.md（origin: self）
    - 水位は skill_gen_fail_last_turn_id（成功レーンとは別管理）
    """

    def setUp(self):
        self.mem = _support.memory_stub()
        self.llm = _support.llm_stub()
        self._iso = _support.IsolatedModules(stubs={
            "companion.llm": self.llm,
            "companion.memory": self.mem,
        })
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.skills = self._iso.import_module("companion.skills")
        self.sg = self._iso.import_module("companion.skill_gen")
        self.cfg = self._iso.cfg

    def _seed_turn(self, calls, user="なんかやって", assistant="むりでした"):
        tid = self.db.record_turn("s", user, assistant)
        self.db.record_tool_calls(tid, "s", calls)
        return tid

    def _mixed_calls(self, n_ok=2, n_err=1):
        calls = [
            {"tool": "web_search", "call_id": f"e{i}",
             "args": {"q": i}, "status": "error", "origin": "core",
             "result": "timeout"} for i in range(n_err)]
        calls += [
            {"tool": "web_search", "call_id": f"o{i}",
             "args": {"q": i}, "status": "ok", "origin": "core",
             "result": f"r{i}"} for i in range(n_ok)]
        return calls

    def _ok_calls(self, n=3, tool="web_search"):
        return [{"tool": tool, "call_id": f"c{i}", "args": {"q": i},
                 "status": "ok", "origin": "core", "result": f"r{i}"}
                for i in range(n)]

    def _queue_guardrail_json(self, name="web-search-retry", **kw):
        payload = {
            "name": name,
            "description": "検索失敗時の注意",
            "scopes": "web",
            "requires_tools": "web_search",
            "when": "web_search が連続して落ちるとき",
            "cautions": ["同じクエリで連続 retry しない",
                         "時間を置いてから短い語で試す"],
            "steps": ["失敗を確認", "クエリを短くして再試行"],
        }
        payload.update(kw)
        self.llm.fake.queue.append(
            _support.make_resp(content=json.dumps(payload)))

    def _inbox_files(self):
        return sorted(self.cfg.SKILLS_INBOX_DIR.glob("*/SKILL.md"))

    async def test_guardrail_md_into_inbox(self):
        self._seed_turn(self._mixed_calls())
        self._queue_guardrail_json()
        self.assertEqual(await self.sg.run(), "done")
        files = self._inbox_files()
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("origin: self", text)
        self.assertIn("name: web-search-retry", text)
        self.assertIn("## 注意・やってはいけないこと", text)
        self.assertIn("同じクエリで連続 retry しない", text)
        self.assertIn("fail-lane: source turn", text)

    async def test_guardrail_stages_as_self_origin(self):
        self._seed_turn(self._mixed_calls())
        self._queue_guardrail_json()
        await self.sg.run()
        res = self.skills.scan_inbox()
        self.assertEqual(res["staged"], ["web-search-retry"])
        row = self.db.skill_get("web-search-retry")
        self.assertEqual(row["status"], "staged")
        self.assertEqual(row["origin"], "self")

    async def test_all_ok_turn_is_not_failure_candidate(self):
        """全 ok の往復は失敗レーンの材料にしない（LLM すら呼ばない）"""
        self._seed_turn(self._ok_calls(3))
        # 成功レーン側の応答だけ queue（失敗レーンには回らない前提）
        self.llm.fake.queue.append(
            _support.make_resp(content='{"skip": true}'))
        await self.sg.run()
        self.assertEqual(
            self.db.load_state().get("skill_gen_fail_last_turn_id", 0), 0)

    async def test_too_few_calls_not_failure_candidate(self):
        """呼出し1件だけで失敗した往復（前後関係が無い）は材料にしない"""
        self._seed_turn([
            {"tool": "web_search", "call_id": "e0", "args": {},
             "status": "error", "origin": "core", "result": "timeout"}])
        self.assertEqual(await self.sg.run(), "skipped")
        self.assertEqual(self.llm.fake.calls, [])

    async def test_guardrail_skip_advances_watermark(self):
        tid = self._seed_turn(self._mixed_calls())
        self.llm.fake.queue.append(
            _support.make_resp(content='{"skip": true}'))
        await self.sg.run()
        self.assertEqual(self._inbox_files(), [])
        self.assertEqual(
            self.db.load_state()["skill_gen_fail_last_turn_id"], tid)

    async def test_guardrail_invalid_json_skips(self):
        self._seed_turn(self._mixed_calls())
        self._queue_guardrail_json(cautions=[])   # cautions 必須なので不合格
        await self.sg.run()
        self.assertEqual(self._inbox_files(), [])

    async def test_guardrail_name_collision_skipped(self):
        self.db.skill_stage(
            "web-search-retry", "既存", "", "imported", "h", "", "n")
        self._seed_turn(self._mixed_calls())
        self._queue_guardrail_json()
        await self.sg.run()
        self.assertEqual(self._inbox_files(), [])

    async def test_both_lanes_in_one_run(self):
        """1回の run で成功レーン→失敗レーンの順に両方回る"""
        self._seed_turn(self._ok_calls(3))
        self._seed_turn(self._mixed_calls())
        # 応答はレーン順（成功 → 失敗）に queue される
        self._queue_skill_json()
        self._queue_guardrail_json()
        self.assertEqual(await self.sg.run(), "done")
        files = self._inbox_files()
        self.assertEqual(len(files), 2)
        names = {f.parent.name for f in files}
        self.assertEqual(names, {"search-and-summarize", "web-search-retry"})
        # 両レーンの水位が独立に進む
        state = self.db.load_state()
        self.assertGreater(state["skill_gen_last_turn_id"], 0)
        self.assertGreater(state["skill_gen_fail_last_turn_id"], 0)

    def _queue_skill_json(self, name="search-and-summarize", **kw):
        payload = {
            "name": name,
            "description": "調べてまとめる手順",
            "scopes": "web",
            "requires_tools": "web_search",
            "when": "調べ物を頼まれたとき",
            "steps": ["web_search で検索する", "結果を3件に絞る",
                      "要点をまとめて返す"],
            "cautions": ["出典を書く"],
        }
        payload.update(kw)
        self.llm.fake.queue.append(
            _support.make_resp(content=json.dumps(payload)))


if __name__ == "__main__":
    unittest.main()
