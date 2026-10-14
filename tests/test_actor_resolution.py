"""R2 話者解決: x-companion-session → Hermes state.db → user_id → actor

- session_identity: 物理 sessions.id と gwk_<digest> scope の両方を逆引き
- resolve_actor: user_id があればそれ、ローカル面のみ "desktop"、
  解決不能・リモート面で user_id 無しは None（非owner）
- _actor_for_api: ヘッダ無しは "desktop"、mode=legacy/off の分岐

state.db は偽の SQLite ファイルを HERMES_HOME に作って本物の読み取り
経路を通す（mode=ro で開くことも含めて）。
"""
import hashlib
import sqlite3
import types
import unittest
from unittest import mock

import _support


def _mk_state_db(path, rows=(), generations=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT,"
        " session_key TEXT, user_id TEXT, started_at REAL)"
    )
    conn.execute(
        "CREATE TABLE conversation_generations (source TEXT,"
        " session_key TEXT, generation INTEGER,"
        " PRIMARY KEY (source, session_key))"
    )
    conn.executemany(
        "INSERT INTO sessions (id, source, session_key, user_id,"
        " started_at) VALUES (?,?,?,?,?)", rows)
    conn.executemany(
        "INSERT INTO conversation_generations"
        " (source, session_key, generation) VALUES (?,?,?)", generations)
    conn.commit()
    conn.close()


def _gwk(source, key, generation=""):
    """Hermes の declared_conversation_scope と同じ digest を作る"""
    digest = hashlib.sha256(
        f"{source}|{key}|{generation}".encode("utf-8")).hexdigest()[:24]
    return f"gwk_{digest}"


class HermesSessionIdentityTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.hermes = self._iso.import_module("companion.runtime.hermes")
        self.body = self._iso.import_module("companion.runtime.body")
        self.state_db = self._iso.tmp / "hermes" / "state.db"

    def test_physical_session_id_resolves_user(self):
        _mk_state_db(self.state_db, rows=[
            ("sess-1", "discord", "agent:main:discord:dm:42", "12345", 1.0)])
        ident = self.hermes.session_identity("sess-1")
        self.assertEqual(ident["user_id"], "12345")
        self.assertEqual(ident["source"], "discord")
        self.assertEqual(self.body.resolve_actor("sess-1"), "12345")

    def test_gwk_scope_reverse_resolves_latest_speaker(self):
        key = "agent:main:discord:dm:42"
        _mk_state_db(
            self.state_db,
            rows=[("sess-1", "discord", key, "12345", 1.0),
                  ("sess-2", "discord", key, "99999", 2.0)],
            generations=[("discord", key, 2)],
        )
        ident = self.hermes.session_identity(_gwk("discord", key, "2"))
        # 同じ会話キーの最新行（=最後に発言した話者）を指す
        self.assertEqual(ident["session_id"], "sess-2")
        self.assertEqual(ident["user_id"], "99999")

    def test_gwk_without_boundary_also_resolves(self):
        """boundary 未発行の会話は generation="" の digest で来る"""
        key = "agent:main:discord:dm:7"
        _mk_state_db(self.state_db, rows=[
            ("sess-9", "discord", key, "12345", 1.0)])
        ident = self.hermes.session_identity(_gwk("discord", key))
        self.assertEqual(ident["user_id"], "12345")

    def test_unknown_or_missing_db_fails_closed(self):
        _mk_state_db(self.state_db)
        self.assertIsNone(self.hermes.session_identity("no-such"))
        self.assertIsNone(self.hermes.session_identity(_gwk("x", "y")))
        self.assertIsNone(self.body.resolve_actor("no-such"))
        # state.db 自体が無くても落ちずに非ownerへ倒れる
        self.assertIsNone(self.body.resolve_actor("sess-1"))

    def test_local_source_without_user_falls_back_desktop(self):
        _mk_state_db(self.state_db, rows=[
            ("cli-1", "cli", "agent:main:cli", None, 1.0)])
        self.assertEqual(self.body.resolve_actor("cli-1"), "desktop")

    def test_remote_source_without_user_is_not_owner(self):
        _mk_state_db(self.state_db, rows=[
            ("d-1", "discord", "k", None, 1.0)])
        self.assertIsNone(self.body.resolve_actor("d-1"))


class ActorForApiTest(unittest.TestCase):
    """server._actor_for_api のモード分岐（state.db 解決は差し替え）"""

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
        self.actor_result = "12345"
        resolver = mock.patch.object(
            self.server.body_rt, "resolve_actor",
            side_effect=lambda _aff: self.actor_result)
        resolver.start()
        self.addCleanup(resolver.stop)

    def _req(self, affinity=None):
        headers = {}
        if affinity is not None:
            headers["x-companion-session"] = affinity
        return types.SimpleNamespace(headers=headers)

    def test_no_header_is_desktop(self):
        self.assertEqual(self.server._actor_for_api(self._req()), "desktop")

    def test_auto_mode_uses_resolved_actor(self):
        self.assertEqual(
            self.server._actor_for_api(self._req("gwk_x")), "12345")
        self.actor_result = None
        self.assertIsNone(self.server._actor_for_api(self._req("gwk_x")))

    def test_legacy_mode_is_always_desktop(self):
        with mock.patch.object(self.server, "COMPANION_ACTOR_MODE", "legacy"):
            self.actor_result = None
            self.assertEqual(
                self.server._actor_for_api(self._req("gwk_x")), "desktop")

    def test_off_mode_drops_user_ids(self):
        """off は解決経路の検証用: user_id を採用せずローカル面のみ desktop"""
        with mock.patch.object(self.server, "COMPANION_ACTOR_MODE", "off"):
            self.assertIsNone(
                self.server._actor_for_api(self._req("gwk_x")))
            self.actor_result = "desktop"
            self.assertEqual(
                self.server._actor_for_api(self._req("gwk_x")), "desktop")


if __name__ == "__main__":
    unittest.main()
