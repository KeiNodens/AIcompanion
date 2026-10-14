"""Core skill registry — 彼女の技能（runtime-hermes.md 項目E 前提）。

格納形式:
- 技能パッケージは data/skills/<name>/ 以下のファイル群（SKILL.md が入口）。
  agentskills.io 形式（frontmatter + markdown 本文）をそのまま置く
- skills テーブルは状態と出自の正本:
    staged   — inbox から取り込まれた承認待ち。プロンプト索引にも載らない
    active   — マスター承認済み。prompt_index() に載り skill_read で読める
    disabled — 承認の取り消し/停止。再 approve で active に戻せる
    rejected — 取込時の決定的検査に不合格（ファイルは skills-archive/rejected/ に退避）
- origin: 'imported'（body 側生成物の staged import）。将来の Core 側生成
  （turns→手順抽出, Q-P7-218）は 'self' で同じレーンに乗せる想定

取込み経路（staged import 隔離レーン, selection §2.5b）:
- data/skills-inbox/ を走査（Hermes config の skills.create_dir 指定先）
- <name>/SKILL.md のパッケージ or 裸の *.md を受け付ける
- 決定的検査（名前・サイズ・frontmatter）→ 合格は data/skills/<name>/ に
  配置して staged 登録、不合格は rejected 記録。inbox の元は
  data/skills-archive/ の done/ rejected/ に退避して冪等にする
  （inbox 内の退避だと Hermes のスキル探索に見えてしまうため探索外へ出す）
- 同名で内容が変わって再投入されたものは staged に戻す（再承認が要る）
- 本人承認は skill_approve ツール経由（全ツールが owner ゲート済み）。
  さらに approve/disable 関数内でも is_owner() を必須にして、
  プロセス内直接呼出し（委譲 exec 等）からの迂回を塞ぐ
  （decided_by には実際の actor を刻む）

frontmatter の追加項目（§7.4）:
- requires_tools — その技能が前提とする tool 名（カンマ/空白区切り or
  "[a, b]"）。承認時と索引生成時に「彼女の TOOLS＋形式橋で届く body tool」
  と照合し、満たせないものは索引・検索に「⚠ 使えないtoolあり」を出す

配布・照合（§7.2-7.3）は skill_export.py 側の仕事。
"""
import difflib
import fnmatch
import hashlib
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import db, profile_policy
from .config import (
    SKILL_DRAFT_MAX,
    SKILL_INDEX_MAX,
    SKILL_MAX_BYTES,
    SKILL_PACKAGE_MAX_BYTES,
    SKILLS_ARCHIVE_DIR,
    SKILLS_DIR,
    SKILLS_INBOX_DIR,
)

log = logging.getLogger("companion")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DONE_DIR = "_done"
_REJECTED_DIR = "_rejected"
# inbox 内の旧退避ディレクトリ名 → archive 側の bucket 名
_ARCHIVE_BUCKETS = {_DONE_DIR: "done", _REJECTED_DIR: "rejected"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_frontmatter(text: str) -> dict:
    """SKILL.md 先頭の YAML frontmatter からトップレベルの key: value だけ拾う
    （pyyaml 非依存の最小パース。入れ子・複数行値は読まない）"""
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}
    out = {}
    for line in lines[1:end]:
        # インデントされた行は親キーの入れ子なので読み飛ばす
        if not line or line[0] in (" ", "\t", "#"):
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        out[m.group(1).lower()] = m.group(2).strip().strip('"').strip("'")
    return out


def _parse_scopes(raw: str) -> str:
    """frontmatter の scopes/tags 値を正規化してカンマ区切りにする。
    "[a, b]" / "a, b" / "a b" どれでも受ける"""
    raw = raw.strip().strip("[]")
    parts = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
    return ",".join(dict.fromkeys(parts))


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


