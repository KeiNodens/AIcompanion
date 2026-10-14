import logging
from datetime import datetime

from ..config import PERSONA_NOTES_LIMIT, PERSONA_NOTES_PATH
from ..db import load_state, update_state

log = logging.getLogger("companion")

_DEFAULT = {"mood": "普通", "valence": 0, "energy": 60, "updated_at": None}


def _resolve_inner(state: dict) -> dict:
    inner = dict(_DEFAULT)
    inner.update(state.get("inner") or {})
    # 経過時間で energy を60へ、valenceを0へ緩やかに戻す（1日放置でほぼニュートラル）
    try:
        if inner.get("updated_at"):
            dt = datetime.fromisoformat(inner["updated_at"])
            hours = (datetime.now() - dt).total_seconds() / 3600
            if hours > 0:
                k = min(hours / 24.0, 1.0)
                inner["energy"] = round(inner.get("energy", 60) + (60 - inner.get("energy", 60)) * k)
                inner["valence"] = round(inner.get("valence", 0) * (1 - k), 1)
    except Exception:
        pass
    return inner


def get_inner() -> dict:
    return _resolve_inner(load_state())


def set_mood(mood: str, valence: int | None = None, energy: int | None = None) -> str:
    # ロック内で最新の state に対して更新（他キーの同時更新を消さない）
    def apply(state: dict) -> None:
        inner = _resolve_inner(state)
        inner["mood"] = mood
        if valence is not None:
            inner["valence"] = max(-2, min(2, int(valence)))
        if energy is not None:
            inner["energy"] = max(0, min(100, int(energy)))
        inner["updated_at"] = datetime.now().isoformat(timespec="seconds")
        state["inner"] = inner

    update_state(apply)
    return "気分を記録しました"


def update_persona_notes(note: str) -> str:
    note = note.strip()
    if not note:
        return "note が空です"
    PERSONA_NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        PERSONA_NOTES_PATH.read_text(encoding="utf-8")
        if PERSONA_NOTES_PATH.exists()
        else ""
    )
    line = f"- {datetime.now().strftime('%Y-%m-%d')} {note}"
    body = (existing.rstrip("\n") + "\n" + line + "\n") if existing else line + "\n"
    truncated = False
    if len(body) > PERSONA_NOTES_LIMIT:
        # 古い行から削る（先頭行を落とす）
        lines = body.splitlines()
        while lines and len("\n".join(lines)) > PERSONA_NOTES_LIMIT:
            lines.pop(0)
        body = "\n".join(lines) + "\n"
        truncated = True
    PERSONA_NOTES_PATH.write_text(body, encoding="utf-8")
    log.info("persona_notes updated: %s", note[:60])
    return "自分メモに記録しました" + ("（古いメモを一部整理）" if truncated else "")


def persona_notes() -> str:
    if PERSONA_NOTES_PATH.exists():
        return PERSONA_NOTES_PATH.read_text(encoding="utf-8").strip()
    return ""


def inner_prompt() -> str:
    inner = get_inner()
    lines = [f"気分: {inner['mood']}", f"元気度: {inner['energy']}/100"]
    v = inner.get("valence", 0)
    if v:
        lines.append(f"気分の明るさ: {'+' if v > 0 else ''}{v}")
    return "\n".join(lines)
