"""LLM と Hermes API の生存監視 — 自分の故障を自分で通報する。

WATCHDOG_INTERVAL 秒ごとに GET だけの probe（LLM /models、Hermes /health）
を行い、連続 WATCHDOG_FAILS 回の失敗で「落ちた」と判定して notify_outbox に
1回だけ通知を載せる（配送は discord_out で LLM/Hermes 非依存）。復旧も
1回だけ知らせる。dedupe_key に障害開始時刻を入れるので通知は1障害1通。
"""
import asyncio
import logging
import time
from datetime import datetime

from . import config, db
from . import llm as llm_mod
from .runtime import hermes

log = logging.getLogger("companion")


def _probe_llm() -> bool:
    try:
        return bool(llm_mod.available_models())
    except Exception:
        return False


def _probe_hermes() -> bool:
    if not config.HERMES_API_KEY:
        return True  # body 未接続の構成では監視しない
    try:
        return bool(hermes.health())
    except Exception:
        return False


_TARGETS = (
    ("llm", "LLM", _probe_llm),
    ("hermes", "Hermes API", _probe_hermes),
)

# name -> {"down": bool, "fails": int, "since": str|None}
_state: dict[str, dict] = {}
_last_run = 0.0


async def tick() -> None:
    """scheduler の毎 tick から呼ぶ。間隔・連続失敗数は config で調整"""
    global _last_run
    if not config.WATCHDOG:
        return
    now = time.monotonic()
    if now - _last_run < config.WATCHDOG_INTERVAL:
        return
    _last_run = now
    for name, label, probe in _TARGETS:
        ok = await asyncio.to_thread(probe)
        _update(name, label, ok)


def _update(name: str, label: str, ok: bool) -> None:
    st = _state.setdefault(name, {"down": False, "fails": 0, "since": None})
    if ok:
        if st["down"]:
            since = st["since"] or "さっき"
            _notify(
                name, "up",
                f"🟢 {label} が復旧したよ（{since} ころから応答がなかった）",
            )
        st.update(down=False, fails=0, since=None)
        return
    st["fails"] += 1
    if st["down"] or st["fails"] < config.WATCHDOG_FAILS:
        return
    st["down"] = True
    st["since"] = datetime.now().strftime("%m-%d %H:%M")
    log.warning("watchdog: %s down (%d consecutive failures)", name, st["fails"])
    _notify(
        name, "down",
        f"⚠️ 身体が不調みたい: {label} が応答しないよ"
        f"（{st['fails']}回連続で失敗）",
    )


def _notify(name: str, kind: str, text: str) -> None:
    cid = db.resolve_notify_channel()
    if not cid:
        log.warning("watchdog %s %s but no notify channel: %s", name, kind, text)
        return
    stamp = _state[name].get("since") or datetime.now().strftime("%m-%d %H:%M")
    db.enqueue_notification(f"watchdog:{name}:{kind}:{stamp}", cid, text)
