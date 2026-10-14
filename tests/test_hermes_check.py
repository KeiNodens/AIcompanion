"""hermes_conf.py / hermes_check.py — Hermes config 読取と lint のテスト"""
import textwrap
import unittest
from unittest import mock

import _support

# aux タスク一覧の元になる最小の config_defaults.py（ast.parse するだけで
# 実行はしないので _aux は未定義でよい）
_CONFIG_DEFAULTS_SRC = textwrap.dedent('''\
    DEFAULT_CONFIG = {
        "model": "",
        "auxiliary": {
            "transient_retries": 2,
            "free_only": False,
            "stream_only_base_urls": [],
            "vision": _aux(30),
            "compression": {"provider": "auto", "model": ""},
        },
    }
''')


def _write_src(src_dir):
    d = src_dir / "hermes_cli"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config_defaults.py").write_text(_CONFIG_DEFAULTS_SRC, encoding="utf-8")


def _cfg(**over):
    """「素直に全部正しい」Hermes config。aux は vision/compression のみ
    pin 済み（_CONFIG_DEFAULTS_SRC のタスク集合と一致させる）"""
    base = {
        "model": {"provider": "custom",
                  "base_url": "http://localhost:8788/v1",
                  "api_mode": "chat_completions",
                  "default": "companion"},
        "auxiliary": {
            "vision": {"provider": "custom",
                       "base_url": "http://localhost:8000/v1",
                       "model": "aux-model"},
            "compression": {"provider": "custom",
                            "base_url": "http://localhost:8000/v1",
                            "model": "aux-model"},
        },
        "delegation": {"provider": "custom",
                       "base_url": "http://localhost:8000/v1",
                       "model": "aux-model"},
        "platforms": {"api_server": {"enabled": True, "extra": {
            "key": "test-key",   # fake_config.HERMES_API_KEY と一致させる
            "model_routes": {"body": {"model": "aux-model"}}}}},
        "providers": {"companion-api": {
            "base_url": "http://localhost:8788/v1",
            "session_affinity_header": "x-companion-session"}},
    }
    base.update(over)
    return base


def _levels(findings):
    return [f.level for f in findings]


def _keys(findings):
    return [f.key for f in findings]


class HermesConfTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.hc = self._iso.import_module("companion.hermes_conf")

    @property
    def cfg(self):
        return self._iso.cfg

    def test_targets_companion(self):
        self.assertTrue(self.hc.targets_companion("http://localhost:8788/v1"))
        self.assertTrue(self.hc.targets_companion("http://127.0.0.1:8788"))
        self.assertTrue(self.hc.targets_companion("http://[::1]:8788/v1"))
        self.assertFalse(self.hc.targets_companion("http://localhost:8000/v1"))
        self.assertFalse(self.hc.targets_companion("http://example.com:8788"))
        self.assertFalse(self.hc.targets_companion("http://localhost"))  # port無し
        self.assertFalse(self.hc.targets_companion(""))
        self.assertFalse(self.hc.targets_companion(None))

    def test_resolve_env_var(self):
        self.cfg.HERMES_HOME.mkdir(parents=True, exist_ok=True)
        (self.cfg.HERMES_HOME / ".env").write_text(
            "MY_TOKEN=from-dotenv\n", encoding="utf-8"
        )
        self.assertEqual(self.hc.resolve("${MY_TOKEN}"), "from-dotenv")
        self.assertEqual(self.hc.resolve("plain"), "plain")
        self.assertEqual(self.hc.resolve("${NO_SUCH_VAR}"), "")
        self.assertEqual(self.hc.resolve(123), "123")

    def test_load_config_missing_is_empty(self):
        self.assertEqual(self.hc.load_config(), {})

    def test_load_config_reads_and_caches(self):
        self.cfg.HERMES_HOME.mkdir(parents=True, exist_ok=True)
        p = self.cfg.HERMES_HOME / "config.yaml"
        p.write_text("model:\n  base_url: http://localhost:8788/v1\n",
                     encoding="utf-8")
        self.assertEqual(
            self.hc.load_config()["model"]["base_url"],
            "http://localhost:8788/v1",
        )
        p.write_text("model:\n  base_url: http://localhost:9999/v1\n",
                     encoding="utf-8")
        self.assertEqual(
            self.hc.load_config()["model"]["base_url"],
            "http://localhost:9999/v1",
        )

    def test_api_server_key_priority(self):
        # 1. companion env の HERMES_API_KEY が最優先
        self.assertEqual(self.hc.api_server_key(), ("test-key", "env"))
        # 2. env が空なら Hermes config の platforms.api_server.extra.key
        self.cfg.HERMES_API_KEY = ""
        self.cfg.HERMES_HOME.mkdir(parents=True, exist_ok=True)
        (self.cfg.HERMES_HOME / "config.yaml").write_text(
            "platforms:\n  api_server:\n    extra:\n      key: cfg-key\n",
            encoding="utf-8",
        )
        self.assertEqual(self.hc.api_server_key(), ("cfg-key", "hermes-config"))
        # 3. config にも無ければ Hermes .env の API_SERVER_KEY
        (self.cfg.HERMES_HOME / "config.yaml").write_text("model: {}\n",
                                                          encoding="utf-8")
        (self.cfg.HERMES_HOME / ".env").write_text(
            "API_SERVER_KEY=dotenv-key\n", encoding="utf-8"
        )
        self.assertEqual(self.hc.api_server_key(), ("dotenv-key", "hermes-env"))
        (self.cfg.HERMES_HOME / ".env").write_text("# empty\n",
                                                   encoding="utf-8")
        self.assertEqual(self.hc.api_server_key(), ("", ""))

    def test_api_server_key_resolves_var(self):
        self.cfg.HERMES_API_KEY = ""
        self.cfg.HERMES_HOME.mkdir(parents=True, exist_ok=True)
        (self.cfg.HERMES_HOME / "config.yaml").write_text(
            "platforms:\n  api_server:\n    extra:\n      key: ${MY_KEY}\n",
            encoding="utf-8",
        )
        (self.cfg.HERMES_HOME / ".env").write_text("MY_KEY=resolved\n",
                                                    encoding="utf-8")
        self.assertEqual(self.hc.api_server_key(), ("resolved", "hermes-config"))

    def test_companion_key_from_provider_extra_headers(self):
        self.cfg.HERMES_HOME.mkdir(parents=True, exist_ok=True)
        (self.cfg.HERMES_HOME / "config.yaml").write_text(
            "providers:\n"
            "  companion-api:\n"
            "    base_url: http://localhost:8788/v1\n"
            "    extra_headers:\n"
            "      X-Companion-Key: hdr-key\n"
            "  other:\n"
            "    base_url: http://localhost:8000/v1\n"
            "    extra_headers:\n"
            "      x-companion-key: wrong-port\n",
            encoding="utf-8",
        )
        self.assertEqual(self.hc.companion_key(), ("hdr-key", "hermes-config"))
        # env の COMPANION_API_KEY があればそちらが優先
        self.cfg.COMPANION_API_KEY = "env-key"
        self.assertEqual(self.hc.companion_key(), ("env-key", "env"))


class HermesCheckTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.hc = self._iso.import_module("companion.hermes_conf")
        self.chk = self._iso.import_module("companion.hermes_check")
        self.bp = self._iso.import_module("companion.body_plugin")
        self.src = self._iso.tmp / "hermes-src"
        _write_src(self.src)
        # §7.1/§7.2 の配線（create_dir=inbox、external_dirs∋export）が
        # 正しい形。欠ける場合はそれぞれ warn が出る
        self._skills_cfg = {
            "create_dir": str(self._iso.cfg.SKILLS_INBOX_DIR),
            "external_dirs": [str(self._iso.cfg.SKILLS_EXPORT_DIR)],
        }
        # R5b: approval-audit plugin は導入済み＋enabled がクリーン状態
        self.bp.install()
        self._plugin_cfg = {"enabled": [self.bp.PLUGIN_NAME]}

    def _findings(self, cfg):
        cfg = dict(cfg)
        cfg.setdefault("skills", dict(self._skills_cfg))
        cfg.setdefault("plugins", dict(self._plugin_cfg))
        return self.chk.check(cfg, src=self.src)

    def test_aux_task_names_from_ast(self):
        names = self.chk.aux_task_names(self.src)
        # Dict/Call 値だけ拾い、スカラー(transient_retries等)は除外
        self.assertEqual(names, {"vision", "compression"})

    def test_aux_task_names_fallback_on_missing(self):
        names = self.chk.aux_task_names(self._iso.tmp / "no-src")
        self.assertIn("compression", names)
        self.assertIn("vision", names)
        self.assertNotIn("transient_retries", names)

    def test_aux_task_names_fallback_on_broken(self):
        d = self._iso.tmp / "broken" / "hermes_cli"
        d.mkdir(parents=True)
        (d / "config_defaults.py").write_text("def (((", encoding="utf-8")
        names = self.chk.aux_task_names(self._iso.tmp / "broken")
        self.assertEqual(names, set(self.chk._AUX_FALLBACK))

    def test_clean_config_has_no_error_or_warn(self):
        findings = self._findings(_cfg())
        self.assertNotIn("error", _levels(findings))
        self.assertNotIn("warn", _levels(findings))
        # companion 入口鍵の info だけが出る
        self.assertEqual(_keys(findings), ["companion_key"])

    def test_api_mode_error(self):
        cfg = _cfg()
        cfg["model"]["api_mode"] = "responses"
        findings = self._findings(cfg)
        err = [f for f in findings if f.level == "error"]
        self.assertIn("model.api_mode", _keys(err))
        self.assertIn("404", err[0].message)

    def test_aux_unpinned_warns(self):
        cfg = _cfg()
        cfg["auxiliary"]["compression"] = {"provider": "auto", "model": ""}
        findings = self._findings(cfg)
        self.assertIn("auxiliary.compression",
                      _keys(f for f in findings if f.level == "warn"))
        # entry 自体が無いタスクも継承として warn
        cfg["auxiliary"] = {}
        findings = self._findings(cfg)
        warns = _keys(f for f in findings if f.level == "warn")
        self.assertIn("auxiliary.vision", warns)
        self.assertIn("auxiliary.compression", warns)

    def test_aux_targeting_companion_is_error(self):
        cfg = _cfg()
        cfg["auxiliary"]["vision"]["base_url"] = "http://localhost:8788/v1"
        findings = self._findings(cfg)
        self.assertIn("auxiliary.vision",
                      _keys(f for f in findings if f.level == "error"))

    def test_aux_not_checked_when_main_is_not_companion(self):
        cfg = _cfg()
        cfg["model"]["base_url"] = "http://localhost:8000/v1"
        cfg["model"]["api_mode"] = "responses"   # companion 以外なら OK
        cfg["auxiliary"] = {}
        findings = self._findings(cfg)
        self.assertNotIn("error", _levels(findings))
        self.assertNotIn("warn", _levels(findings))

    def test_delegation_inherit_warns(self):
        cfg = _cfg()
        cfg["delegation"] = {}
        findings = self._findings(cfg)
        self.assertIn("delegation",
                      _keys(f for f in findings if f.level == "warn"))

    def test_delegation_targeting_companion_warns(self):
        cfg = _cfg()
        cfg["delegation"]["base_url"] = "http://localhost:8788/v1"
        findings = self._findings(cfg)
        self.assertIn("delegation.base_url",
                      _keys(f for f in findings if f.level == "warn"))

    def test_api_server_enabled_without_key_is_error(self):
        findings = self._findings(_cfg())
        self.assertNotIn("platforms.api_server.extra.key", _keys(findings))
        with mock.patch.object(self.hc, "api_server_key",
                               return_value=("", "")):
            findings = self._findings(_cfg())
        self.assertIn("platforms.api_server.extra.key",
                      _keys(f for f in findings if f.level == "error"))

    def test_api_key_mismatch_is_error(self):
        """companion env 側の鍵と Hermes config の鍵が食い違うと error。
        鍵の値は文言に出さない"""
        cfg = _cfg()
        cfg["platforms"]["api_server"]["extra"]["key"] = "different-secret"
        findings = self._findings(cfg)
        err = [f for f in findings
               if f.level == "error"
               and f.key == "platforms.api_server.extra.key"]
        self.assertEqual(len(err), 1)
        self.assertIn("食い違", err[0].message)
        self.assertNotIn("different-secret", err[0].message)
        self.assertNotIn("test-key", err[0].message)

    def test_api_key_match_or_empty_is_fine(self):
        # 一致していれば findings なし（_cfg は env の test-key と一致）
        findings = self._findings(_cfg())
        self.assertNotIn("platforms.api_server.extra.key", _keys(findings))
        # env 側が空なら食い違い判定はしない
        self._iso.cfg.HERMES_API_KEY = ""
        cfg = _cfg()
        cfg["platforms"]["api_server"]["extra"]["key"] = "other-key"
        findings = self._findings(cfg)
        errs = [f for f in findings
                if f.key == "platforms.api_server.extra.key"
                and "食い違" in f.message]
        self.assertEqual(errs, [])

    def test_model_routes_missing_warns(self):
        cfg = _cfg()
        cfg["platforms"]["api_server"]["extra"]["model_routes"] = {}
        findings = self._findings(cfg)
        self.assertIn("platforms.api_server.extra.model_routes",
                      _keys(f for f in findings if f.level == "warn"))

    def test_session_affinity_header_missing_warns(self):
        cfg = _cfg()
        cfg["providers"]["companion-api"] = {
            "base_url": "http://localhost:8788/v1"
        }
        findings = self._findings(cfg)
        self.assertIn("providers.companion-api.session_affinity_header",
                      _keys(f for f in findings if f.level == "warn"))

    def test_skills_dirs_warn(self):
        """§7.1/§7.2 の配線が欠けると warn になる"""
        cfg = _cfg()
        cfg["skills"] = {}
        findings = self._findings(cfg)
        warns = _keys(f for f in findings if f.level == "warn")
        self.assertIn("skills.create_dir", warns)
        self.assertIn("skills.external_dirs", warns)
        # create_dir が別所を向く／external_dirs に export が無い形も同様
        cfg["skills"] = {
            "create_dir": str(self._iso.tmp / "elsewhere"),
            "external_dirs": ["C:/other/dir"],
        }
        findings = self._findings(cfg)
        warns = _keys(f for f in findings if f.level == "warn")
        self.assertIn("skills.create_dir", warns)
        self.assertIn("skills.external_dirs", warns)
        # SKILLS_EXPORT=0 なら external_dirs 未登録でも warn は出ない
        self._iso.cfg.SKILLS_EXPORT = False
        findings = self._findings(cfg)
        warns = _keys(f for f in findings if f.level == "warn")
        self.assertNotIn("skills.external_dirs", warns)

    def test_companion_key_info(self):
        findings = self._findings(_cfg())
        self.assertIn("companion_key",
                      _keys(f for f in findings if f.level == "info"))
        with mock.patch.object(self.hc, "companion_key",
                               return_value=("x", "env")):
            findings = self._findings(_cfg())
        self.assertNotIn("companion_key", _keys(findings))

    def test_approval_audit_plugin_findings(self):
        """R5b: approval-audit plugin の未導入/ずれ/未有効化で warn"""
        # setUp で導入済み+enabled がクリーン。まず全て揃っていることを確認
        findings = self._findings(_cfg())
        self.assertNotIn("plugins.enabled", _keys(findings))
        # plugins.enabled から外す → warn
        cfg = _cfg()
        cfg["plugins"] = {"enabled": []}
        findings = self._findings(cfg)
        self.assertIn("plugins.enabled",
                      _keys(f for f in findings if f.level == "warn"))
        # disabled も warn
        cfg["plugins"] = {"enabled": [self.bp.PLUGIN_NAME],
                          "disabled": [self.bp.PLUGIN_NAME]}
        findings = self._findings(cfg)
        self.assertIn("plugins.disabled",
                      _keys(f for f in findings if f.level == "warn"))
        # 写しがずれる → warn
        (self.bp.installed_dir() / "plugin.yaml").write_text(
            "name: old", encoding="utf-8")
        findings = self._findings(_cfg())
        self.assertIn("plugins.enabled",
                      _keys(f for f in findings if f.level == "warn"))
        # 未導入 → warn
        import shutil
        shutil.rmtree(self.bp.installed_dir())
        findings = self._findings(_cfg())
        self.assertIn("plugins.enabled",
                      _keys(f for f in findings if f.level == "warn"))

    def test_findings_never_contain_secret(self):
        cfg = _cfg()
        cfg["platforms"]["api_server"]["extra"]["key"] = "supersecret123"
        cfg["providers"]["companion-api"]["extra_headers"] = {
            "x-companion-key": "othersecret456"
        }
        cfg["auxiliary"]["vision"]["api_key"] = "auxsecret789"
        findings = self._findings(cfg)
        blob = " ".join(f"{f.key} {f.message}" for f in findings)
        self.assertNotIn("supersecret123", blob)
        self.assertNotIn("othersecret456", blob)
        self.assertNotIn("auxsecret789", blob)


if __name__ == "__main__":
    unittest.main()
