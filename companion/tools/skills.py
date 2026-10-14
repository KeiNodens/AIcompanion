"""skill_* ツール群 — companion/skills.py の会話向け口。

技能そのものの判定・保管は companion/skills.py。ここは一覧・検索・閲覧・
承認/停止の入出力整形だけを持つ（全ツールは dispatch 時点で owner ゲート済み。
skill_approve がマスター承認として成立するのはそのゲートがあるから）。

一覧・検索・閲覧は呼出し元 profile の skills 許可（profile_policy）で
絞る — 索引に出ない技能を read で読み回れないようにするため。
"""
from .. import profile_policy, skills
from .actor import CURRENT_PROFILE

_STATUS_LABEL = {
    "staged": "承認待ち", "active": "有効",
    "disabled": "停止中", "rejected": "検査不合格",
}


def _fmt_row(r: dict, profile=None) -> str:
    label = _STATUS_LABEL.get(r["status"], r["status"])
    desc = f" — {r['description']}" if r.get("description") else ""
    scopes = f"（用途: {r['scopes']}）" if r.get("scopes") else ""
    miss = skills.missing_tools(r, profile)
    flag = f" ⚠使えないtool: {','.join(miss)}" if miss else ""
    return f"- {r['name']} [{label}]{desc}{scopes}{flag}"


def skill_list(status: str | None = None) -> str:
    profile = CURRENT_PROFILE.get()
    rows = [
        r for r in skills.list_skills(status or None)
        if profile_policy.skill_allowed(r["name"], profile)
    ]
    if not rows:
        return "スキルはまだ登録されてない"
    return "\n".join(_fmt_row(r, profile) for r in rows)


def skill_search(query: str) -> str:
    profile = CURRENT_PROFILE.get()
    rows = [
        r for r in skills.search(query)
        if profile_policy.skill_allowed(r["name"], profile)
    ]
    if not rows:
        return "該当するスキルはない"
    return "\n".join(_fmt_row(r, profile) for r in rows)


def skill_read(name: str, file: str = "") -> str:
    profile = CURRENT_PROFILE.get()
    if not profile_policy.skill_allowed(name, profile):
        return f"スキル {name} はこの会話では使えない"
    row, body, warn = skills.read_skill(name)
    if row is None:
        return f"スキル {name} は見つからない（skill_list で確認して）"
    label = _STATUS_LABEL.get(row["status"], row["status"])
    rel = (file or "").strip()
    if rel:
        text, err = skills.read_skill_file(row, rel)
        if err:
            return f"{name} [{label}]: {err}"
        return (
            (warn + "\n" if warn else "")
            + f"# {row['name']} [{label}] {rel}\n" + (text or "")
        )
    if not body:
        return f"{name} [{label}]: {warn or '本文を読めなかった'}"
    head = f"# {row['name']} [{label}]\n"
    others = [
        f for f in skills.package_files(row) if f.lower() != "skill.md"
    ]
    tail = "\n\n同梱ファイル: " + ", ".join(others) if others else ""
    return (warn + "\n" if warn else "") + head + body + tail


def skill_draft(name: str, description: str, body: str,
                scopes: str = "", requires_tools: str = "") -> str:
    return skills.draft(name, description, body, scopes, requires_tools)


def skill_approve(name: str) -> str:
    return skills.approve(name)


def skill_disable(name: str) -> str:
    return skills.disable(name)
