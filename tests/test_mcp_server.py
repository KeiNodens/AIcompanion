"""/mcp（runtime-hermes-tooling.md §5 S3、R3）の面分離と3 tool の挙動。

- /mcp は専用鍵 COMPANION_MCP_KEY でのみ開く（/v1 鍵では開かず、逆も同じ）
- 鍵未設定なら面ごと無い扱いで404（素通ししない）
- tools/list は core_context / core_report / core_skill_read の3件
- core_report は「身体からの報告」ターン＋通知キュー登録（ref で冪等）
- core_skill_read は active のみ。file はパッケージ内相対に限定
"""
import hashlib
import unittest

import httpx

import _support

# mcp SDK の DNS rebinding 対策が Host を検証するので、テストでも
# 実際のバインド先（127.0.0.1:8788）を名乗る
_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "Host": "127.0.0.1:8788",
}


class McpServerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []

        async def fake_generate(session_key, user_text, **kw):
            self.calls.append({"session": session_key, "text": user_text, **kw})
            return {"reply": "整えた報告文", "inner": "", "tools": [], "model": "m"}

        async def fake_recall(query):
            return "記憶: 寿司が好き"

        self._iso = _support.IsolatedModules(
            stubs={
                "companion.engine": _support.module_stub(
                    "companion.engine", generate=fake_generate
                ),
                "companion.memory": _support.module_stub(
                    "companion.memory", recall=fake_recall
                ),
            }
        )
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.cfg = self._iso.cfg
        self.cfg.COMPANION_API_AUTH = "enforce"
        self.cfg.COMPANION_API_KEY = "main-key-1"
        self.cfg.COMPANION_MCP_KEY = "mcp-key-1"
        self.server = self._iso.import_module("companion.server")
        self.mcp_server = self._iso.import_module("companion.mcp_server")
        self.db = self._iso.import_module("companion.db")

    def _client(self, app):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    @staticmethod
    def _rpc(method, params=None, rid=1):
        return {"jsonrpc": "2.0", "id": rid, "method": method,
                "params": params or {}}

    async def test_mcp_key_separation(self):
        """/mcp は MCP 専用鍵でのみ開き、/v1 鍵・無鍵は401。MCP 鍵は /v1 を開かない"""
        app = self.server.create_app()
        async with self._client(app) as c:
            r = await c.post("/mcp", json=self._rpc("tools/list"),
                             headers=_MCP_HEADERS)
            self.assertEqual(r.status_code, 401)
            r = await c.post("/mcp", json=self._rpc("tools/list"),
                             headers={**_MCP_HEADERS,
                                      "x-companion-key": "main-key-1"})
            self.assertEqual(r.status_code, 401)
            r = await c.get(
                "/v1/models",
                headers={"x-companion-key": "mcp-key-1"},
            )
            self.assertEqual(r.status_code, 401)

    async def test_mcp_absent_without_key(self):
        """MCP 鍵が解決できなければ面自体が無い扱い（404。素通ししない）"""
        self.cfg.COMPANION_MCP_KEY = ""
        app = self.server.create_app()
        async with self._client(app) as c:
            r = await c.post("/mcp", json=self._rpc("tools/list"),
                             headers=_MCP_HEADERS)
            self.assertEqual(r.status_code, 404)

    async def test_tools_list(self):
        """session manager を lifespan で起動して tools/list を確認"""
        app = self.server.create_app()
        async with app.router.lifespan_context(app):
            async with self._client(app) as c:
                r = await c.post(
                    "/mcp", json=self._rpc("tools/list"),
                    headers={**_MCP_HEADERS, "x-companion-key": "mcp-key-1"},
                )
                # Bearer でも受ける
                r2 = await c.post(
                    "/mcp", json=self._rpc("tools/list", rid=2),
                    headers={**_MCP_HEADERS,
                             "Authorization": "Bearer mcp-key-1"},
                )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        tools = r.json()["result"]["tools"]
        self.assertEqual(
            sorted(t["name"] for t in tools),
            ["core_context", "core_report", "core_skill_read"],
        )
        ann = {t["name"]: (t.get("annotations") or {}) for t in tools}
        self.assertTrue(ann["core_context"].get("readOnlyHint"))
        self.assertTrue(ann["core_skill_read"].get("readOnlyHint"))
        self.assertFalse(ann["core_report"].get("readOnlyHint", False))

    async def test_core_context(self):
        (self.cfg.DATA_DIR / "user_profile.md").write_text(
            "プロフ内容", encoding="utf-8"
        )
        (self.cfg.DATA_DIR / "recent_context.md").write_text(
            "直近内容", encoding="utf-8"
        )
        out = await self.mcp_server.core_context("寿司")
        self.assertIn("寿司", out)
        self.assertIn("プロフ内容", out)
        self.assertIn("直近内容", out)
        # query 空なら記憶検索なしでプロフ系だけ
        out = await self.mcp_server.core_context("")
        self.assertNotIn("寿司", out)
        self.assertIn("プロフ内容", out)

    async def test_core_report(self):
        self.cfg.NOTIFY_CHANNEL_ID = 42
        out = await self.mcp_server.core_report(
            "バックアップ完了", kind="result", ref="run-9"
        )
        self.assertIn("彼女に渡した", out)
        self.assertEqual(self.calls[0]["session"], "desktop")
        self.assertEqual(self.calls[0]["actor"], "desktop")
        self.assertEqual(self.calls[0]["source"], "body")
        self.assertIn("[身体からの報告:result] run-9", self.calls[0]["text"])
        self.assertIn("バックアップ完了", self.calls[0]["text"])
        rows = self.db.due_notifications()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 42)            # channel_id
        self.assertEqual(rows[0][3], "整えた報告文")  # body
        # 同じ ref の配送中重複は冪等に潰れる
        await self.mcp_server.core_report(
            "バックアップ完了", kind="result", ref="run-9"
        )
        self.assertEqual(len(self.db.due_notifications()), 1)

    async def test_core_report_no_channel(self):
        out = await self.mcp_server.core_report("x", ref="r1")
        self.assertIn("通知先が解決できない", out)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.db.due_notifications(), [])

    def test_core_skill_read(self):
        pkg = self.cfg.SKILLS_DIR / "cook"
        pkg.mkdir(parents=True)
        md = pkg / "SKILL.md"
        md.write_text("# cook\n手順本文", encoding="utf-8")
        (pkg / "helper.txt").write_text("支援ファイル", encoding="utf-8")
        chash = hashlib.sha256(md.read_bytes()).hexdigest()
        self.db.skill_stage(
            "cook", "料理", "", "imported", chash, str(pkg), "cook",
            note=None,
        )
        # staged/disabled は出さない
        self.assertIn("staged", self.mcp_server.core_skill_read("cook"))
        self.db.skill_set_status("cook", "active", decided_by="master")
        # §7.4: file 省略時は SKILL.md 本文＋同梱ファイル一覧
        out = self.mcp_server.core_skill_read("cook")
        self.assertIn("手順本文", out)
        self.assertIn("helper.txt", out)
        self.assertIn(
            "支援ファイル",
            self.mcp_server.core_skill_read("cook", file="helper.txt"),
        )
        # パッケージ外への参照は拒否（".." は外判定、絶対パスは
        # 先頭を削ってパッケージ内に落ちて「ない」になる。どちらも出さない）
        self.assertIn(
            "パッケージ外",
            self.mcp_server.core_skill_read("cook", file="../secret.txt"),
        )
        self.assertIn(
            "ない",
            self.mcp_server.core_skill_read("cook", file="/abs/path"),
        )
        self.assertIn(
            "見つからない", self.mcp_server.core_skill_read("missing")
        )


if __name__ == "__main__":
    unittest.main()
