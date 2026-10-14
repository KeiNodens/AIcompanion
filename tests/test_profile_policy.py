"""案A profile 別ポリシーのテスト。

- profile_policy: yaml の読み・fallback 順・glob 判定
- session_identity: profile_name 列の有無両方で解決
- skills.prompt_index / missing_tools: profile 別フィルタ
- server._hermes_event: session → profile → bridge patterns の配線
"""
import sqlite3
import types
import unittest
from unittest import mock

import _support


def _mk_state_db(path, rows=(), with_profile=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    cols = ("id TEXT PRIMARY KEY, source TEXT, session_key TEXT,"
            " user_id TEXT, started_at REAL")
    if with_profile:
        cols += ", profile_name TEXT"
    conn.execute(f"CREATE TABLE sessions ({cols})")
    if with_profile:
        conn.executemany(
            "INSERT INTO sessions (id, source, session_key, user_id,"
            " started_at, profile_name) VALUES (?,?,?,?,?,?)", rows)
    else:
        conn.executemany(
            "INSERT INTO sessions (id, source, session_key, user_id,"
            " started_at) VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


class PolicyLoadTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.pp = self._iso.import_module("companion.profile_policy")
        self.policy_path = self._iso.cfg.PROFILE_POLICY_PATH

    def _write(self, text: str):
        self.policy_path.write_text(text, encoding="utf-8")

    def test_missing_file_falls_back_to_env(self):
        always, owner, dm = self.pp.bridge_patterns("somebot")
        self.assertEqual(always, ["web_search", "web_extract", "browser_*"])
        self.assertEqual(dm, ["terminal", "write_file", "patch",
                              "execute_code"])
        self.assertIsNone(self.pp.skill_globs("somebot"))

    def test_default_section_overrides_env(self):
        self._write(
            "default:\n"
            "  bridge_tools: [alpha, beta]\n"
            "  bridge_dm_tools: [gamma]\n"
            "  skills: [tea-*]\n"
        )
        always, owner, dm = self.pp.bridge_patterns("unknown-profile")
        self.assertEqual(always, ["alpha", "beta"])
        self.assertEqual(dm, ["gamma"])
        self.assertEqual(self.pp.skill_globs("unknown-profile"), ["tea-*"])

    def test_profile_section_overrides_per_key(self):
        self._write(
            "default:\n"
            "  bridge_tools: [a]\n"
            "  bridge_dm_tools: [b]\n"
            "profiles:\n"
            "  bot-x:\n"
            "    bridge_tools: [x-*]\n"
            "    skills: [x-skill]\n"
        )
        # bridge_tools は profile、bridge_dm_tools は default 継承
        always, owner, dm = self.pp.bridge_patterns("bot-x")
        self.assertEqual(always, ["x-*"])
        self.assertEqual(dm, ["b"])
        self.assertEqual(self.pp.skill_globs("bot-x"), ["x-skill"])
        # 未登録 profile は default 全部
        always2, _, _ = self.pp.bridge_patterns("other")
        self.assertEqual(always2, ["a"])

    def test_malformed_yaml_falls_back(self):
        self._write("{{{{")
        always, _, _ = self.pp.bridge_patterns(None)
        self.assertIn("web_search", always)

    def test_comma_string_is_accepted(self):
        self._write("profiles:\n  bot: {bridge_tools: 'web_*, mcp__x__*'}\n")
        always, _, _ = self.pp.bridge_patterns("bot")
        self.assertEqual(always, ["web_*", "mcp__x__*"])

    def test_skill_allowed(self):
        self._write(
            "profiles:\n"
            "  bot: {skills: [tea-*, memo-hows]}\n"
        )
        self.assertTrue(self.pp.skill_allowed("tea-time", "bot"))
        self.assertTrue(self.pp.skill_allowed("memo-hows", "bot"))
        self.assertFalse(self.pp.skill_allowed("other", "bot"))
        # 未登録 profile・未指定は全許可
        self.assertTrue(self.pp.skill_allowed("other", "nope"))
        self.assertTrue(self.pp.skill_allowed("other", None))

    def test_empty_skill_list_allows_nothing(self):
        self._write("profiles:\n  bot: {skills: []}\n")
        self.assertEqual(self.pp.skill_globs("bot"), [])
        self.assertFalse(self.pp.skill_allowed("anything", "bot"))


class SessionIdentityProfileTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.hermes = self._iso.import_module("companion.runtime.hermes")
        self.state_db = self._iso.tmp / "hermes" / "state.db"

    def test_profile_name_resolved(self):
        _mk_state_db(self.state_db, rows=[
            ("s-1", "desktop", "agent:main:desktop", None, 1.0, "workbot"),
        ])
        ident = self.hermes.session_identity("s-1")
        self.assertEqual(ident["profile_name"], "workbot")

    def test_missing_column_yields_none_not_error(self):
        """profile_name 列の無い schema でも解決自体は壊れない"""
        _mk_state_db(self.state_db, rows=[
            ("s-1", "desktop", "k", "12345", 1.0),
        ], with_profile=False)
        ident = self.hermes.session_identity("s-1")
        self.assertEqual(ident["user_id"], "12345")
        self.assertIsNone(ident["profile_name"])


class SkillPolicyFilterTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.skills = self._iso.import_module("companion.skills")
        self.policy_path = self._iso.cfg.PROFILE_POLICY_PATH
        # active 技能を2件登録（staged → active へ直接遷移）
        for name in ("tea-time", "memo-hows"):
            self.db.skill_stage(
                name, "説明", "", "imported", "hash-" + name,
                source_path="/src/" + name, rel_path=name + "/SKILL.md")
            self.db.skill_set_status(name, "active")

    def _write(self, text: str):
        self.policy_path.write_text(text, encoding="utf-8")

    def test_prompt_index_filters_by_profile(self):
        self._write("profiles:\n  bot: {skills: [tea-*]}\n")
        index = self.skills.prompt_index("bot")
        self.assertIn("tea-time", index)
        self.assertNotIn("memo-hows", index)
        # 未登録 profile は全件
        index_all = self.skills.prompt_index("other")
        self.assertIn("tea-time", index_all)
        self.assertIn("memo-hows", index_all)

    def test_missing_tools_uses_profile_patterns(self):
        """requires_tools は profile の bridge 許可で判定される"""
        row = {"name": "x", "requires_tools": "mcp__foo__bar"}
        # 既定（mcp 許可なし）では不足
        self.assertEqual(self.skills.missing_tools(row), ["mcp__foo__bar"])
        self._write(
            "profiles:\n  bot: {bridge_tools: [mcp__foo__*]}\n"
        )
        # 許可 profile では満たせる
        self.assertEqual(self.skills.missing_tools(row, "bot"), [])


class HermesEventProfileTest(unittest.IsolatedAsyncioTestCase):
    """_hermes_event: session → profile → bridge patterns の配線"""

    def setUp(self):
        self.generated = []

        async def fake_generate(session_key, user_text, **kw):
            self.generated.append(kw)
            return {"reply": "ok", "inner": "", "tools": [], "model": "m"}

        engine_stub = _support.module_stub(
            "companion.engine", generate=fake_generate)
        delegate_stub = _support.module_stub(
            "companion.tools.delegate",
            pending_approvals=lambda: [],
            find_pending_by_code=lambda code: None,
        )
        self._iso = _support.IsolatedModules(
            stubs={"companion.engine": engine_stub,
                   "companion.tools.delegate": delegate_stub})
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.server = self._iso.import_module("companion.server")
        self.policy_path = self._iso.cfg.PROFILE_POLICY_PATH

        self.ident = {
            "source": "discord", "user_id": "12345",
            "session_id": "s-1", "session_key":
            "agent:main:discord:dm:42", "profile_name": "bot-x",
        }
        idp = mock.patch.object(
            self.server.hermes_rt, "session_identity",
            side_effect=lambda _aff: dict(self.ident))
        idp.start()
        self.addCleanup(idp.stop)

    def _tool(self, name):
        return {"type": "function",
                "function": {"name": name, "description": "d",
                             "parameters": {"type": "object"}}}

    def _spy_build_schemas(self):
        """server.bridge.build_schemas を包んで patterns 引数を捕まえる"""
        real = self.server.bridge.build_schemas
        captured = {}

        def spy(tools, pats):
            captured["patterns"] = list(pats)
            return real(tools, pats)

        patcher = mock.patch.object(
            self.server.bridge, "build_schemas", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return captured

    async def test_profile_patterns_reach_build_schemas(self):
        self.policy_path.write_text(
            "profiles:\n  bot-x: {bridge_dm_tools: [special_tool]}\n",
            encoding="utf-8",
        )
        captured = self._spy_build_schemas()
        await self.server._hermes_event(
            {"tools": [self._tool("web_search"), self._tool("special_tool")]},
            [{"role": "user", "content": "hi"}],
            "aff-1", "hi", [], "12345",
        )
        # DM 会話なので常時(env) + profile の DM 限定が両方載る
        self.assertIn("web_search", captured["patterns"])
        self.assertIn("special_tool", captured["patterns"])
        # generate には profile が渡る
        self.assertEqual(self.generated[0]["profile"], "bot-x")

    async def test_unresolved_identity_uses_default_policy(self):
        self.ident.clear()
        self.ident.update({"session_key": "agent:main:discord:guild:1",
                           "profile_name": None})
        captured = self._spy_build_schemas()
        await self.server._hermes_event(
            {"tools": [self._tool("web_search"), self._tool("terminal")]},
            [{"role": "user", "content": "hi"}],
            "aff-2", "hi", [], "12345",
        )
        # guild チャンネル + 未登録 profile → 常時のみ（書込系は載らない）
        self.assertIn("web_search", captured["patterns"])
        self.assertNotIn("terminal", captured["patterns"])
        self.assertIsNone(self.generated[0]["profile"])


class RecordTurnProfileTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")

    def test_turn_records_profile(self):
        tid = self.db.record_turn("s", "u", "a", profile="workbot")
        with self.db.get_db() as conn:
            row = conn.execute(
                "SELECT profile FROM turns WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row[0], "workbot")

    def test_turn_profile_defaults_none(self):
        tid = self.db.record_turn("s", "u", "a")
        with self.db.get_db() as conn:
            row = conn.execute(
                "SELECT profile FROM turns WHERE id=?", (tid,)).fetchone()
        self.assertIsNone(row[0])


class EngineProfilePromptTest(unittest.IsolatedAsyncioTestCase):
    """render_system/generate に profile が届き、system prompt に載る"""

    def setUp(self):
        self.mem = _support.memory_stub()
        self.llm_mod = _support.llm_stub()
        self.refl = _support.reflect_stub()
        self.tpkg = _support.tools_pkg_stub()
        self._iso = _support.IsolatedModules(
            stubs={
                "companion.memory": self.mem,
                "companion.llm": self.llm_mod,
                "companion.reflect": self.refl,
                "companion.tools": self.tpkg,
            }
        )
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.engine = self._iso.import_module("companion.engine")

    def test_render_system_marks_profile(self):
        text = self.engine.render_system(
            "now", "(mem)", "(self)", False, profile="workbot")
        self.assertIn("workbot", text)
        self.assertIn("profile", text)

    def test_render_system_no_profile_has_no_line(self):
        text = self.engine.render_system("now", "(mem)", "(self)", False)
        self.assertNotIn("経由で届いている", text)

    async def test_generate_passes_profile_to_system_and_record(self):
        db = self._iso.import_module("companion.db")
        self.llm_mod.fake.queue.append(_support.make_resp("ok"))
        await self.engine.generate("s-prof", "hi", profile="workbot")
        system = self.llm_mod.fake.calls[0]["messages"][0]["content"]
        self.assertIn("workbot", system)
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT profile FROM turns WHERE session_key='s-prof'"
            ).fetchone()
        self.assertEqual(row[0], "workbot")


class PolicyLintTest(unittest.TestCase):
    """hermes_check の profile_policy.yaml 検証"""

    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.chk = self._iso.import_module("companion.hermes_check")
        self.policy_path = self._iso.cfg.PROFILE_POLICY_PATH

    def _warn_keys(self):
        return {f.key for f in self.chk.check({}) if f.level == "warn"}

    def test_unknown_key_warns(self):
        self.policy_path.write_text(
            "profiles:\n  bot: {brige_tools: [x]}\n", encoding="utf-8")
        self.assertIn("profile_policy.yaml:profiles.bot.brige_tools",
                      self._warn_keys())

    def test_ghost_profile_warns(self):
        self.policy_path.write_text(
            "profiles:\n  ghost-bot: {skills: [x]}\n", encoding="utf-8")
        self.assertIn("profile_policy.yaml:profiles.ghost-bot",
                      self._warn_keys())

    def test_registered_profile_no_warn(self):
        (self._iso.cfg.HERMES_HOME / "profiles" / "workbot").mkdir(
            parents=True)
        self.policy_path.write_text(
            "profiles:\n  workbot: {skills: [x]}\n", encoding="utf-8")
        warns = self._warn_keys()
        self.assertFalse(any(k.startswith("profile_policy.yaml")
                             for k in warns))

    def test_malformed_file_warns(self):
        self.policy_path.write_text("{{{{", encoding="utf-8")
        self.assertIn("profile_policy.yaml", self._warn_keys())


class ProfileCheckSmokeTest(unittest.TestCase):
    """profile_check が壊れずに概要を出せる"""

    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.pc = self._iso.import_module("companion.profile_check")

    def test_describe_and_list_run(self):
        import io
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.pc._describe("default")
            self.pc._list_all()
        text = out.getvalue()
        self.assertIn("== default ==", text)
        self.assertIn("bridge 常時", text)


if __name__ == "__main__":
    unittest.main()