def _validate_entrypoint(md_path: Path, fallback_name: str):
    """SKILL.md 本文を検査して (name, description, scopes, requires_tools,
    origin, error) を返す。origin は frontmatter の `origin: self` だけを
    認め、それ以外（未指定含む）は 'imported' とみなす"""
    try:
        raw = md_path.read_bytes()
    except OSError as e:
        return None, "", "", "", "", f"read error: {e}"
    if len(raw) > SKILL_MAX_BYTES:
        return None, "", "", "", "", (
            f"SKILL.md too large ({len(raw)} > {SKILL_MAX_BYTES})"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "", "", "", "", "SKILL.md is not utf-8"
    if not text.strip():
        return None, "", "", "", "", "SKILL.md is empty"
    fm = _parse_frontmatter(text)
    name = (fm.get("name") or fallback_name).strip()
    if not _NAME_RE.match(name):
        return None, "", "", "", "", f"invalid skill name: {name!r}"
    description = fm.get("description", "")
    scopes = _parse_scopes(fm.get("scopes") or fm.get("tags") or "")
    requires = _parse_scopes(fm.get("requires_tools") or "")
    origin = "self" if fm.get("origin") == "self" else "imported"
    return name, description, scopes, requires, origin, None


def _dest_dir(name: str) -> Path:
    return SKILLS_DIR / name


def _unique_dest(dest_dir: Path, name: str) -> Path:
    """退避先のパスを名前衝突しないよう決める"""
    dest = dest_dir / name
    if not dest.exists():
        return dest
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    for i in range(100):
        cand = dest_dir / f"{name}-{stamp}-{i}"
        if not cand.exists():
            return cand
    return dest_dir / f"{name}-{stamp}-{datetime.now().microsecond}"


def _retire_inbox_entry(src: Path, bucket: str) -> Path:
    """処理済みの inbox 項目を skills-archive の done/ rejected/ に時刻付きで退避する"""
    dest_dir = SKILLS_ARCHIVE_DIR / _ARCHIVE_BUCKETS[bucket]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(dest_dir, f"{src.name}-{datetime.now():%Y%m%d-%H%M%S}")
    try:
        shutil.move(str(src), str(dest))
    except OSError:
        log.exception("inbox retire failed: %s", src)
    return dest


def _migrate_legacy_buckets() -> None:
    """旧来の inbox 内 _done/_rejected を skills-archive へ移す。

    inbox は Hermes のスキル探索対象なので、過去に退避された分を探索外へ
    出す。全部移せた旧ディレクトリは消す（残った場合は次回に持ち越し）。
    """
    for legacy, bucket in _ARCHIVE_BUCKETS.items():
        src_dir = SKILLS_INBOX_DIR / legacy
        if not src_dir.is_dir():
            continue
        dest_dir = SKILLS_ARCHIVE_DIR / bucket
        for item in sorted(src_dir.iterdir()):
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(_unique_dest(dest_dir, item.name)))
            except OSError:
                log.exception("legacy skills bucket move failed: %s", item)
        try:
            if not any(src_dir.iterdir()):
                src_dir.rmdir()
        except OSError:
            log.exception("legacy skills bucket cleanup failed: %s", src_dir)


def _diff_counts(old: str, new: str) -> tuple[int, int]:
    """SKILL.md 前版との差分の大きさ（追加/削除行数）。承認前の判断材料に使う"""
    add = rem = 0
    for line in difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm=""
    ):
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            add += 1
        elif line.startswith("-"):
            rem += 1
    return add, rem


def _stage_package(name: str, src: Path, description: str, scopes: str,
                   requires_tools: str, content_hash: str,
                   origin: str = "imported") -> str:
    """検査済みパッケージを data/skills/<name>/ に配置して staged 登録する"""
    dest = _dest_dir(name)
    try:
        if src.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / "SKILL.md")
    except OSError:
        log.exception("skill package copy failed: %s -> %s", src, dest)
        raise
    return db.skill_stage(
        name, description, scopes, origin, content_hash,
        str(src), name, requires_tools=requires_tools, note=None,
    )


