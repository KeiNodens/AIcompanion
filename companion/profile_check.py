"""profile 別ポリシーの診断口（`python -m companion.profile_check`）。

引数なし: profiles/ 配下と profile_policy.yaml に出てくる profile を
一覧し、companion 向きか・ポリシー登録があるかを並べる。

引数あり: その profile の有効な設定を展開して表示する ——
- policy.yaml の節（default への fallback 込みの「実効」値）
- 実効 bridge glob（常時/DM 限定）・skills 許可 glob・見える active 技能
- profile 側ファイル: config.yaml の providers/auxiliary/delegation、
  .env の COMPANION_API_KEY の有無（値は出さない）
- Hermes state.db でその profile_name を持つ直近セッション

秘密値（鍵・トークン）は一切表示しない。
"""
import sqlite3
import sys

import yaml

from . import config, db, profile_policy, profile_provision

_KNOWN_KEYS = {"bridge_tools", "bridge_dm_tools", "skills"}


def _policy_unknown_keys() -> list[str]:
    """policy.yaml 内の未定義キー（typo 検出用）。["section.key", ...]"""
    cfg = profile_policy.load()
    out = []
    for top in ("default", "profiles"):
        sec = cfg.get(top)
        if top == "default" and isinstance(sec, dict):
            out += [f"default.{k}" for k in sec if k not in _KNOWN_KEYS]
        if top == "profiles" and isinstance(sec, dict):
            for name, entry in sec.items():
                if isinstance(entry, dict):
                    out += [f"profiles.{name}.{k}"
                            for k in entry if k not in _KNOWN_KEYS]
                elif entry is not None:
                    out.append(f"profiles.{name}（節が dict ではない）")
    return out


def _known_profile_names() -> set[str]:
    """profiles/ のディレ名 + "default"（launch profile）"""
    names = {"default"}
    root = config.HERMES_HOME / "profiles"
    if root.is_dir():
        names |= {p.name for p in root.iterdir() if p.is_dir()}
    return names


def _profile_files(name: str) -> dict:
    """profile 側ファイルの状況。launch profile("default")は HERMES_HOME 直下"""
    home = (config.HERMES_HOME if name == "default"
            else config.HERMES_HOME / "profiles" / name)
    out = {"dir": str(home), "exists": home.is_dir()}
    cfg_path = home / "config.yaml"
    if cfg_path.is_file():
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            cfg = {}
        if isinstance(cfg, dict):
            model = cfg.get("model") or {}
            out["targets_companion"] = (
                profile_provision._targets_companion(model))
            out["providers"] = bool(
                profile_provision._companion_providers(cfg))
            out["auxiliary"] = bool(cfg.get("auxiliary"))
            out["delegation"] = bool(cfg.get("delegation"))
    env_path = home / ".env"
    out["env_key"] = False
    if env_path.is_file():
        for line in env_path.read_text(
                encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("COMPANION_API_KEY="):
                out["env_key"] = bool(line.split("=", 1)[1].strip())
                break
    return out


def _recent_sessions(name: str, limit: int = 3) -> list[tuple]:
    """state.db で profile_name が一致する直近セッション"""
    path = config.HERMES_HOME / "state.db"
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "profile_name" not in cols:
            return []
        return conn.execute(
            "SELECT id, source, session_key FROM sessions"
            " WHERE profile_name=? ORDER BY started_at DESC LIMIT ?",
            (name, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _describe(name: str) -> None:
    policy = profile_policy.load()
    profs = policy.get("profiles") if isinstance(policy, dict) else {}
    registered = isinstance(profs, dict) and name in profs
    always, owner, dm = profile_policy.bridge_patterns(name)
    globs = profile_policy.skill_globs(name)

    print(f"== {name} ==")
    print(f"policy.yaml: {'登録あり' if registered else '未登録（default/env に fallback）'}")
    print(f"bridge 常時: {always or '（なし）'}")
    print(f"bridge owner限定: {owner or '（なし）'}")
    print(f"bridge DM限定: {dm or '（なし）'}")
    print(f"skills 許可: {'全て' if globs is None else (globs or '（なし）')}")

    rows = [
        r for r in db.skill_list(status="active")
        if profile_policy.skill_allowed(r["name"], name)
    ]
    if rows:
        print("見える active 技能:")
        for r in rows:
            print(f"  - {r['name']}")
    else:
        print("見える active 技能: なし")

    files = _profile_files(name)
    print(f"profile dir: {files['dir']}"
          + ("" if files["exists"] else "（存在しない）"))
    if files.get("targets_companion"):
        marks = []
        for key in ("providers", "auxiliary", "delegation", "env_key"):
            label = {"providers": "providers", "auxiliary": "auxiliary",
                     "delegation": "delegation",
                     "env_key": "COMPANION_API_KEY"}[key]
            marks.append(f"{label}={'✓' if files.get(key) else '✗'}")
        print("companion 接続: " + " ".join(marks))
        missing = [m for m in marks if "✗" in m]
        if missing:
            print("  ※欠損あり — scheduler の自動補完 or "
                  "`python scripts/bot_profile_setup.py "
                  f"{name}` で埋まる")

    sessions = _recent_sessions(name)
    if sessions:
        print("直近セッション:")
        for sid, source, sk in sessions:
            print(f"  - {sid} [{source}] {sk or ''}")


def _list_all() -> None:
    known = _known_profile_names()
    policy = profile_policy.load()
    policy_names = set((policy.get("profiles") or {}).keys()) \
        if isinstance(policy.get("profiles"), dict) else set()
    for name in sorted(known | policy_names):
        files = _profile_files(name)
        flags = []
        if files.get("targets_companion"):
            flags.append("companion向き")
        if name in policy_names:
            flags.append("policy登録")
        if not files["exists"]:
            flags.append("dir無し")
        print(f"{name}: {', '.join(flags) or '—'}")
    unknown = _policy_unknown_keys()
    for k in unknown:
        print(f"※ policy.yaml の未定義キー: {k}")
    ghosts = policy_names - known
    for g in sorted(ghosts):
        print(f"※ policy.yaml にあるが profile 未作成: {g}")


def main() -> None:
    args = sys.argv[1:]
    if args:
        _describe(args[0])
    else:
        _list_all()


if __name__ == "__main__":
    main()
