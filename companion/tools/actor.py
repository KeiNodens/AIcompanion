"""ツール呼び出し元の話者識別（ownerゲート用）とリクエスト文脈。

engine.generate() がツールループ前に CURRENT_ACTOR / CURRENT_CHANNEL をセットする。
asyncio.to_thread は contextvars を伝播するので、スレッド内の
ハンドラからも読める。値は Discord では str(author.id)、
デスクトップ窓では "desktop"、未設定時は None。
CURRENT_CHANNEL は「この依頼が来た通知先チャンネルID」（リマインド等の宛先に使う）。
CURRENT_SESSION は会話セッションキー（body_task が依頼元セッションを知る用）。
"""
import contextvars

from ..config import OWNER_USER_ID

CURRENT_ACTOR = contextvars.ContextVar("companion_actor", default=None)
CURRENT_CHANNEL = contextvars.ContextVar("companion_channel", default=None)
CURRENT_SESSION = contextvars.ContextVar("companion_session", default=None)
# 呼出し元の Hermes profile（profile_policy 判定用。未解決は None）
CURRENT_PROFILE = contextvars.ContextVar("companion_profile", default=None)


def is_owner_actor(actor) -> bool:
    """actor 値そのものが owner か。

    "desktop" はローカル信頼面として常に owner。それ以外は
    OWNER_USER_ID との一致のみ（None/未設定は非 owner）"""
    if actor == "desktop":
        return True
    return bool(OWNER_USER_ID) and actor == str(OWNER_USER_ID)


def is_owner() -> bool:
    return is_owner_actor(CURRENT_ACTOR.get())