def scan_inbox() -> dict:
    """skills-inbox を1周走査する。scheduler の定期 tick から呼ばれる。

    戻り値: {"staged": [name], "restaged": [name], "rejected": [(name, reason)],
            "meta": {name: {"diff": (add, rem) | None}}}
    meta.diff は restaged 時の SKILL.md 前版との差分行数（通知の判断材料用）。
    例外は投げず、項目ごとの失敗は rejected 相当として記録する。
    """
    result = {"staged": [], "restaged": [], "rejected": [], "meta": {}}
    if not SKILLS_INBOX_DIR.is_dir():
        return result
    _migrate_legacy_buckets()
    try:
        entries = sorted(SKILLS_INBOX_DIR.iterdir())
    except OSError:
        log.exception("skills inbox not readable")
        return result

    for entry in entries:
        if entry.name.startswith(("_", ".")):
            continue
        try:
            if entry.is_dir():
                cand = entry / "SKILL.md"
                if not cand.is_file():
                    # SKILL.md を持たないディレクトリは取り込み対象外（退避もしない）
                    continue
                md_path = cand
            elif entry.suffix.lower() == ".md":
                md_path = entry
            else:
                continue

            fallback = entry.name if entry.is_dir() else entry.stem
            name, desc, scopes, requires, origin, err = _validate_entrypoint(
                md_path, fallback
            )
            raw = md_path.read_bytes() if md_path.exists() else b""
            chash = _sha256(raw)
            if entry.is_dir() and err is None and _dir_size(entry) > SKILL_PACKAGE_MAX_BYTES:
                err = f"package too large (> {SKILL_PACKAGE_MAX_BYTES})"
            if err:
                label = name or entry.stem
                db.skill_mark_rejected(
                    label, err, content_hash=chash, source_path=str(entry)
                )
                _retire_inbox_entry(entry, _REJECTED_DIR)
                result["rejected"].append((label, err))
                log.warning("skill %s rejected at import: %s", label, err)
                continue

            existing = db.skill_get(name)
            if existing and existing["content_hash"] == chash:
                # 同一内容の再投入。重複登録せず inbox だけ片付ける
                _retire_inbox_entry(entry, _DONE_DIR)
                continue

            # 上書きで消える前の版との差分行数（承認前の判断材料に使う）
            if existing:
                old_md = _package_md(existing)
                old_text = ""
                if old_md is not None:
                    try:
                        old_text = old_md.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        pass
                result["meta"][name] = {
                    "diff": _diff_counts(old_text, raw.decode("utf-8"))
                }
            if origin != "imported":
                result["meta"].setdefault(name, {})["origin"] = origin

            action = _stage_package(
                name, entry, desc, scopes, requires, chash, origin=origin
            )
            _retire_inbox_entry(entry, _DONE_DIR)
            if action in ("new", "restaged"):
                result["staged" if action == "new" else "restaged"].append(name)
            log.info("skill %s imported (%s)", name, action)
        except Exception:
            # 個別の失敗で走査全体を止めない。inbox に残るので次 tick で再試行
            log.exception("skill import failed: %s", entry)
    return result


def list_skills(status: str | None = None) -> list[dict]:
    return db.skill_list(status or None)


def missing_tools(row: dict, profile: str | None = None) -> list[str]:
    """requires_tools のうち今の彼女が満たせない tool 名の一覧（§7.4）。

    満たせる = companion の TOOLS に同名がある、または profile 別 bridge
    許可 glob（無指定時は BODY_BRIDGE_TOOLS＋DM_TOOLS。DM 限定の書込系も
    DM 運用では満たせる）に一致する。"body__" 前置の書き方も受ける。
    """
    req = [
        t.strip() for t in (row.get("requires_tools") or "").split(",")
        if t.strip()
    ]
    if not req:
        return []
    from .tools import TOOLS  # tools→skills の循環を避けるため呼ぶ時に解決
    own = {t["function"]["name"] for t in TOOLS}
    always, owner, dm = profile_policy.bridge_patterns(profile)
    globs = always + owner + dm
    missing = []
    for t in req:
        if t in own:
            continue
        base = t[6:] if t.startswith("body__") else t
        if any(fnmatch.fnmatchcase(base, g) for g in globs):
            continue
        missing.append(t)
    return missing


def prompt_index(profile: str | None = None) -> str:
    """{skills} ブロック用。active な技能の索引。

    件数が SKILL_INDEX_MAX を超えたら「名前＋scope」だけに縮め、説明は
    skill_search で引かせる（§7.5。索引の並びは名前順固定=prompt cache
    保護）。requires_tools を満たせない技能には「⚠ 使えないtoolあり」を付ける
    （§7.4）。profile 指定時は profile_policy の skills 許可で絞る。"""
    rows = [
        r for r in db.skill_list(status="active")
        if profile_policy.skill_allowed(r["name"], profile)
    ]
    compact = len(rows) > SKILL_INDEX_MAX
    lines = []
    if compact:
        lines.append(
            f"（{len(rows)}件あるので名前＋scopeのみ。詳しくは skill_search、"
            "本文は skill_read で）"
        )
    for r in rows:
        if compact:
            line = f"- {r['name']}"
            if r.get("scopes"):
                line += f"（用途: {r['scopes']}）"
        else:
            line = f"- {r['name']}: {r.get('description') or '(説明なし)'}"
            if r.get("scopes"):
                line += f"（用途: {r['scopes']}）"
        miss = missing_tools(r, profile)
        if miss:
            line += f" ⚠ 使えないtoolあり: {','.join(miss)}"
        lines.append(line)
    return "\n".join(lines)


