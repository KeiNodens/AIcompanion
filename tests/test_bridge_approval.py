"""R5a-e: 橋経由承認の監査写し + DM-only 書込系開放 + Hermes plugin

- POST /v1/bridge/approval_event: 検証・記録・冪等・抜粋スクラブ・認証
- approval_affinity: Hermes state.db の (session_id, session_key) →
  companion 側の gwk_<digest> 逆算
- DM-only: BODY_BRIDGE_DM_TOOLS は DM 会話のスキーマにだけ載る
- companion-approval-audit plugin: redact / register / forward の
  fail-open（Hermes 非依存なので実ファイルから直接 import する）
"""
import hashlib
import importlib.util
import json
import sqlite3
import types
import unittest
from unittest import mock

import httpx

import _support


def _mk_state_db(path, rows=(), generations=()):
    """test_actor_resolution と同じ形の偽 state.db"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT,"
        " session_key TEXT, user_id TEXT, started_at REAL)")
    conn.execute(
        "CREATE TABLE conversation_generations (source TEXT,"
        " session_key TEXT, generation INTEGER,"
        " PRIMARY KEY (source, session_key))")
    conn.executemany(
        "INSERT INTO sessions (id, source, session_key, user_id,"
        " started_at) VALUES (?,?,?,?,?)", rows)
    conn.executemany(
        "INSERT INTO conversation_generations"
        " (source, session_key, generation) VALUES (?,?,?)", generations)
    conn.commit()
    conn.close()


def _gwk(source, key, generation=""):
    digest = hashlib.sha256(
        f"{source}|{key}|{generation}".encode("utf-8")).hexdigest()[:24]
    return f"gwk_{digest}"


class ApprovalEventEndpointTest(unittest.IsolatedAsyncioTestCase):
    """POST /v1/bridge/approval_event の入口面"""

    def setUp(self):
        engine_stub = _support.module_stub(
            "companion.engine", generate=lambda *a, **k: {})
        delegate_stub = _support.module_stub(
            "companion.tools.delegate",
            pending_approvals=lambda: [],
            delegate_approve=lambda jid, who: "ok",
            delegate_deny=lambda jid, who="": "deny",
            find_pending_by_code=lambda code: None,
        )
        self._iso = _support.IsolatedModules(
            stubs={"companion.engine": engine_stub,
                   "companion.tools.delegate": delegate_stub})
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.server = self._iso.import_module("companion.server")
        self.db = self._iso.import_module("companion.db")

    def _client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.create_app()),
            base_url="http://t")

    def _event(self, **kw):
        body = {
            "event": "pre_approval_request",
            "session_key": "agent:main:discord:dm:42",
            "session_id": "sess-1",
            "turn_id": "turn-9",
            "tool_call_id": "call-7",
            "pattern_key": "terminal",
            "surface": "gateway",
            "command_excerpt": "rm -rf ./tmp",
        }
        body.update(kw)
        return body

    def _rows(self, where="", args=()):
        with self.db.get_db() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM turn_tool_calls"
                + (f" WHERE {where}" if where else ""), args).fetchall()

    async def test_pre_event_records_audit_row(self):
        async with self._client() as c:
            res = await c.post("/v1/bridge/approval_event",
                               json=self._event())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True, "recorded": True})
        rows = self._rows("origin='body_approval'")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["tool"], "terminal")
        self.assertEqual(r["status"], "requested")
        # ref は橋 trace（origin='body_bridge' の ref=call_id）と join できる
        self.assertEqual(r["ref"], "call-7")
        self.assertEqual(r["result_excerpt"], "rm -rf ./tmp")
        args = json.loads(r["args_json"])
        self.assertEqual(args["event"], "pre_approval_request")
        self.assertEqual(args["session_id"], "sess-1")
        self.assertEqual(args["turn_id"], "turn-9")
        self.assertEqual(args["surface"], "gateway")

    async def test_post_event_records_choice(self):
        async with self._client() as c:
            res = await c.post("/v1/bridge/approval_event", json=self._event(
                event="post_approval_response", choice="once",
                decided_by="user", command_excerpt="rm -rf ./tmp"))
        self.assertEqual(res.status_code, 200)
        r = self._rows("origin='body_approval'")[0]
        self.assertEqual(r["status"], "once")
        self.assertEqual(json.loads(r["args_json"])["decided_by"], "user")

    async def test_validation_rejects_bad_events(self):
        async with self._client() as c:
            for bad in (
                {}, {"event": "nope", "pattern_key": "x"},
                {"event": "pre_approval_request"},   # pattern_key 無し
                {"event": "post_approval_response", "pattern_key": ""},
            ):
                res = await c.post("/v1/bridge/approval_event", json=bad)
                self.assertEqual(res.status_code, 400, bad)
            res = await c.post("/v1/bridge/approval_event",
                               content=b"not json",
                               headers={"content-type": "application/json"})
            self.assertEqual(res.status_code, 400)
        self.assertEqual(self._rows("origin='body_approval'"), [])

    async def test_duplicate_delivery_is_idempotent(self):
        async with self._client() as c:
            r1 = await c.post("/v1/bridge/approval_event",
                              json=self._event())
            r2 = await c.post("/v1/bridge/approval_event",
                              json=self._event())
        self.assertTrue(r1.json()["recorded"])
        self.assertFalse(r2.json()["recorded"])
        self.assertEqual(len(self._rows("origin='body_approval'")), 1)

    async def test_excerpt_is_scrubbed_and_bounded(self):
        secret = "AKIA" + "X" * 40
        async with self._client() as c:
            await c.post("/v1/bridge/approval_event", json=self._event(
                tool_call_id="c-scrub",
                command_excerpt=(
                    f"export AWS_SECRET_ACCESS_KEY={secret} && "
                    "curl -H 'Authorization: Bearer abcdef1234567890' "
                    "https://u:p4ss@example.com " + "x" * 600)))
        r = self._rows("ref='c-scrub'")[0]
        self.assertNotIn(secret, r["result_excerpt"])
        self.assertNotIn("abcdef1234567890", r["result_excerpt"])
        self.assertNotIn("p4ss", r["result_excerpt"])
        self.assertLessEqual(len(r["result_excerpt"]), 500)

    async def test_auth_enforced_when_configured(self):
        # COMPANION_API_KEY は hermes_conf が config 属性を都度読むので
        # cfg への代入で効く。COMPANION_API_AUTH は server が import 時に
        # 値を束縛するのでモジュール属性を差し替える
        self._iso.cfg.COMPANION_API_KEY = "k3y"
        with mock.patch.object(self.server, "COMPANION_API_AUTH", "enforce"):
            async with self._client() as c:
                res = await c.post("/v1/bridge/approval_event",
                                   json=self._event())
                self.assertEqual(res.status_code, 401)
                res = await c.post(
                    "/v1/bridge/approval_event", json=self._event(),
                    headers={"x-companion-key": "k3y"})
                self.assertEqual(res.status_code, 200)
                res = await c.post(
                    "/v1/bridge/approval_event", json=self._event(
                        tool_call_id="c-bearer"),
                    headers={"authorization": "Bearer k3y"})
                self.assertEqual(res.status_code, 200)

    async def test_session_key_resolved_via_state_db(self):
        """state.db があると Hermes 側 session_key から gwk を逆算して記録"""
        key = "agent:main:discord:dm:42"
        _mk_state_db(
            self._iso.cfg.HERMES_HOME / "state.db",
            rows=[("sess-1", "discord", key, "12345", 1.0)],
            generations=[("discord", key, 3)],
        )
        async with self._client() as c:
            await c.post("/v1/bridge/approval_event", json=self._event())
        r = self._rows("origin='body_approval'")[0]
        self.assertEqual(r["session_key"], _gwk("discord", key, "3"))

    async def test_no_state_db_falls_back_to_hermes_key(self):
        async with self._client() as c:
            await c.post("/v1/bridge/approval_event", json=self._event())
        r = self._rows("origin='body_approval'")[0]
        self.assertEqual(r["session_key"], "sess-1")


class ApprovalAffinityTest(unittest.TestCase):
    """runtime.hermes.approval_affinity の逆算"""

    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.hermes = self._iso.import_module("companion.runtime.hermes")
        self.state_db = self._iso.tmp / "hermes" / "state.db"

    def test_session_id_and_key_resolve_to_gwk(self):
        key = "agent:main:discord:dm:42"
        _mk_state_db(self.state_db,
                     rows=[("s1", "discord", key, "1", 1.0)],
                     generations=[("discord", key, 2)])
        self.assertEqual(
            self.hermes.approval_affinity("s1", key),
            _gwk("discord", key, "2"))

    def test_key_only_resolves(self):
        key = "agent:main:discord:dm:7"
        _mk_state_db(self.state_db,
                     rows=[("s9", "discord", key, "1", 1.0)])
        self.assertEqual(
            self.hermes.approval_affinity("", key),
            _gwk("discord", key))

    def test_unknown_falls_back_to_physical_id(self):
        _mk_state_db(self.state_db)
        self.assertEqual(
            self.hermes.approval_affinity("phys-1", "no-such-key"), "phys-1")
        self.assertIsNone(self.hermes.approval_affinity("", ""))

    def test_missing_db_fails_soft(self):
        self.assertEqual(
            self.hermes.approval_affinity("s1", "k"), "s1")


class DmOnlyBridgeTest(unittest.IsolatedAsyncioTestCase):
    """BODY_BRIDGE_DM_TOOLS は DM 会話にだけ見せる（§16.1 決裁条件1）"""

    TOOLS = [
        {"type": "function", "function": {
            "name": n, "description": "d",
            "parameters": {"type": "object"}}}
        for n in ("web_search", "terminal", "write_file")
    ]
    HEADERS = {"x-companion-session": "h-sess"}

    def setUp(self):
        self.seen_schemas = []

        async def fake_generate(session_key, user_text, bridge=None, **kw):
            if bridge is not None:
                self.seen_schemas.append(
                    sorted(s["function"]["name"] for s in bridge.schemas))
            return {"reply": "ok", "inner": "", "tools": [], "model": "m"}

        engine_stub = _support.module_stub(
            "companion.engine", generate=fake_generate)
        delegate_stub = _support.module_stub(
            "companion.tools.delegate",
            pending_approvals=lambda: [],
            delegate_approve=lambda jid, who: "ok",
            delegate_deny=lambda jid, who="": "deny",
            find_pending_by_code=lambda code: None,
        )
        self._iso = _support.IsolatedModules(
            stubs={"companion.engine": engine_stub,
                   "companion.tools.delegate": delegate_stub})
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.server = self._iso.import_module("companion.server")
        resolver = mock.patch.object(
            self.server.body_rt, "resolve_actor", return_value="12345")
        resolver.start()
        self.addCleanup(resolver.stop)

    def _client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.create_app()),
            base_url="http://t")

    def _turn(self):
        return {"messages": [{"role": "user", "content": "やって"}],
                "tools": self.TOOLS}

    def _ident(self, session_key):
        return mock.patch.object(
            self.server.hermes_rt, "session_identity",
            return_value={"source": "discord", "user_id": "12345",
                          "session_id": "s1", "session_key": session_key})

    async def test_dm_session_gets_write_tools(self):
        with self._ident("agent:main:discord:dm:42"):
            async with self._client() as c:
                await c.post("/v1/chat/completions",
                             headers=self.HEADERS, json=self._turn())
        self.assertIn("body__terminal", self.seen_schemas[0])
        self.assertIn("body__write_file", self.seen_schemas[0])
        self.assertIn("body__web_search", self.seen_schemas[0])

    async def test_guild_channel_gets_read_tools_only(self):
        with self._ident("agent:main:discord:channel:99"):
            async with self._client() as c:
                await c.post("/v1/chat/completions",
                             headers=self.HEADERS, json=self._turn())
        self.assertEqual(self.seen_schemas[0], ["body__web_search"])

    async def test_unresolvable_session_fails_closed(self):
        with mock.patch.object(
                self.server.hermes_rt, "session_identity",
                return_value=None):
            async with self._client() as c:
                await c.post("/v1/chat/completions",
                             headers=self.HEADERS, json=self._turn())
        self.assertEqual(self.seen_schemas[0], ["body__web_search"])


class OwnerGateBridgeTest(unittest.IsolatedAsyncioTestCase):
    """BODY_BRIDGE_OWNER_TOOLS（read_file等）はowner判定の会話にだけ見せる。
    共有chの非owner発言でローカルファイルが読み出せないようにするため"""

    TOOLS = [
        {"type": "function", "function": {
            "name": n, "description": "d",
            "parameters": {"type": "object"}}}
        for n in ("web_search", "read_file", "search_files")
    ]
    HEADERS = {"x-companion-session": "h-sess"}

    def setUp(self):
        self.seen_schemas = []

        async def fake_generate(session_key, user_text, bridge=None, **kw):
            if bridge is not None:
                self.seen_schemas.append(
                    sorted(s["function"]["name"] for s in bridge.schemas))
            return {"reply": "ok", "inner": "", "tools": [], "model": "m"}

        engine_stub = _support.module_stub(
            "companion.engine", generate=fake_generate)
        delegate_stub = _support.module_stub(
            "companion.tools.delegate",
            pending_approvals=lambda: [],
            delegate_approve=lambda jid, who: "ok",
            delegate_deny=lambda jid, who="": "deny",
            find_pending_by_code=lambda code: None,
        )
        self._iso = _support.IsolatedModules(
            stubs={"companion.engine": engine_stub,
                   "companion.tools.delegate": delegate_stub})
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.server = self._iso.import_module("companion.server")

    def _client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.create_app()),
            base_url="http://t")

    def _turn(self):
        return {"messages": [{"role": "user", "content": "読んで"}],
                "tools": self.TOOLS}

    def _setup_session(self, session_key, actor):
        """session_identity と resolve_actor をまとめて差し替える"""
        ident = mock.patch.object(
            self.server.hermes_rt, "session_identity",
            return_value={"source": "discord", "user_id": "999",
                          "session_id": "s1", "session_key": session_key})
        resolver = mock.patch.object(
            self.server.body_rt, "resolve_actor", return_value=actor)
        ident.start(); resolver.start()
        self.addCleanup(ident.stop); self.addCleanup(resolver.stop)

    async def test_owner_in_shared_channel_gets_read_tools(self):
        self._setup_session("agent:main:discord:channel:99", "12345")
        async with self._client() as c:
            await c.post("/v1/chat/completions",
                         headers=self.HEADERS, json=self._turn())
        self.assertIn("body__read_file", self.seen_schemas[0])
        self.assertIn("body__search_files", self.seen_schemas[0])

    async def test_non_owner_shared_channel_gets_no_file_tools(self):
        self._setup_session("agent:main:discord:channel:99", "999")
        async with self._client() as c:
            await c.post("/v1/chat/completions",
                         headers=self.HEADERS, json=self._turn())
        self.assertEqual(self.seen_schemas[0], ["body__web_search"])

    async def test_local_desktop_actor_gets_read_tools(self):
        # source='desktop' のローカル面は actor="desktop"=owner → 読取系が届く
        self._setup_session("agent:main:desktop:s1", "desktop")
        async with self._client() as c:
            await c.post("/v1/chat/completions",
                         headers=self.HEADERS, json=self._turn())
        self.assertIn("body__read_file", self.seen_schemas[0])


# --- Hermes plugin（実ファイルから直接 import。companion 非依存） -------------

_PLUGIN_PATH = (
    _support.PKG_DIR / "hermes_plugin" / "companion-approval-audit"
    / "__init__.py")


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "companion_approval_audit", _PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PluginRedactTest(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin()

    def test_masks_kv_secrets(self):
        out = self.plugin.redact(
            "export AWS_SECRET_ACCESS_KEY=AKIA1234567890 && run")
        self.assertIn("AWS_SECRET_ACCESS_KEY=***", out)
        self.assertNotIn("AKIA1234567890", out)

    def test_masks_bearer_and_userinfo_and_long_token(self):
        out = self.plugin.redact(
            "curl -H 'Authorization: Bearer tok123456789' "
            "https://user:hunter2pass@example.com "
            "abcdefghijklmnopqrstuvwxyz0123456789")
        self.assertIn("Bearer ***", out)
        self.assertIn("://***:***@example.com", out)
        self.assertNotIn("hunter2pass", out)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", out)

    def test_known_secret_literal_is_masked(self):
        out = self.plugin.redact(
            "echo the-key-is-SECRETVALUE999", extra_secrets=("SECRETVALUE999",))
        self.assertNotIn("SECRETVALUE999", out)

    def test_bounded_and_whitespace_collapsed(self):
        out = self.plugin.redact("cmd\n" + "a " * 300)
        self.assertLessEqual(len(out), 200)
        self.assertNotIn("\n", out)

    def test_register_hooks(self):
        hooks = {}
        ctx = types.SimpleNamespace(
            register_hook=lambda name, fn: hooks.setdefault(
                name, []).append(fn))
        self.plugin.register(ctx)
        self.assertIn("pre_approval_request", hooks)
        self.assertIn("post_approval_response", hooks)


class PluginForwardTest(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin()

    def _capture(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            resp = mock.MagicMock()
            resp.status = 200
            resp.__enter__.return_value = resp
            return resp

        return captured, mock.patch.object(
            self.plugin.urllib.request, "urlopen",
            side_effect=fake_urlopen)

    def test_forward_posts_redacted_event(self):
        env = {"COMPANION_APPROVAL_URL": "http://127.0.0.1:8788/v1/x",
               "COMPANION_API_KEY": "sekret-key-123"}
        captured, patcher = self._capture()
        with mock.patch.dict("os.environ", env, clear=False), patcher:
            self.plugin._on_pre(
                command="rm -rf data --token sekret-key-123",
                description="danger",
                pattern_key="terminal",
                session_key="agent:main:discord:dm:42",
                session_id="s1", turn_id="t1", tool_call_id="c1",
                surface="gateway")
        body = captured["body"]
        self.assertEqual(body["event"], "pre_approval_request")
        self.assertEqual(body["tool_call_id"], "c1")
        # 生 command は送らず、解決済みの companion 鍵も伏せる
        self.assertNotIn("sekret-key-123", body["command_excerpt"])
        self.assertIn("x-companion-key", {k.lower() for k in
                                        captured["headers"]})

    def test_post_event_carries_choice(self):
        env = {"COMPANION_APPROVAL_URL": "http://127.0.0.1:8788/v1/x"}
        captured, patcher = self._capture()
        with mock.patch.dict("os.environ", env, clear=False), patcher:
            self.plugin._on_post(
                command="ls", pattern_key="terminal", choice="deny",
                decided_by="user", session_key="k")
        self.assertEqual(captured["body"]["event"], "post_approval_response")
        self.assertEqual(captured["body"]["choice"], "deny")
        self.assertEqual(captured["body"]["decided_by"], "user")

    def test_forward_failure_is_fail_open(self):
        with mock.patch.object(
                self.plugin.urllib.request, "urlopen",
                side_effect=OSError("core down")), \
                mock.patch.dict("os.environ",
                                {"COMPANION_APPROVAL_URL": "http://x"},
                                clear=False):
            # 例外が上に出ない（承認経路を壊さない）
            self.plugin._on_pre(command="rm -rf /", pattern_key="terminal")
            self.plugin._on_post(command="rm -rf /", pattern_key="terminal",
                                 choice="timeout")


class BodyPluginInstallTest(unittest.TestCase):
    """body_plugin: 出荷 plugin の HERMES_HOME/plugins への写しと点検"""

    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.bp = self._iso.import_module("companion.body_plugin")

    def test_install_copies_shipped_files(self):
        st = self.bp.install()
        dest = self.bp.installed_dir()
        self.assertTrue((dest / "plugin.yaml").is_file())
        self.assertTrue((dest / "__init__.py").is_file())
        self.assertTrue(st["installed"])
        self.assertFalse(st["drift"])
        self.assertFalse(st["enabled"])   # config.yaml が無いので未登録扱い

    def test_drift_detected(self):
        self.bp.install()
        (self.bp.installed_dir() / "plugin.yaml").write_text(
            "name: tampered", encoding="utf-8")
        self.assertTrue(self.bp.status()["drift"])

    def test_enabled_via_hermes_config(self):
        home = self._iso.cfg.HERMES_HOME
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - companion-approval-audit\n",
            encoding="utf-8")
        self.bp.install()
        st = self.bp.status()
        self.assertTrue(st["enabled"])


if __name__ == "__main__":
    unittest.main()
