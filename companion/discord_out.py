"""Discord REST送信 — websocket接続なしでチャンネルに投稿するだけの薄い口。

Hermes gateway が Discord の受信（websocket）を持つ構成で、companion 側は
リマインダー・digest 等の能動送信だけを担う。REST はゲートウェイ接続と
競合しないため同じbotトークンを共有できる。

戻り値/例外:
- 送信成功: None
- UndeliverableError: 404/403/401（再試行しても届かない → outbox 側で give_up）
- その他の httpx/HTTP エラー: そのまま投げる（再試行対象）
"""
import logging
import os

import httpx

log = logging.getLogger("companion")

_API = "https://discord.com/api/v10"

# Discordの本文上限は2000字。UAや改行処理の安全側で少し引く
_CHUNK_LIMIT = 1990


class UndeliverableError(Exception):
    """チャンネル消失・権限なし等、再試行しても届かない恒久的失敗"""


def _split_chunks(text: str) -> list[str]:
    """上限を跨ぐ本文を複数メッセージに分ける。改行→空白→強制断ちの順"""
    chunks, rest = [], text
    while len(rest) > _CHUNK_LIMIT:
        cut = rest.rfind("\n", _CHUNK_LIMIT // 2, _CHUNK_LIMIT)
        if cut < 0:
            cut = rest.rfind(" ", _CHUNK_LIMIT // 2, _CHUNK_LIMIT)
        if cut < 0:
            cut = _CHUNK_LIMIT
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest.strip() or not chunks:
        chunks.append(rest)
    return chunks


async def send_channel_message(channel_id: int, text: str) -> None:
    """Bot token で REST 経由のメッセージ送信。長い本文は複数通に分ける
    （途中失敗は呼び出し元のoutbox再試行に委ねる。先頭分は重複送信になり得るが
    全文欠落より重複の方が軽い）"""
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise UndeliverableError("DISCORD_TOKEN が .env に設定されていません")
    headers = {
        "Authorization": f"Bot {token}",
        # UAなし/不明なUAはCloudflareが403で弾く
        "User-Agent": "DiscordBot (companion, 1.0)",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in _split_chunks(text):
            res = await client.post(
                f"{_API}/channels/{int(channel_id)}/messages",
                headers=headers,
                json={"content": chunk},
            )
            if res.status_code in (200, 201):
                continue
            if res.status_code in (401, 403, 404):
                raise UndeliverableError(
                    f"discord send {res.status_code}: {res.text[:200]}")
            res.raise_for_status()