def search(query: str, limit: int = 10) -> list[dict]:
    """skill_search ツール用。名前・説明・用途・requires_tools を
    トークン部分一致で検索し、名前一致を優先して返す。rejected は除く"""
    q = (query or "").strip().lower()
    rows = [r for r in db.skill_list() if r["status"] != "rejected"]
    if not q:
        return rows[:limit]
    scored = []
    for r in rows:
        hay = " ".join(
            str(x) for x in (
                r["name"], r.get("description"), r.get("scopes"),
                r.get("requires_tools"),
            ) if x
        ).lower()
        score = sum(1 for tok in q.split() if tok in hay)
        if r["name"].lower() == q:
            score += 20
        elif q in r["name"].lower():
            score += 8
        if score > 0:
            scored.append((score, r["name"], r))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [r for _, _, r in scored[:limit]]


def _package_md(row: dict) -> Path | None:
    rel = row.get("rel_path") or row["name"]
    p = SKILLS_DIR / rel / "SKILL.md"
    return p if p.is_file() else None


def _package_dir(row: dict) -> Path:
    return SKILLS_DIR / (row.get("rel_path") or row["name"])


def package_files(row: dict) -> list[str]:
    """パッケージ内のファイル一覧（skill_read の file 指定先を示す目次）"""
    base = _package_dir(row)
    if not base.is_dir():
        return []
    return sorted(
        str(p.relative_to(base)).replace("\\", "/")
        for p in base.rglob("*") if p.is_file()
    )


def read_skill_file(row: dict, rel: str):
    """パッケージ内の support file を読む（§7.4）。(content, error) を返す。

    rel はパッケージ根幹からの相対パスに限定し、".." や絶対パスで外に
    出る参照は拒否する。エラー時は (None, メッセージ)。
    """
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        return None, "file が空"
    base = _package_dir(row).resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        return None, "パッケージ外のファイルは読めない"
    if not target.is_file():
        return None, f"パッケージ内に {rel} はない"
    try:
        raw = target.read_bytes()
    except OSError as e:
        return None, f"読み込み失敗: {e}"
    if len(raw) > SKILL_MAX_BYTES:
        return None, f"{rel} が大きすぎる（{len(raw)} > {SKILL_MAX_BYTES}）"
    return raw.decode("utf-8", errors="replace"), ""


# 承認前の判断材料として SKILL.md 本文に立てるリスク旗（§7.5）
_RISK_RULES = [
    ("terminal系", re.compile(
        r"terminal|execute_command|powershell|cmd\.exe|subprocess|"
        r"コマンド実行|シェル実行", re.I)),
    ("ネットワーク", re.compile(
        r"https?://|curl |invoke-webrequest|requests\.|urllib|"
        r"ネットワーク", re.I)),
    ("削除操作", re.compile(
        r"\brm\b|remove-item|\bdel\b|shutil\.rmtree|\bdelete\b|削除", re.I)),
]


def risk_flags(row: dict) -> list[str]:
    """SKILL.md 本文への決定的リスク旗（terminal/ネットワーク/削除操作への
    言及を検出するだけで判断はしない。§7.5 の事前要約に添える）"""
    md = _package_md(row)
    if md is None:
        return []
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [label for label, pat in _RISK_RULES if pat.search(text)]


def read_skill(name: str):
    """skill_read ツール用。(row, body, warn) を返す。row が無ければ (None, "", "")"""
    row = db.skill_get((name or "").strip())
    if not row:
        return None, "", ""
    md = _package_md(row)
    warn = ""
    if md is None:
        return row, "", "技能ファイルが見つかりません"
    try:
        raw = md.read_bytes()
        body = raw.decode("utf-8", errors="replace")
    except OSError as e:
        return row, "", f"読み込みに失敗: {e}"
    if _sha256(raw) != row["content_hash"]:
        warn = "（注意: 登録・承認時点と内容が変わっています）"
    return row, body, warn


def _owner_gate() -> str | None:
    """管理操作の関数層ゲート。dispatch_tool_result の is_owner() は
    会話ツール経路だけを守るが、approve/disable は registry 正本を直に
    書き換えるので、プロセス内からの直接呼出し（委譲エージェントの
    in-process 実行等）にも同じ判定を掛ける。"""
    from .tools.actor import is_owner  # tools→skills の循環を避けるため呼ぶ時に解決
    return None if is_owner() else "そのお願いはマスターにしか聞けないの"


def _decider(decided_by: str | None) -> str:
    """決裁者の記録値。明示指定が無ければ実際の actor（話者）を刻む"""
    if decided_by:
        return decided_by
    from .tools.actor import CURRENT_ACTOR
    return CURRENT_ACTOR.get() or "unknown"


