"""data/skills-export/ — active 技能を身体（Hermes）に見せる配布口と
upstream 照合（runtime-hermes-tooling.md §7.2-7.3、R4）。

soul.py と同じ定期同期・管理印方式。scheduler の定期 tick が
sync_export() を呼ぶ:

- active 技能のパッケージを data/skills-export/<name>/ に写す。
  Hermes config の skills.external_dirs にこの dir を登録しておくと
  body 側 agent が技能として読める。disabled/staged になった写しは消す
- external_dirs は書込保護ではない（Hermes の skill_manage 等で書き換え
  られうる）ので、同期時に写しと正本の内容を照合する。ずれていれば
  正本で上書きして戻し、ずれた版は skills-inbox へ「改訂提案」として
  回す（→ scan_inbox が staged→承認のレーンに乗せる）。
  上書きで消える正本は念のため skills-archive/superseded/ に退避する
- 管理対象は各パッケージ内の .companion-export 印がある dir だけ。
  印のない dir はこちらの生成物でないとみなして消さずに残す

check_upstream()（§7.3）: origin='imported' かつ active の技能は
export 経由で身体側の GET /v1/skills に見えるはずなので、定期的に
照合して見えなくなったものは upstream_gone_at を記録してマスターに
通知する（companion 側で育てた可能性があるので自動 disable はしない。
一覧に戻れば旗を下ろす）。
"""
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import db, skills
from .config import (
    SKILL_UPSTREAM_GRACE_MIN,
    SKILL_UPSTREAM_INTERVAL_MIN,
    SKILLS_ARCHIVE_DIR,
    SKILLS_DIR,
    SKILLS_EXPORT,
    SKILLS_EXPORT_DIR,
    SKILLS_INBOX_DIR,
)
from .runtime import body, hermes

log = logging.getLogger("companion")

# export した各パッケージ内に置く管理印（内容ハッシュの照合対象からは外す。
# この印がない dir はこちらの生成物ではないので消さない）
_PACKAGE_MARKER = ".companion-export"
# export ルートの管理印（中身が機械生成だと分かる目印）
_ROOT_MARKER = ".companion-managed"
_ROOT_MARKER_TEXT = (
    "このディレクトリは companion が active 技能を Hermes の\n"
    " skills.external_dirs 向けに写したもの。正本は data/skills/。\n"
)
# 改訂提案として inbox に回すときのディレクトリ名の印
_PROPOSAL_SUFFIX = ".export-proposal"

_last_upstream_check = 0.0


def _manifest(root: Path) -> dict:
    """パッケージの内容表 {相対パス: sha256}。管理印ファイルは比較から外す"""
    out = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        if rel == _PACKAGE_MARKER:
            continue
        try:
            out[rel] = skills._sha256(p.read_bytes())
        except OSError:
            out[rel] = "<unreadable>"
    return out


def _canon_dir(row: dict) -> Path:
    return SKILLS_DIR / (row.get("rel_path") or row["name"])


def _write_export(canon: Path, dest: Path) -> None:
    if dest.is_dir():
        shutil.rmtree(dest)
    elif dest.exists():
        dest.unlink()
    shutil.copytree(canon, dest)
    (dest / _PACKAGE_MARKER).write_text(
        f"generated from data/skills/{canon.name} — 正本は companion 側。\n",
        encoding="utf-8",
    )


def _propose_revision(drifted: Path, row: dict) -> None:
    """ずれた写しを skills-inbox に「改訂提案」として回す。

    採用可否は通常の staged→承認レーン（scan_inbox→通知→skill_approve）に
    任せる。提案が staged になると data/skills/<name>/ の正本は上書きで
    消えるので、現在の正本は skills-archive/superseded/ に退避しておく。
    """
    name = row["name"]
    dest = skills._unique_dest(
        SKILLS_INBOX_DIR, f"{name}{_PROPOSAL_SUFFIX}"
    )
    try:
        shutil.copytree(drifted, dest)
        marker = dest / _PACKAGE_MARKER
        if marker.exists():
            marker.unlink()
    except OSError:
        log.exception("skill export proposal copy failed: %s", drifted)
        return
    canon = _canon_dir(row)
    try:
        sup_dir = SKILLS_ARCHIVE_DIR / "superseded"
        sup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            canon,
            skills._unique_dest(
                sup_dir, f"{name}-{datetime.now():%Y%m%d-%H%M%S}"
            ),
        )
    except OSError:
        log.exception("superseded snapshot failed: %s", canon)
    log.info("skill %s export drift -> revision proposal at %s", name, dest)


