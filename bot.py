"""Personal companion — headless core entry.

Discord の受信（websocket）は Hermes gateway に一本化済み
（research/design/runtime-hermes.md Step 2）。このプロセスは
engine・記憶・jobs・:8788 API を持ち、通知は Discord REST で送る。
ローカル会話面は :8788 が配信する /ui ページをブラウザで開く。

旧来の Discord listener は `python -m companion.discord_bot` で起動できる
（ロールバック用。Hermes と同じトークンで二重接続すると殴り合うので注意）。
"""
import asyncio

from companion import runtime


async def _main() -> None:
    await runtime.ensure_started()
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
