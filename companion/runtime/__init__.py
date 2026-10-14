"""常駐処理（scheduler・API）の起動・終了を一元管理する。

起動元（Discord接続イベント・headlessエントリ等）はここを呼ぶだけにし、
タスクの寿命と二重起動防止はこのモジュールで持つ。終了・再起動時は
shutdown() でキャンセルする。1プロセス・1つの起動管理元。
service分割や別接続先は扱わない。

パッケージ内の他モジュール（Step 4・Runtime Interface）:
- body.py   — companion が body に求める能力の抽象面（呼び出し側はここ）
- hermes.py — body 側 REST 制御面のクライアント（Hermes 固有名の閉じ込め先）
- codex.py  — Codex app-server (JSON-RPC) のクライアント（codex-body.md）
- errors.py — 身体 backend 共通の例外型（BodyUnreachableError/BodyAPIError）
"""
import asyncio
import logging

log = logging.getLogger("companion")

_started = False
_tasks: list[asyncio.Task] = []
_loop: asyncio.AbstractEventLoop | None = None


def main_loop() -> asyncio.AbstractEventLoop | None:
    """ensure_started が走ったイベントループ。スレッド越しに遅延処理を
    投げたいツール（restart_self 等）が参照する"""
    return _loop


async def _check_hermes_config() -> None:
    """Hermes config の lint を起動時に1回だけ走らせる。
    error/warn をログに出すだけで起動はブロックしない"""
    try:
        from .. import hermes_check
        findings = await asyncio.to_thread(hermes_check.check)
    except Exception:
        log.exception("hermes config check failed")
        return
    for f in findings:
        if f.level == "error":
            log.error("hermes config %s: %s", f.key, f.message)
        elif f.level == "warn":
            log.warning("hermes config %s: %s", f.key, f.message)


async def ensure_started(send=None) -> None:
    """scheduler と API を一度だけ起動する。再呼び出しは何もしない。

    send: 通知配送 callable ``async (channel_id, text) -> None``。
    省略時は Discord REST 送信（jobs.scheduler の既定）。"""
    global _started, _loop
    if _started:
        log.info("runtime already started; skipping duplicate startup")
        return
    _started = True
    loop = asyncio.get_running_loop()
    _loop = loop
    from .. import backup
    from ..config import DATA_DIR
    fixed = await asyncio.to_thread(backup.fix_readonly, DATA_DIR)
    if fixed:
        log.warning("startup: cleared read-only attr on %d data files: %s",
                    len(fixed), fixed[:20])
    from .. import jobs
    _tasks.append(loop.create_task(jobs.scheduler(send), name="scheduler"))
    try:
        from ..server import serve
        _tasks.append(loop.create_task(serve(), name="api"))
    except ImportError:
        log.warning("fastapi/uvicorn 未インストールのためAPI窓口は無効です")
    _tasks.append(loop.create_task(_check_hermes_config(), name="hermes-check"))
    _tasks.append(loop.create_task(_seal_guard(), name="canonical-seal"))
    log.info("runtime started: %s", [t.get_name() for t in _tasks])


async def _seal_guard() -> None:
    """L-MIG Phase 1 の起動ガード（seal 厳格化）。

    authority が dual_run / canonical のとき、canonical 記録 lane が
    開けなければログ＋通知で可視化する。dual_run で canonical 不録の
    まま mem0 書込みが来た場合は memory.store_turn が seal 判定で
    raise する（mem0 書込み路は canonical なしで開かない）。
    起動そのものは止めない —— 止めるのは「書込み」の方だけ。"""
    try:
        from .. import memory
        auth = memory.authority()
        if auth not in ("dual_run", "canonical"):
            return
        from ..adapters import live
        if await asyncio.to_thread(live.lane.ensure_open, 15.0):
            log.info("canonical seal ok (authority=%s)", auth)
            return
        log.error("canonical seal failed: core.db が開けません"
                  " (authority=%s) — mem0 書込み路は閉じます", auth)
        try:
            from .. import db
            db.update_state(mutator=lambda s: s.__setitem__(
                "canonical_seal",
                {"ok": False, "authority": auth,
                 "at": __import__("datetime").datetime.now(
                     __import__("datetime").timezone.utc).isoformat()}))
        except Exception:
            pass
    except Exception:
        log.exception("canonical seal guard failed")


async def shutdown(timeout: float = 5.0) -> None:
    """常駐タスクをキャンセルして終了を待つ。bot終了・再起動の直前に呼ぶ"""
    global _started, _loop
    for t in _tasks:
        t.cancel()
    if _tasks:
        _, pending = await asyncio.wait(_tasks, timeout=timeout)
        for t in pending:
            log.warning("runtime task %s did not stop within %.1fs",
                        t.get_name(), timeout)
    _tasks.clear()
    _started = False
    _loop = None
    # codex app-server を立てていたら子プロセスごと畳む（未使用なら何もしない）
    try:
        import sys
        mod = sys.modules.get("companion.runtime.codex")
        if mod is not None:
            await asyncio.to_thread(mod.shutdown)
    except Exception:
        log.exception("codex app-server shutdown failed")