def sync_export() -> dict:
    """data/skills-export/ を active 技能に合わせて同期する（§7.2）。"""
    res = {"synced": [], "restored": [], "proposed": [], "removed": []}
    if not SKILLS_EXPORT:
        return {**res, "skipped": True}
    SKILLS_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    marker = SKILLS_EXPORT_DIR / _ROOT_MARKER
    try:
        if not marker.is_file() or marker.read_text(
            encoding="utf-8", errors="replace"
        ) != _ROOT_MARKER_TEXT:
            marker.write_text(_ROOT_MARKER_TEXT, encoding="utf-8")
    except OSError:
        log.exception("skills-export marker write failed")

    active = {r["name"]: r for r in db.skill_list(status="active")}
    try:
        dirs = [p for p in SKILLS_EXPORT_DIR.iterdir() if p.is_dir()]
    except OSError:
        log.exception("skills-export not readable: %s", SKILLS_EXPORT_DIR)
        return res

    # 既存の写しを見る: active 以外は消し（管理印付きのみ）、内容ずれは復元＋改訂提案
    for d in sorted(dirs):
        row = active.get(d.name)
        canon = _canon_dir(row) if row is not None else None
        if row is None or not canon.is_dir():
            if (d / _PACKAGE_MARKER).exists():
                try:
                    shutil.rmtree(d)
                    res["removed"].append(d.name)
                except OSError:
                    log.exception("skills-export remove failed: %s", d)
            else:
                log.warning(
                    "skills-export に管理印のない dir がある（残す）: %s", d
                )
            continue
        if _manifest(canon) != _manifest(d):
            _propose_revision(d, row)
            res["proposed"].append(row["name"])
            try:
                _write_export(canon, d)
                res["restored"].append(row["name"])
            except OSError:
                log.exception("skills-export restore failed: %s", d)

    # まだ写しがない active 技能を出す
    for name, row in active.items():
        dest = SKILLS_EXPORT_DIR / name
        if dest.exists():
            continue
        canon = _canon_dir(row)
        if not canon.is_dir():
            continue
        try:
            _write_export(canon, dest)
            res["synced"].append(name)
        except OSError:
            log.exception("skills-export write failed: %s", name)
    return res


def check_upstream(force: bool = False) -> dict:
    """§7.3: active な imported 技能が Hermes の /v1/skills に見えるか照合。

    見えなくなっていたら upstream_gone_at を刻んで1回だけ通知し、
    戻っていれば旗を下ろす。Hermes に繋がらないときは誤検知防止のため
    何もしない。呼び出し間隔は SKILL_UPSTREAM_INTERVAL_MIN で絞る。
    """
    global _last_upstream_check
    res = {"flagged": [], "cleared": []}
    if not SKILLS_EXPORT:
        return {**res, "skipped": "export disabled"}
    if not force:
        interval = max(SKILL_UPSTREAM_INTERVAL_MIN, 0.1) * 60
        if time.monotonic() - _last_upstream_check < interval:
            return {**res, "skipped": "interval"}
    _last_upstream_check = time.monotonic()
    try:
        data = body.skills()
    except (hermes.BodyUnreachableError, hermes.BodyAPIError) as e:
        return {**res, "skipped": f"body unreachable: {e}"}
    names = {
        s.get("name") for s in data
        if isinstance(s, dict) and s.get("name")
    }
    now = datetime.now()
    cutoff = now - timedelta(minutes=SKILL_UPSTREAM_GRACE_MIN)
    cid = db.resolve_notify_channel()
    for r in db.skill_list():
        if r["origin"] != "imported" or r["status"] != "active":
            continue
        # export の写しが無ければ upstream 確認の対象外（§7.2 側の仕事）
        if not (SKILLS_EXPORT_DIR / r["name"]).is_dir():
            continue
        # 承認直後は Hermes の技能キャッシュが追いつかないことがあるので猶予
        if (r.get("decided_at") or "") > f"{cutoff:%Y-%m-%d %H:%M:%S}":
            continue
        if r["name"] in names:
            if r.get("upstream_gone_at"):
                db.skill_set_upstream_gone(r["name"], None)
                res["cleared"].append(r["name"])
            continue
        if r.get("upstream_gone_at"):
            continue  # 記録済み（通知は遷移の1回だけ）
        ts = f"{now:%Y-%m-%d %H:%M:%S}"
        db.skill_set_upstream_gone(r["name"], ts)
        res["flagged"].append(r["name"])
        log.warning("skill %s is gone from Hermes /v1/skills", r["name"])
        if cid:
            db.enqueue_notification(
                f"skill_upstream_gone:{r['name']}:{ts}",
                cid,
                f"🧭 スキル「{r['name']}」が身体側の技能一覧から見えなく"
                "なったよ（upstream_gone）。技能そのものは止めてない。"
                " Hermes 側の skills.external_dirs 設定か技能登録を見てみて",
            )
        else:
            log.warning(
                "skill %s upstream_gone but no notify channel", r["name"]
            )
    return res
