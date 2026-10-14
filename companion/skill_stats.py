"""active 技能の成果追跡 — 週次レポート。

skill_gen が成功・失敗 trace から技能候補を起こすのに対し、こちらは
activate 済みの技能が実際に役に立っているかを見る側のレーン
（SkillOpt/RRSI 系の「成果で検証してから残す」発想の読み取り版）。

材料は turn_tool_calls(origin='core') の skill_read 呼出し。技能ごとに
- reads …「読めた」回数（見つからない・使えない応答は read_errors 側）
- turns …読まれた往復の件数
- fail_turns …その往復内に非 ok の tool 呼出しがあった件数
を集計し、読まれた往復の半分以上で tool 失敗がある技能を「要注意」
として報告する。

turns は TURNS_RETENTION_DAYS で掃除されるので、集計の窓は保持期間に
連動して自然にローリングする。判定は通知の文章として出すだけで、
技能の自動 disable はしない（upstream_gone と同じ方針。改訂・停止の
判断はマスター）。
"""
import json
import logging
import sqlite3

from . import db

log = logging.getLogger("companion")

# 「読まれた往復のうち tool 失敗あり」がこの割合を超え、かつ
# MIN_TURNS 件以上あるとき要注意として旗を立てる
# （少ない件数の偶然で技能を糾弾しないための下限）
FLAG_MIN_TURNS = 3
FLAG_FAIL_RATE = 0.5

# skill_read の「読めなかった」応答の印（tools/skills.py の定型文。
# 「読もうとして読めなかった」呼出しは使用回数に数えず read_errors に）
_READ_REFUSAL_MARKS = ("は見つからない", "はこの会話では使えない")


def _skill_read_calls() -> list:
    """往復に紐づく skill_read 呼出し（turn 正本がある分だけ）"""
    with db.get_db() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, turn_id, args_json, status, result_excerpt"
            " FROM turn_tool_calls"
            " WHERE origin='core' AND tool='skill_read'"
            " AND turn_id IS NOT NULL ORDER BY id",
        ).fetchall()


def _turn_outcomes(turn_ids) -> dict:
    """往復 id → その往復に非 ok の tool 呼出しがあったか"""
    ids = sorted({int(t) for t in turn_ids if t is not None})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT turn_id,"
            " SUM(CASE WHEN status = 'ok' THEN 0 ELSE 1 END) AS fails"
            f" FROM turn_tool_calls WHERE turn_id IN ({marks})"
            " GROUP BY turn_id",
            ids,
        ).fetchall()
    return {r[0]: bool(r[1]) for r in rows}


def _read_name(args_json: str) -> str:
    try:
        args = json.loads(args_json or "{}")
    except Exception:
        return ""
    if not isinstance(args, dict):
        return ""
    return str(args.get("name") or "").strip()


def _read_ok(row) -> bool:
    """「読めた」呼出しか。tool 自体が失敗・拒否応答は False（read_errors 側）"""
    if row["status"] != "ok":
        return False
    excerpt = row["result_excerpt"] or ""
    return not any(m in excerpt for m in _READ_REFUSAL_MARKS)


def collect() -> dict:
    """技能ごとの使用状況を集計する。

    戻り値:
      per_skill   {name: {reads, turns, fail_turns, read_errors, status}}
      flagged     要注意の技能名（active・MIN_TURNS 超・失敗率 FAIL_RATE 超）
      unused_active  active だが一度も読まれていない技能名
      total_reads 全技能の reads 合計
    """
    per = {}
    for c in _skill_read_calls():
        name = _read_name(c["args_json"])
        if not name:
            continue
        e = per.setdefault(
            name, {"reads": 0, "turns": set(), "read_errors": 0})
        if _read_ok(c):
            e["reads"] += 1
            e["turns"].add(c["turn_id"])
        else:
            e["read_errors"] += 1
    outcomes = _turn_outcomes(
        tid for e in per.values() for tid in e["turns"])
    registered = {r["name"]: r for r in db.skill_list()}
    per_skill = {}
    flagged = []
    for name, e in sorted(per.items()):
        turns = sorted(e["turns"])
        fail_turns = sum(1 for t in turns if outcomes.get(t))
        row = registered.get(name)
        per_skill[name] = {
            "reads": e["reads"],
            "turns": len(turns),
            "fail_turns": fail_turns,
            "read_errors": e["read_errors"],
            "status": row["status"] if row else None,
        }
        if (row and row["status"] == "active"
                and len(turns) >= FLAG_MIN_TURNS
                and fail_turns / len(turns) >= FLAG_FAIL_RATE):
            flagged.append(name)
    unused_active = sorted(
        n for n, r in registered.items()
        if r["status"] == "active" and not per.get(n, {}).get("reads")
    )
    return {
        "per_skill": per_skill,
        "flagged": flagged,
        "unused_active": unused_active,
        "total_reads": sum(e["reads"] for e in per_skill.values()),
    }


def format_report(rep: dict) -> str | None:
    """週次通知の本文。報るものが無ければ None（job は skipped 扱い）"""
    per = rep["per_skill"]
    if not rep["total_reads"] and not rep["flagged"] \
            and not rep["unused_active"]:
        return None
    lines = ["📊 技能の使われ方レポート"]
    used = [(n, e) for n, e in per.items() if e["reads"]]
    for name, e in sorted(used, key=lambda kv: (-kv[1]["reads"], kv[0])):
        line = f"・{name}: 読まれ{e['reads']}回 / 往復{e['turns']}件"
        if e["fail_turns"]:
            line += f"（うちtool失敗{e['fail_turns']}件）"
        if e["read_errors"]:
            line += f"、読めなかった呼出し{e['read_errors']}回"
        lines.append(line)
    if rep["flagged"]:
        lines.append("")
        for name in rep["flagged"]:
            e = per[name]
            lines.append(
                f"⚠ {name}: 読まれた往復の {e['fail_turns']}/{e['turns']}件"
                "で tool 失敗あり。skill_read で中身を見直して、"
                "改訂するか skill_disable を検討して")
    if rep["unused_active"]:
        lines.append("")
        lines.append(
            "直近の記録で一度も読まれてない: "
            + "、".join(rep["unused_active"]))
    return "\n".join(lines)
