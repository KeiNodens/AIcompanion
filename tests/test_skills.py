"""skills.py — Core skill registry と staged import のテスト"""
import unittest
from pathlib import Path

import _support


def _pkg(inbox: Path, name: str, body: str = "手順", desc: str = "説明",
         requires: str = "") -> Path:
    d = inbox / name
    d.mkdir(parents=True)
    req = f"\nrequires_tools: {requires}" if requires else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}{req}\n---\n\n# {name}\n{body}\n",
        encoding="utf-8",
    )
    return d


class SkillsImportTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.skills = self._iso.import_module("companion.skills")

    @property
    def cfg(self):
        return self._iso.cfg

    def test_inbox_package_is_staged(self):
        _pkg(self.cfg.SKILLS_INBOX_DIR, "tea-time")
        res = self.skills.scan_inbox()
        self.assertEqual(res["staged"], ["tea-time"])
        row = self.db.skill_get("tea-time")
        self.assertEqual(row["status"], "staged")
        self.assertEqual(row["origin"], "imported")
        self.assertEqual(row["description"], "説明")
        # パッケージは data/skills/ に配置され inbox からは archive/done へ退避
        # （Hermes のスキル探索が inbox を見るので退避先は探索外）
        self.assertTrue((self.cfg.SKILLS_DIR / "tea-time" / "SKILL.md").is_file())
        self.assertFalse((self.cfg.SKILLS_INBOX_DIR / "tea-time").exists())
        self.assertTrue(
            list((self.cfg.SKILLS_ARCHIVE_DIR / "done").glob("tea-time-*"))
        )
        self.assertFalse((self.cfg.SKILLS_INBOX_DIR / "_done").exists())
        # staged はプロンプト索引に載らない
        self.assertEqual(self.skills.prompt_index(), "")

    def test_bare_md_file_import(self):
        (self.cfg.SKILLS_INBOX_DIR / "memo-hows.md").write_text(
            "---\ndescription: 手順\n---\n\n# hows\n", encoding="utf-8"
        )
        res = self.skills.scan_inbox()
        self.assertEqual(res["staged"], ["memo-hows"])
        row = self.db.skill_get("memo-hows")
        self.assertEqual(row["status"], "staged")
        self.assertTrue(
            (self.cfg.SKILLS_DIR / "memo-hows" / "SKILL.md").is_file()
        )

    def test_support_files_are_copied(self):
        d = _pkg(self.cfg.SKILLS_INBOX_DIR, "pack")
        (d / "references").mkdir()
        (d / "references" / "ref.md").write_text("参照", encoding="utf-8")
        self.skills.scan_inbox()
        self.assertTrue(
            (self.cfg.SKILLS_DIR / "pack" / "references" / "ref.md").is_file()
        )

    def test_invalid_name_is_rejected(self):
        d = self.cfg.SKILLS_INBOX_DIR / "bad"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: ../evil\n---\n\nbody\n", encoding="utf-8"
        )
        res = self.skills.scan_inbox()
        self.assertEqual([n for n, _ in res["rejected"]], ["bad"])
        row = self.db.skill_get("bad")
        self.assertEqual(row["status"], "rejected")
        self.assertIn("invalid skill name", row["note"])
        self.assertFalse((self.cfg.SKILLS_DIR / "bad").exists())
        self.assertTrue(
            list((self.cfg.SKILLS_ARCHIVE_DIR / "rejected").glob("bad-*"))
        )
        self.assertFalse((self.cfg.SKILLS_INBOX_DIR / "_rejected").exists())

    def test_empty_skill_is_rejected(self):
        d = self.cfg.SKILLS_INBOX_DIR / "empty"
        d.mkdir()
        (d / "SKILL.md").write_text("  \n", encoding="utf-8")
        res = self.skills.scan_inbox()
        self.assertEqual(len(res["rejected"]), 1)
        self.assertEqual(res["staged"], [])

    def test_dir_without_skill_md_is_left(self):
        (self.cfg.SKILLS_INBOX_DIR / "random-dir").mkdir()
        res = self.skills.scan_inbox()
        self.assertEqual(res["staged"], [])
        self.assertEqual(res["rejected"], [])
        self.assertTrue((self.cfg.SKILLS_INBOX_DIR / "random-dir").is_dir())

    def test_same_content_redrop_is_skipped(self):
        _pkg(self.cfg.SKILLS_INBOX_DIR, "same")
        self.skills.scan_inbox()
        _pkg(self.cfg.SKILLS_INBOX_DIR, "same")
        res = self.skills.scan_inbox()
        self.assertEqual(res["staged"], [])
        self.assertEqual(res["restaged"], [])
        self.assertFalse((self.cfg.SKILLS_INBOX_DIR / "same").exists())

    def test_changed_content_restages(self):
        _pkg(self.cfg.SKILLS_INBOX_DIR, "ver", body="v1")
        self.skills.scan_inbox()
        self.db.skill_set_status("ver", "active", decided_by="master")
        _pkg(self.cfg.SKILLS_INBOX_DIR, "ver", body="v2")
        res = self.skills.scan_inbox()
        self.assertEqual(res["restaged"], ["ver"])
        row = self.db.skill_get("ver")
        self.assertEqual(row["status"], "staged")
        self.assertIn("v2", (self.cfg.SKILLS_DIR / "ver" / "SKILL.md")
                      .read_text(encoding="utf-8"))
        # 前版との差分行数が通知材料として meta に載る（§7.5）
        self.assertEqual(res["meta"]["ver"]["diff"], (1, 1))

    def test_requires_tools_is_parsed(self):
        _pkg(self.cfg.SKILLS_INBOX_DIR, "need",
             requires="[terminal, web_search]")
        self.skills.scan_inbox()
        row = self.db.skill_get("need")
        self.assertEqual(row["requires_tools"], "terminal,web_search")


    def test_legacy_buckets_are_migrated_out_of_inbox(self):
        """旧 _done/_rejected（inbox 内＝Hermes の探索範囲）は archive へ移す"""
        done = self.cfg.SKILLS_INBOX_DIR / "_done"
        done.mkdir()
        (done / "old-skill-20260101-000000").mkdir()
        rejected = self.cfg.SKILLS_INBOX_DIR / "_rejected"
        rejected.mkdir()
        (rejected / "bad-skill-20260101-000000.md").write_text("x", encoding="utf-8")
        self.skills.scan_inbox()
        self.assertFalse(done.exists())
        self.assertFalse(rejected.exists())
        self.assertTrue(
            (self.cfg.SKILLS_ARCHIVE_DIR / "done" / "old-skill-20260101-000000").is_dir()
        )
        self.assertTrue(
            (self.cfg.SKILLS_ARCHIVE_DIR / "rejected" / "bad-skill-20260101-000000.md").is_file()
        )

    def test_legacy_bucket_name_collision_is_kept(self):
        """archive 側に同名があっても潰さず別名で移す"""
        (self.cfg.SKILLS_ARCHIVE_DIR / "done").mkdir(parents=True)
        (self.cfg.SKILLS_ARCHIVE_DIR / "done" / "dup").mkdir()
        done = self.cfg.SKILLS_INBOX_DIR / "_done"
        done.mkdir()
        (done / "dup").mkdir()
        self.skills.scan_inbox()
        self.assertFalse(done.exists())
        moved = [p for p in (self.cfg.SKILLS_ARCHIVE_DIR / "done").iterdir()
                 if p.name.startswith("dup")]
        self.assertEqual(len(moved), 2)


class SkillsLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.skills = self._iso.import_module("companion.skills")
        # approve/disable は関数層でも owner 判定するので会話相当の actor を立てる
        self.actor = self._iso.import_module("companion.tools.actor")
        self._tok = self.actor.CURRENT_ACTOR.set("desktop")
        self.addCleanup(self.actor.CURRENT_ACTOR.reset, self._tok)
        _pkg(self.cfg.SKILLS_INBOX_DIR, "tea-time", desc="お茶の淹れ方")
        self.skills.scan_inbox()

    @property
    def cfg(self):
        return self._iso.cfg

    def test_approve_activates_and_indexes(self):
        msg = self.skills.approve("tea-time")
        self.assertIn("有効", msg)
        row = self.db.skill_get("tea-time")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["decided_by"], "desktop")
        idx = self.skills.prompt_index()
        self.assertIn("tea-time", idx)
        self.assertIn("お茶の淹れ方", idx)

    def test_approve_denied_without_owner(self):
        """関数層の owner ゲート: actor 未設定の直接呼出し（in-process 実行等）は
        dispatch を通らなくても拒否される。レジストリ正本を守る防衛線"""
        self.actor.CURRENT_ACTOR.set(None)
        self.assertIn("マスター", self.skills.approve("tea-time"))
        self.assertIn("マスター", self.skills.disable("tea-time"))
        row = self.db.skill_get("tea-time")
        self.assertEqual(row["status"], "staged")
        self.assertIsNone(row["decided_by"])

    def test_disable_removes_from_index(self):
        self.skills.approve("tea-time")
        self.skills.disable("tea-time")
        self.assertEqual(self.db.skill_get("tea-time")["status"], "disabled")
        self.assertNotIn("tea-time", self.skills.prompt_index())
        # 再有効化もできる
        self.skills.approve("tea-time")
        self.assertIn("tea-time", self.skills.prompt_index())

    def test_read_returns_body(self):
        row, body, warn = self.skills.read_skill("tea-time")
        self.assertEqual(row["name"], "tea-time")
        self.assertIn("手順", body)
        self.assertEqual(warn, "")

    def test_read_missing(self):
        row, body, warn = self.skills.read_skill("nope")
        self.assertIsNone(row)

    def test_read_warns_on_drift(self):
        md = self.cfg.SKILLS_DIR / "tea-time" / "SKILL.md"
        md.write_text("改ざんされた内容", encoding="utf-8")
        row, body, warn = self.skills.read_skill("tea-time")
        self.assertIn("変わっています", warn)

    def test_approve_refuses_tampered(self):
        md = self.cfg.SKILLS_DIR / "tea-time" / "SKILL.md"
        md.write_text("改ざんされた内容", encoding="utf-8")
        msg = self.skills.approve("tea-time")
        self.assertIn("保留", msg)
        self.assertEqual(self.db.skill_get("tea-time")["status"], "staged")

    def test_approve_unknown(self):
        self.assertIn("見つからない", self.skills.approve("ghost"))

    def test_approve_all_staged_and_multi(self):
        """§7.5 まとめ承認: "all-staged" と複数名を受ける"""
        _pkg(self.cfg.SKILLS_INBOX_DIR, "a")
        _pkg(self.cfg.SKILLS_INBOX_DIR, "b")
        self.skills.scan_inbox()
        msg = self.skills.approve("all-staged")
        self.assertIn("a", msg)
        self.assertIn("b", msg)
        self.assertEqual(self.db.skill_get("a")["status"], "active")
        self.assertEqual(self.db.skill_get("b")["status"], "active")
        self.assertIn("ない", self.skills.approve("all-staged"))
        # カンマ・空白区切りの複数名
        _pkg(self.cfg.SKILLS_INBOX_DIR, "c", body="x")
        _pkg(self.cfg.SKILLS_INBOX_DIR, "d", body="x")
        self.skills.scan_inbox()
        msg = self.skills.approve("c, d ghost")
        self.assertIn("有効", msg)
        self.assertIn("見つからない", msg)
        self.assertEqual(self.db.skill_get("c")["status"], "active")
        self.assertEqual(self.db.skill_get("d")["status"], "active")

    def test_index_compact_over_limit(self):
        """§7.5: active が SKILL_INDEX_MAX を超えたら名前+scope のみに縮む"""
        self.skills.SKILL_INDEX_MAX = 2
        self.addCleanup(setattr, self.skills, "SKILL_INDEX_MAX", 15)
        for i in range(3):
            _pkg(self.cfg.SKILLS_INBOX_DIR, f"s{i}", desc=f"説明{i}")
        self.skills.scan_inbox()
        self.skills.approve("all-staged")
        # tea-time（setUp 由来）＋ s0..s2 = 4件 > 上限2
        idx = self.skills.prompt_index()
        self.assertIn("4件", idx)
        self.assertIn("skill_search", idx)
        self.assertIn("- s0", idx)
        self.assertIn("- s2", idx)
        self.assertNotIn("説明0", idx)

    def test_index_flags_unsatisfied_requires_tools(self):
        """§7.4: requires_tools を満たせない技能は索引に ⚠ が出る。
        body__ 前置や橋渡し glob に合うものは満たせる扱い"""
        _pkg(self.cfg.SKILLS_INBOX_DIR, "need", requires="process_manage")
        _pkg(self.cfg.SKILLS_INBOX_DIR, "ok", requires="web_search")
        _pkg(self.cfg.SKILLS_INBOX_DIR, "ok2", requires="body__browser_x")
        _pkg(self.cfg.SKILLS_INBOX_DIR, "ok3", requires="terminal")
        self.skills.scan_inbox()
        self.skills.approve("all-staged")
        row = self.db.skill_get("need")
        # fake 環境の TOOLS は空、bridge glob は
        # web_search,web_extract,browser_* ＋DM系 terminal,write_file,…
        self.assertEqual(self.skills.missing_tools(row), ["process_manage"])
        self.assertEqual(
            self.skills.missing_tools(self.db.skill_get("ok")), []
        )
        self.assertEqual(
            self.skills.missing_tools(self.db.skill_get("ok2")), []
        )
        idx = self.skills.prompt_index()
        self.assertIn("⚠ 使えないtoolあり: process_manage", idx)
        self.assertNotIn("⚠", idx.split("- ok")[1])  # ok/ok2 行には旗なし

    def test_approve_warns_on_unsatisfied_requires_tools(self):
        """§7.4: 承認時にも requires_tools 不備を通知する（承認自体は止めない）"""
        _pkg(self.cfg.SKILLS_INBOX_DIR, "need", requires="process_manage")
        self.skills.scan_inbox()
        msg = self.skills.approve("need")
        self.assertIn("有効", msg)
        self.assertIn("process_manage", msg)
        self.assertEqual(self.db.skill_get("need")["status"], "active")

    def test_read_skill_file_and_package_list(self):
        """§7.4: file 指定で support file、省略時一覧で同梱ファイルが分かる"""
        d = _pkg(self.cfg.SKILLS_INBOX_DIR, "pack")
        (d / "references").mkdir()
        (d / "references" / "ref.md").write_text("参照本文", encoding="utf-8")
        self.skills.scan_inbox()
        self.db.skill_set_status("pack", "active", decided_by="master")
        row = self.db.skill_get("pack")
        self.assertEqual(
            self.skills.package_files(row),
            ["SKILL.md", "references/ref.md"],
        )
        text, err = self.skills.read_skill_file(row, "references/ref.md")
        self.assertEqual(err, "")
        self.assertIn("参照本文", text)
        _t, err = self.skills.read_skill_file(row, "../secret.txt")
        self.assertIn("パッケージ外", err)
        _t, err = self.skills.read_skill_file(row, "nope.txt")
        self.assertIn("ない", err)

    def test_search_finds_by_text_and_scope(self):
        """§7.5: skill_search は名前・説明・用途・requires_tools を引く"""
        _pkg(self.cfg.SKILLS_INBOX_DIR, "tea-time", desc="お茶の淹れ方")
        _pkg(self.cfg.SKILLS_INBOX_DIR, "cli-work", desc="端末作業",
             requires="terminal")
        self.skills.scan_inbox()
        self.skills.approve("tea-time")
        rows = self.skills.search("お茶")
        self.assertEqual([r["name"] for r in rows], ["tea-time"])
        rows = self.skills.search("terminal")  # requires_tools でも引ける
        self.assertIn("cli-work", [r["name"] for r in rows])
        rows = self.skills.search("無関係な語")
        self.assertEqual(rows, [])

    def test_risk_flags(self):
        """§7.5: terminal/ネットワーク/削除への言及がリスク旗になる"""
        _pkg(self.cfg.SKILLS_INBOX_DIR, "risky",
             body="rm -rf と curl https://x と terminal を使う手順")
        _pkg(self.cfg.SKILLS_INBOX_DIR, "calm", body="静かな手順")
        self.skills.scan_inbox()
        flags = self.skills.risk_flags(self.db.skill_get("risky"))
        self.assertEqual(
            flags, ["terminal系", "ネットワーク", "削除操作"]
        )
        self.assertEqual(
            self.skills.risk_flags(self.db.skill_get("calm")), []
        )


