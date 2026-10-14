"""profile_provision.py のテスト — Hermes bot profile の自動補完。

偽 HERMES_HOME に launch config.yaml と profiles/<name>/config.yaml を
作り、companion 向きの profile だけが補完されることを見る。
"""
import unittest

import yaml

import _support


LAUNCH = {
    "model": {
        "provider": "custom", "default": "companion",
        "base_url": "http://localhost:8788/v1", "api_key": "local",
        "api_mode": "chat_completions",
    },
    "providers": {
        "companion-api": {
            "base_url": "http://localhost:8788/v1",
            "session_affinity_header": "x-companion-session",
            "extra_headers": {"x-companion-key": "${COMPANION_API_KEY}"},
        },
        "other-svc": {
            "base_url": "http://localhost:9999/v1",
            "extra_headers": {"x-other": "1"},
        },
    },
    "auxiliary": {
        "compression": {"base_url": "http://localhost:8000/v1",
                        "model": "gpt-5.6-luna"},
        "title": {"base_url": "http://localhost:8000/v1",
                  "model": "gpt-5.6-luna"},
    },
    "platforms": {
        "api_server": {
            "extra": {
                "model_routes": {
                    "body": {"model": "gpt-5.6-luna",
                             "base_url": "http://localhost:8000/v1",
                             "api_key": "local"},
                },
            },
        },
    },
}


def _write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True),
                    encoding="utf-8")


def _read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class ProvisionTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.pp = self._iso.import_module("companion.profile_provision")
        self.home = self._iso.tmp / "hermes"
        _write_yaml(self.home / "config.yaml", LAUNCH)

    def _mk_profile(self, name, cfg):
        _write_yaml(self.home / "profiles" / name / "config.yaml", cfg)
        return self.home / "profiles" / name

    def test_missing_keys_are_added(self):
        """継承された model だけの profile に3点が補完される"""
        pdir = self._mk_profile("bot1", {"model": dict(LAUNCH["model"])})
        added = self.pp.provision_profile(pdir)
        self.assertIn("providers", added)
        self.assertIn("auxiliary", added)
        self.assertIn("delegation", added)
        cfg = _read(pdir / "config.yaml")
        prov = cfg["providers"]["companion-api"]
        self.assertEqual(prov["session_affinity_header"],
                         "x-companion-session")
        # :8788 以外の providers エントリは持ち込まない
        self.assertNotIn("other-svc", cfg["providers"])
        self.assertEqual(cfg["delegation"]["model"], "gpt-5.6-luna")
        self.assertEqual(cfg["auxiliary"]["title"]["model"], "gpt-5.6-luna")

    def test_non_companion_profile_untouched(self):
        """companion 以外のモデルを向く profile は対象外"""
        pdir = self._mk_profile("plain", {
            "model": {"provider": "custom", "default": "gpt-5.6-luna",
                      "base_url": "http://localhost:8000/v1"},
        })
        self.assertEqual(self.pp.provision_profile(pdir), [])
        self.assertEqual(_read(pdir / "config.yaml")["model"]["default"],
                         "gpt-5.6-luna")

    def test_no_model_block_skipped(self):
        pdir = self._mk_profile("empty", {"display": {"skin": "mono"}})
        self.assertEqual(self.pp.provision_profile(pdir), [])

    def test_existing_values_not_overwritten(self):
        """profile が既に持つ値は尊重される（部分補完のみ）"""
        pdir = self._mk_profile("bot2", {
            "model": dict(LAUNCH["model"]),
            "auxiliary": {"title": {"model": "custom-model"}},
        })
        added = self.pp.provision_profile(pdir)
        cfg = _read(pdir / "config.yaml")
        # title は既存を維持、compression だけ追加
        self.assertEqual(cfg["auxiliary"]["title"]["model"], "custom-model")
        self.assertIn("auxiliary.compression", added)
        self.assertNotIn("auxiliary.title", added)

    def test_idempotent(self):
        pdir = self._mk_profile("bot3", {"model": dict(LAUNCH["model"])})
        first = self.pp.provision_profile(pdir)
        self.assertTrue(first)
        self.assertEqual(self.pp.provision_profile(pdir), [])

    def test_scan_all_profiles(self):
        self._mk_profile("a", {"model": dict(LAUNCH["model"])})
        self._mk_profile("b", {
            "model": {"default": "x", "base_url": "http://x/v1"}})
        changed = self.pp.ensure_companion_profiles(force=True)
        self.assertEqual(list(changed), ["a"])

    def test_interval_throttle(self):
        self._mk_profile("a", {"model": dict(LAUNCH["model"])})
        first = self.pp.ensure_companion_profiles(force=True)
        self.assertIn("a", first)
        self._mk_profile("b", {"model": dict(LAUNCH["model"])})
        # 間隔内の非 force 呼び出しは走査しない
        self.assertEqual(self.pp.ensure_companion_profiles(), {})
        # force は間隔を無視
        self.assertIn("b", self.pp.ensure_companion_profiles(force=True))


if __name__ == "__main__":
    unittest.main()