def approve(names: str, decided_by: str | None = None) -> str:
    """1件またはまとめて承認（"all-staged" / カンマ・空白区切りの複数名）"""
    denied = _owner_gate()
    if denied:
        return denied
    names = (names or "").strip()
    if not names:
        return "承認するスキル名を教えて（all-staged で全部まとめても可）"
    if names.lower() in ("all-staged", "all_staged", "allstaged"):
        targets = [r["name"] for r in db.skills_staged_pending()]
        if not targets:
            return "承認待ちのスキルはないよ"
    else:
        targets = [t for t in re.split(r"[,\s]+", names) if t]
    decider = _decider(decided_by)
    return "\n".join(_approve_one(t, decider) for t in targets)


def _approve_one(name: str, decided_by: str) -> str:
    row = db.skill_get((name or "").strip())
    if not row:
        return f"スキル {name} は見つからない"
    if row["status"] == "active":
        return f"{name} はすでに有効だよ"
    # 承認時点で staged 時と内容が変わっていたら承認しない（staged 検査の意味を保つ）
    md = _package_md(row)
    if md is not None and _sha256(md.read_bytes()) != row["content_hash"]:
        return f"{name} は登録時と内容が変わっているから承認を保留したよ（再投入して）"
    db.skill_set_status(name, "active", decided_by=decided_by)
    msg = f"スキル {name} を有効にしたよ"
    miss = missing_tools(db.skill_get(name) or row)
    if miss:
        msg += f"（ただし要求 tool の {', '.join(miss)} は今の私には届かないよ）"
    return msg


def disable(name: str, decided_by: str | None = None) -> str:
    denied = _owner_gate()
    if denied:
        return denied
    row = db.skill_get((name or "").strip())
    if not row:
        return f"スキル {name} は見つからない"
    if row["status"] == "disabled":
        return f"{name} はもう止まってる"
    db.skill_set_status(name, "disabled", decided_by=_decider(decided_by))
    return f"スキル {name} を止めたよ"


def draft(name: str, description: str, body: str,
          scopes: str = "", requires_tools: str = "") -> str:
    """skill_draft ツール用。会話の中で彼女自身が SKILL.md を起票して
    skills-inbox に投函する口（§7.6 skill_gen の会話駆動版）。

    frontmatter はここで決定的に組み立てる。LLM が書くのは本文だけで、
    origin: self が Core 起票の印になる。投函後は scan_inbox の通常
    レーン（決定的検査→staged→承認）に乗るので自動有効化はしない。
    同名技能があるときは改訂として扱い、入れ替わりには再承認が要る。
    暴走防止で1日 SKILL_DRAFT_MAX 件まで。
    """
    name = (name or "").strip()
    description = (description or "").strip()
    body = (body or "").strip()
    if not _NAME_RE.match(name):
        return "スキル名は英字か数字で始まる英数字・_・- の64文字以内にして"
    if not description or "\n" in description:
        return "description は1行の説明文にして"
    if not body:
        return "本文（body）が空だよ"

    state = db.load_state()
    today = f"{datetime.now():%Y-%m-%d}"
    if state.get("skill_draft_day") != today:
        db.save_state(skill_draft_day=today, skill_draft_count=0)
        state["skill_draft_count"] = 0
    count = int(state.get("skill_draft_count") or 0)
    if count >= SKILL_DRAFT_MAX:
        return f"今日の起票は {SKILL_DRAFT_MAX} 件までだよ（明日また投函できる）"

    lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    scopes_n = _parse_scopes(scopes or "")
    requires_n = _parse_scopes(requires_tools or "")
    if scopes_n:
        lines.append(f"scopes: {scopes_n}")
    if requires_n:
        lines.append(f"requires_tools: {requires_n}")
    lines += ["origin: self", "---", "", body, ""]
    md = "\n".join(lines)
    if len(md.encode("utf-8")) > SKILL_MAX_BYTES:
        return f"本文が大きすぎる（SKILL.md は {SKILL_MAX_BYTES} bytes まで）"

    existing = db.skill_get(name)
    dest = SKILLS_INBOX_DIR / name
    try:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "SKILL.md").write_text(md, encoding="utf-8")
    except OSError as e:
        return f"inbox への投函に失敗した: {e}"
    db.save_state(skill_draft_count=count + 1)
    log.info("skill_draft: %s を inbox に投函", name)
    if existing:
        return (
            f"{name} の改訂版を投函したよ。検査を通ったら承認待ちに"
            "なるので、入れ替わるにはマスターの再承認が要るよ"
        )
    return (
        f"{name} を起票して inbox に投函したよ。検査を通ったら承認待ちに"
        "なるから、使えるようになるにはマスターの承認が要るよ"
    )
