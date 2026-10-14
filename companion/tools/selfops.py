"""companion 自身のプロセス操作ツール。

restart_self は旧 `!restart` コマンドのツール版。Discord listener 退役後も
delegate 反映のための再起動を彼女自身が行えるようにする。

呼び出しは dispatch_tool から asyncio.to_thread 経由（このスレッドには
イベントループが無い）なので、排水→shutdown→exit はメインループへ
call_soon_threadsafe で投げ、返信がクライアントへ届く猶予を取ってから
落ちる。
"""
import asyncio
import logging
import os
import subprocess
import threading

from ..config import ROOT

log = logging.getLogger("companion")

# 返信が先に出る猶予 + restart.bat 側の起動待ち(5s)より早く落ちる時間
_EXIT_DELAY_SEC = 3.0


async def _deferred_exit() -> None:
    from .. import engine, runtime

    await asyncio.sleep(_EXIT_DELAY_SEC)
    try:
        await engine.drain_background(timeout=5.0)
    except Exception:
        log.exception("background drain failed")
    try:
        await runtime.shutdown(timeout=3.0)
    except Exception:
        log.exception("runtime shutdown failed")
    os._exit(0)


def self_restart() -> str:
    """companion プロセスを restart.bat 経由で再起動する"""
    from .. import runtime

    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    subprocess.Popen(
        ["cmd", "/c", "start", "", "/min", "restart.bat"],
        cwd=str(ROOT), creationflags=flags,
    )
    loop = runtime.main_loop()
    if loop is not None:
        loop.call_soon_threadsafe(lambda: loop.create_task(_deferred_exit()))
    else:
        # ループ未起動=排水対象も無い。実用上到達しないが保険として遅延 exit
        threading.Timer(_EXIT_DELAY_SEC, lambda: os._exit(0)).start()
    return "再起動するね。ちょっと待ってて"
