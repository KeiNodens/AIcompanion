"""形式橋（runtime-hermes-tooling.md §3、S1 保留ターン型）。

Hermes が tools 付きで /v1/chat/completions を呼んできたとき、engine の
tool loop は companion 側に残したまま、身体ツール（body__* に改名して
彼女に見せる）の呼出しだけを OpenAI 形式の tool_calls として Hermes に
返し、role:"tool" の継続リクエストで結果が戻るまで engine を保留する。
turn・tool trace は companion 側の正本（turns / turn_tool_calls）に残る。

Suspension は session_key ごとに最大1つ。server が継続リクエストを
pending の Future に引き当てて engine を再開させる。
"""
import asyncio
import dataclasses
import fnmatch
import logging
from collections.abc import Awaitable, Callable

from .config import BRIDGE_RESUME_TIMEOUT

log = logging.getLogger("companion")

# 彼女に見せる身体ツール名の接頭辞（companion 側ツールとの衝突防止＋
# プロンプトで「body__* は身体の手足」と説明するための目印）
PREFIX = "body__"

# Hermes 側で自分自身に見えるツールの接頭辞（橋渡しするとループするので除外）。
# Hermes の MCP 命名規約は mcp__<server>__<tool>（tools/mcp_tool_schema.py）
_SELF_PREFIXES = ("mcp__companion", "mcp_companion")


class BridgeAborted(Exception):
    """保留中の橋が中断された（新しい発言・タイムアウト・再起動等）"""


@dataclasses.dataclass
class BodyBridge:
    """engine._generate に渡す橋の面。emit は Suspension が供給する"""

    schemas: list[dict]      # 彼女に見せるスキーマ（名前順）
    names: dict[str, str]    # 見せ名 → Hermes 元名
    emit: Callable[[list[dict]], Awaitable[list[str]]]
    # 引数 [{"id": tool_call_id, "name": Hermes元名, "arguments": JSON文字列}]、
    # 戻り値は同順の結果テキスト。中断は BridgeAborted で伝わる


def build_schemas(
    hermes_tools: list, patterns: list[str]
) -> tuple[list[dict], dict[str, str]]:
    """Hermes が送ってきた tools から橋渡し対象のスキーマを作る。

    対象は type=function で名前が glob に一致するもの。mcp__companion*
    （自分自身）は除く。見せ名は PREFIX+元名（64字を超えるものは捨てる）、
    description に【身体】を冠し、parameters はそのまま。名前順に並べる。
    戻り値: (スキーマ列, 見せ名→元名の対応表)"""
    pats = [p.strip() for p in patterns if p and p.strip()]
    schemas: list[dict] = []
    names: dict[str, str] = {}
    for t in hermes_tools or []:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = t.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name") or ""
        if not name or name.startswith(_SELF_PREFIXES):
            continue
        if not any(fnmatch.fnmatch(name, p) for p in pats):
            continue
        shown = PREFIX + name
        if len(shown) > 64:
            continue
        schemas.append({
            "type": "function",
            "function": {
                "name": shown,
                "description": "【身体】" + str(fn.get("description") or ""),
                "parameters": fn.get("parameters") or {},
            },
        })
        names[shown] = name
    schemas.sort(key=lambda s: s["function"]["name"])
    return schemas, names


# session_key → Suspension（保留ターンはセッションに最大1つ）
_suspended: dict[str, "Suspension"] = {}


class Suspension:
    """保留中の形式橋ターン。engine タスク・結果待ち Future・
    server へのイベントを束ねる"""

    def __init__(self, session_key: str = ""):
        self.session_key = session_key
        self.task: asyncio.Task | None = None
        # ("calls", [..]) / ("done", result) / ("error", exc)
        self.events: asyncio.Queue = asyncio.Queue()
        self.pending: dict[str, asyncio.Future] = {}
        self.order: list[tuple[str, str]] = []  # (id, Hermes名) emit 順
        self.aborted = False

    async def emit(self, calls: list[dict]) -> list[str]:
        """engine からの橋渡し要求。tool_calls を events に積んで
        結果が揃うまで待つ。タイムアウト・中断は BridgeAborted"""
        if self.aborted:
            raise BridgeAborted()
        loop = asyncio.get_running_loop()
        self.pending = {}
        self.order = []
        futs = []
        for c in calls:
            fut = loop.create_future()
            self.pending[c["id"]] = fut
            futs.append(fut)
            self.order.append((c["id"], c["name"]))
        await self.events.put(("calls", calls))
        try:
            return await asyncio.wait_for(
                asyncio.gather(*futs), BRIDGE_RESUME_TIMEOUT
            )
        except asyncio.CancelledError:
            self.aborted = True
            raise
        except Exception:
            self.aborted = True
            raise BridgeAborted("body bridge interrupted")

    def abort(self) -> None:
        """未解決の Future に BridgeAborted を入れて engine を締める"""
        self.aborted = True
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(BridgeAborted("body bridge aborted"))
        self.pending = {}

    def on_task_done(self, task: asyncio.Task) -> None:
        """engine タスクの完了をイベントに写す。
        abort 済みなら受け手のいない保留を _suspended から外す"""
        try:
            if task.cancelled():
                self.events.put_nowait(
                    ("error", BridgeAborted("body bridge cancelled"))
                )
            elif task.exception() is not None:
                self.events.put_nowait(("error", task.exception()))
            else:
                self.events.put_nowait(("done", task.result()))
        finally:
            if self.aborted and _suspended.get(self.session_key) is self:
                _suspended.pop(self.session_key, None)
