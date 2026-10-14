"""Hermes bot profile を companion 経由にする provisioning（案A）。

Desktop/CLI で作った Hermes profile（~hermes_home/profiles/<name>/）の
config.yaml に、companion 経由に必要な3点を追記・補完する。

profiles.create が継承するのは model ブロックだけで、以下は継承されない:

- providers（companion 向けエントリ: session_affinity_header +
  x-companion-key）。無いと入口認証（COMPANION_API_AUTH=enforce）で 401
- auxiliary — 補助呼出し（圧縮・タイトル・vision等）の pin。無いと
  主モデル（=companion :8788）に fallback し、彼女の口調の要約が
  返って turns/記憶を汚す
- delegation — サブエージェントの pin。無いと子エージェントも
  companion を呼ぶ

本体ロジックは companion/profile_provision.py にあり、稼働中の
companion は scheduler tick で全 profile を自動補完する
（PROFILE_PROVISION / PROFILE_PROVISION_MIN）。このスクリプトは
特定 profile を今すぐ直したいときの手動口（作成直後に使えば
初回メッセージから正しく繋がる）。

.env の COMPANION_API_KEY は作成時に mirror 済みのはずだが、無ければ警告。

使い方:
  python scripts/bot_profile_setup.py <profile_name>          # 適用
  python scripts/bot_profile_setup.py <profile_name> --check  # 確認のみ
"""
import argparse
import sys
from pathlib import Path

import yaml
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion import config, profile_provision  # noqa: E402


def setup_profile(name: str, check_only: bool = False) -> int:
    pdir = config.HERMES_HOME / "profiles" / name
    if not pdir.is_dir():
        print(f"profile が見つかりません: {pdir}\n"
              "先に Desktop/CLI で作成してください", file=sys.stderr)
        return 1
    cfg_path = pdir / "config.yaml"
    existing = {}
    if cfg_path.is_file():
        try:
            existing = yaml.safe_load(
                cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            print(f"config.yaml が読めません: {e}", file=sys.stderr)
            return 1

    want = profile_provision._wanted()
    if not want.get("providers"):
        print("警告: launch config に companion 向け providers がありません",
              file=sys.stderr)
    if not want.get("auxiliary"):
        print("警告: launch config に auxiliary がありません", file=sys.stderr)

    if check_only:
        preview = dict(existing)
        added = profile_provision._merge(preview, want)
        print(f"[{name}] 追加されるキー: {', '.join(added) or 'なし'}")
        return 0

    added = profile_provision.provision_profile(pdir, want)
    model = existing.get("model") or {}
    if not profile_provision._targets_companion(model):
        print(f"注意: {name} の model が companion を向いていません"
              f"（{model.get('base_url') or model.get('default') or '未設定'}）"
              " — companion 経由にするには model ブロックを確認してください",
              file=sys.stderr)

    env_key = dotenv_values(pdir / ".env").get("COMPANION_API_KEY", "")
    if not env_key:
        print("警告: profile .env に COMPANION_API_KEY がありません"
              "（mirror されていない？）", file=sys.stderr)

    if not added:
        print("変更なし（既に揃っているか companion 向きではありません）")
        return 0
    print(f"[{name}] 追加: {', '.join(added)}\n書き込みました: {cfg_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("profile", help="Hermes profile 名")
    ap.add_argument("--check", action="store_true",
                    help="書き込まず確認だけする")
    args = ap.parse_args()
    return setup_profile(args.profile, check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