class SkillsDraftTest(unittest.TestCase):
    """skills.draft（skill_draft ツールの中身）: 会話駆動の起票口。
    frontmatter はコード側が組み立て、投函は inbox 経由で通常レーンに乗る"""

    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.db = self._iso.import_module("companion.db")
        self.skills = self._iso.import_module("companion.skills")
        actor = self._iso.import_module("companion.tools.actor")
        self._tok = actor.CURRENT_ACTOR.set("desktop")
        self.addCleanup(actor.CURRENT_ACTOR.reset, self._tok)

    @property
    def cfg(self):
        return self._iso.cfg

    def _inbox_md(self, name: str) -> str:
        return (self.cfg.SKILLS_INBOX_DIR / name / "SKILL.md").read_text(
            encoding="utf-8")

    def test_draft_lands_in_inbox_and_stages_as_self(self):
        msg = self.skills.draft("about-me", "私についての説明",
                                "# 概要\n私は companion だよ")
        self.assertIn("投函", msg)
        md = self._inbox_md("about-me")
        self.assertIn("origin: self", md)
        self.assertIn("name: about-me", md)
        self.assertIn("私は companion だよ", md)
        # scan_inbox の通常レーンに乗る（自動有効化しない）
        res = self.skills.scan_inbox()
        self.assertEqual(res["staged"], ["about-me"])
        row = self.db.skill_get("about-me")
        self.assertEqual(row["origin"], "self")
        self.assertEqual(row["status"], "staged")
        self.assertEqual(row["description"], "私についての説明")

    def test_drafted_skill_full_lane(self):
        """draft → scan → approve で索引に載り skill_read で読める"""
        self.skills.draft("about", "私について", "# 私\ncompanion です")
        self.skills.scan_inbox()
        self.skills.approve("about")
        self.assertIn("about", self.skills.prompt_index())
        _row, body, _warn = self.skills.read_skill("about")
        self.assertIn("companion です", body)

    def test_draft_validates_inputs(self):
        self.assertIn("スキル名", self.skills.draft("../evil", "d", "b"))
        self.assertIn("スキル名", self.skills.draft("", "d", "b"))
        self.assertIn("説明", self.skills.draft("ok-name", "", "b"))
        self.assertIn("説明", self.skills.draft("ok-name", "複数\n行", "b"))
        self.assertIn("本文", self.skills.draft("ok-name", "d", "  "))
        self.assertEqual(list(self.cfg.SKILLS_INBOX_DIR.iterdir()), [])

    def test_draft_normalizes_scopes_and_requires(self):
        self.skills.draft("s1", "d", "b",
                          scopes="[a, b]", requires_tools="x y")
        md = self._inbox_md("s1")
        self.assertIn("scopes: a,b", md)
        self.assertIn("requires_tools: x,y", md)

    def test_draft_size_limit(self):
        big = "x" * (self.cfg.SKILL_MAX_BYTES + 1)
        self.assertIn("大きすぎ", self.skills.draft("big", "d", big))

    def test_draft_redraft_replaces_unprocessed_inbox_copy(self):
        self.skills.draft("rev", "d", "v1")
        self.skills.draft("rev", "d", "v2")
        md = self._inbox_md("rev")
        self.assertIn("v2", md)
        self.assertNotIn("v1", md)

    def test_draft_existing_skill_is_revision(self):
        """同名の登録済み技能への投函は改訂扱い（restage→再承認）"""
        _pkg(self.cfg.SKILLS_INBOX_DIR, "old", body="v1")
        self.skills.scan_inbox()
        self.db.skill_set_status("old", "active", decided_by="master")
        msg = self.skills.draft("old", "改訂", "v2 の中身")
        self.assertIn("再承認", msg)
        res = self.skills.scan_inbox()
        self.assertEqual(res["restaged"], ["old"])
        row = self.db.skill_get("old")
        self.assertEqual(row["status"], "staged")
        self.assertEqual(row["origin"], "self")

    def test_draft_daily_cap(self):
        self.skills.SKILL_DRAFT_MAX = 2
        self.addCleanup(setattr, self.skills, "SKILL_DRAFT_MAX", 5)
        self.skills.draft("d1", "d", "b")
        self.skills.draft("d2", "d", "b")
        msg = self.skills.draft("d3", "d", "b")
        self.assertIn("2 件まで", msg)
        names = sorted(p.name for p in self.cfg.SKILLS_INBOX_DIR.iterdir())
        self.assertEqual(names, ["d1", "d2"])
        # 水位は state.json に刻まれる
        state = self.db.load_state()
        self.assertEqual(int(state["skill_draft_count"]), 2)


if __name__ == "__main__":
    unittest.main()
