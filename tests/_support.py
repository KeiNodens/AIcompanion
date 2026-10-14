"""テスト用隔離基盤: companion.* を本番 data/ や mem0 から切り離してimportする。

- companion.config は一時ディレクトリを指す偽モジュール
- companion パッケージ自体は空スタブ（__init__ の logging/FileHandler 副作用を防ぐ）
- companion.tools パッケージも空スタブ（__init__ が全ツールをimportするのを避け、
  必要なサブモジュールだけ実ファイルから個別に読めるよう __path__ だけ残す）
- memory / llm / reflect 等の重い依存は偽モジュールを差し込む
"""
import asyncio
import contextlib
import importlib
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "companion"
TOOLS_DIR = PKG_DIR / "tools"


def fake_config(tmp: Path) -> types.ModuleType:
    """一時ディレクトリ向きの companion.config 偽モジュールを作る"""
    cfg = types.ModuleType("companion.config")
    cfg.ROOT = ROOT
    cfg.DATA_DIR = tmp / "data"
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg.DB_PATH = cfg.DATA_DIR / "companion.db"
    cfg.STATE_PATH = cfg.DATA_DIR / "state.json"
    cfg.LOG_DIR = cfg.DATA_DIR
    cfg.LLM_BASE_URL = "http://127.0.0.1:1/v1"
    cfg.LLM_MODEL = "test-model"
    cfg.REASONING_EFFORT = "low"
    cfg.EFFORT_CHOICES = ("none", "minimal", "low", "medium", "high", "xhigh")
    # Existing test fixtures exercise the explicit legacy/migration paths.
    cfg.DEFAULT_MEMORY_AUTHORITY = "mem0"
    cfg.MEMORY_USER = "me"
    cfg.MEMORY_AGENT = "companion"
    cfg.OWNER_USER_ID = 12345
    cfg.HISTORY_LEN = 6
    cfg.MAX_TOOL_ROUNDS = 5
    cfg.INNER_MONOLOGUE = True
    cfg.SPLIT_REPLY = True
    cfg.REACT_MODE = False
    cfg.ALLOWED_CHANNEL_ID = 999
    cfg.NOTIFY_CHANNEL_ID = 0
    cfg.DIGEST_TIME = "08:00"
    cfg.WEEKLY_DAY = 6
    cfg.WEEKLY_TIME = "20:00"
    cfg.OBSIDIAN_VAULT = ""
    cfg.OBSIDIAN_MEDIA_DIR = ""
    cfg.OBSIDIAN_SYNC_DIR = "コンパニオン"
    cfg.OBSIDIAN_SYNC_TIME = "22:00"
    cfg.SELF_MEMO_EVERY_N = 3
    cfg.REFLECT_TIME = "23:30"
    cfg.REFLECT_NOTIFY = False
    cfg.RECENT_COMPRESS_EVERY = 8
    cfg.RECENT_CONTEXT_LIMIT = 500
    cfg.TURNS_RETENTION_DAYS = 30
    # P8-B canonical 記録 lane（既定 off。lane テストが個別に有効化する）
    cfg.CANONICAL_RECORD = False
    cfg.USER_PROFILE = True
    cfg.API_PORT = 8788
    cfg.MAX_DOC_BYTES = 64 * 1024 * 1024
    cfg.MAX_IMAGE_BYTES = 8 * 1024 * 1024
    cfg.DOCS_DIR = cfg.DATA_DIR / "docs"
    cfg.DELEGATE_TIMEOUT_MIN = 30
    cfg.DELEGATE_APPROVAL_MIN = 30
    cfg.DELEGATE_DIR = cfg.DATA_DIR / "delegate"
    cfg.DELEGATE_DIR.mkdir(parents=True, exist_ok=True)
    cfg.DELEGATE_CODEX_CMD = ""
    cfg.DELEGATE_CODEX_MODEL = ""
    cfg.DELEGATE_CODEX_EFFORT = "high"
    cfg.SYSTEM_PROMPT_PATH = tmp / "prompt.txt"   # 存在しない → DEFAULT_SYSTEM_PROMPT
    cfg.PERSONA_PATH = tmp / "persona.md"          # 存在しない → ""
    cfg.PERSONA_NOTES_PATH = cfg.DATA_DIR / "persona_notes.md"
    cfg.PERSONA_NOTES_LIMIT = 2000
    cfg.USER_PROFILE_PATH = cfg.DATA_DIR / "user_profile.md"
    cfg.RECENT_CONTEXT_PATH = cfg.DATA_DIR / "recent_context.md"
    cfg.DEFAULT_SYSTEM_PROMPT = "test system {datetime} {memories} {self_memories}"
    cfg.CIDER_API_BASE = "http://127.0.0.1:1/api/v1"
    cfg.CIDER_TOKEN = ""
    cfg.CIDER_APP_ID = ""
    cfg.MUSIC_APP_ID = ""
    cfg.ITUNES_COUNTRY = "jp"
    cfg.MUSIC_DEFAULT_BACKEND = "cider"
    cfg.MUSIC_URI_SCHEME = "musics:"
    cfg.PC_MAX_STEPS = 8
    cfg.PC_VISION = "off"
    cfg.PC_SHOT_DIR = cfg.DATA_DIR / "shots"
    cfg.PC_MAX_CONTROLS = 120
    # Hermes 連携・スキル registry（runtime-hermes.md Step 3）
    cfg.HERMES_HOME = tmp / "hermes"          # 既定は未作成（テストで mkdir する）
    cfg.HERMES_SRC_DIR = cfg.HERMES_HOME / "hermes-agent"
    cfg.SOUL_SYNC = True
    cfg.SOUL_PATH = cfg.HERMES_HOME / "SOUL.md"
    cfg.COMPANION_API_AUTH = "observe"
    cfg.COMPANION_API_KEY = ""
    cfg.COMPANION_ACTOR_MODE = "auto"
    cfg.COMPANION_MCP = True
    cfg.COMPANION_MCP_KEY = ""
    cfg.SKILLS_DIR = cfg.DATA_DIR / "skills"
    cfg.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    cfg.SKILLS_INBOX_DIR = cfg.DATA_DIR / "skills-inbox"
    cfg.SKILLS_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    cfg.SKILLS_ARCHIVE_DIR = cfg.DATA_DIR / "skills-archive"
    cfg.SKILLS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    cfg.SKILL_MAX_BYTES = 64 * 1024
    cfg.SKILL_PACKAGE_MAX_BYTES = 1024 * 1024
    # R4: skills-export 配布口・索引上限・要約モデル・upstream 照合
    cfg.SKILLS_EXPORT = True
    cfg.SKILLS_EXPORT_DIR = cfg.DATA_DIR / "skills-export"
    cfg.SKILLS_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    cfg.SKILL_INDEX_MAX = 15
    cfg.SKILL_SUMMARY_MODEL = ""
    cfg.SKILL_UPSTREAM_GRACE_MIN = 10
    cfg.SKILL_UPSTREAM_INTERVAL_MIN = 30
    cfg.SKILL_SCAN_MIN = 300
    cfg.PROFILE_POLICY_PATH = cfg.DATA_DIR / "profile_policy.yaml"
    cfg.PROFILE_PROVISION = True
    cfg.PROFILE_PROVISION_MIN = 300
    # Runtime Interface（runtime-hermes.md Step 4）。HTTPはモックするので値は形だけ
    cfg.HERMES_API_BASE = "http://127.0.0.1:1"
    cfg.HERMES_API_KEY = "test-key"
    cfg.BODY_RUN_MODEL = "body"
    cfg.BODY_TASK_GRACE = 0.05
    cfg.BODY_TASK_POLL = 0.01
    cfg.BODY_APPROVAL_MIN = 15
    cfg.BODY_OPS_SESSION = "companion-ops"
    cfg.BODY_BRIDGE = True
    cfg.BODY_BRIDGE_TOOLS = "web_search,web_extract,browser_*"
    cfg.BODY_BRIDGE_OWNER_TOOLS = "read_file,search_files"
    cfg.BODY_BRIDGE_DM_TOOLS = "terminal,write_file,patch,execute_code"
    cfg.BRIDGE_MAX_ROUNDS = 25
    cfg.BRIDGE_RESUME_TIMEOUT = 30
    # Codex body（codex-body.md）。Popen はテストが偽プロセスに差し替える
    cfg.CODEX_BODY_ENABLED = True
    cfg.CODEX_APP_SERVER_CMD = "codex"
    cfg.CODEX_BODY_CWD = str(ROOT)
    cfg.CODEX_BODY_MODEL = ""
    cfg.CODEX_BODY_EFFORT = ""
    cfg.CODEX_BODY_APPROVAL_POLICY = "on-request"
    cfg.CODEX_BODY_SANDBOX = "workspace-write"
    # R5f Core スキル生成
    cfg.SKILL_GEN_TIME = "23:45"
    cfg.SKILL_GEN_MAX = 4
    cfg.SKILL_GEN_MIN_CALLS = 3
    cfg.SKILL_GEN_MODEL = ""
    cfg.SKILL_FAIL_MAX = 3
    cfg.SKILL_FAIL_MIN_CALLS = 2
    cfg.SKILL_STATS_TIME = "20:10"
    cfg.SKILL_DRAFT_MAX = 5
    cfg.BACKUP_TIME = "04:00"
    cfg.BACKUP_DIR = cfg.DATA_DIR / "backups"
    cfg.BACKUP_KEEP = 7
    cfg.WATCHDOG = True
    cfg.WATCHDOG_INTERVAL = 60
    cfg.WATCHDOG_FAILS = 3
    cfg.EMBED_MODEL = "x"
    cfg.EMBED_DIMS = 384
    cfg.EMBED_LAZY = True
    cfg.EMBED_IDLE_SEC = 30
    return cfg


def module_stub(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def tools_pkg_stub(dispatch=None) -> types.ModuleType:
    """companion.tools の空パッケージスタブ。engine用に TOOLS/dispatch_tool を持たせる。
    engine は import 時に dispatch_tool を束縛するため、差し替えは hook["fn"] で行う。"""
    tpkg = types.ModuleType("companion.tools")
    tpkg.__path__ = [str(TOOLS_DIR)]
    tpkg.TOOLS = []
    hook = {"fn": dispatch}

    class _Result:
        def __init__(self, status, text):
            self.status, self.text = status, text

    async def _dispatch_result(name, args):
        if hook["fn"]:
            res = hook["fn"](name, args)
            res = await res if asyncio.iscoroutine(res) else res
            return res if isinstance(res, _Result) else _Result("ok", str(res))
        return _Result("ok", f"stub:{name}")

    async def _dispatch(name, args):
        return (await _dispatch_result(name, args)).text

    tpkg.ToolResult = _Result
    tpkg.dispatch_tool_result = _dispatch_result
    tpkg.dispatch_tool = _dispatch
    tpkg.hook = hook
    return tpkg


def memory_stub() -> types.ModuleType:
    """companion.memory のスタブ。呼び出しは calls に記録する。

    engine/reflect は import 時に関数を束縛するため、失敗させたいテストは
    m.store_turn_hook / m.self_note_hook に呼び出し可能オブジェクトを
    セットして内部で差し込む（hook が raise すればそのまま伝播する）。
    """
    m = types.ModuleType("companion.memory")
    calls = {"store_turn": [], "add_self_note": []}

    async def recall(query, *a, **k):
        return "(記憶なし)"

    async def recall_self(query, top_k=8):
        return "(自己記憶なし)"

    async def store_turn(user_msg, assistant_msg, ts=None):
        if m.store_turn_hook:
            res = m.store_turn_hook(user_msg, assistant_msg, ts=ts)
            if asyncio.iscoroutine(res):
                await res
        calls["store_turn"].append((user_msg, assistant_msg))

    def add_self_note(*a, **k):
        if m.self_note_hook:
            m.self_note_hook(*a, **k)
        calls["add_self_note"].append((a, k))

    m.store_turn_hook = None
    m.self_note_hook = None
    m.recall = recall
    m.recall_self = recall_self
    m.store_turn = store_turn
    m.format_memories = lambda res: "(まだ記憶はありません)"
    m.add_self_note = add_self_note
    m.memory = mock.MagicMock()
    m.self_memories_recent = mock.AsyncMock(return_value=[])
    m.demote_stale = mock.AsyncMock(return_value=0)
    # L-MIG Phase 1 の公開 wrapper 面（seam 経由の唯一の呼出し口）
    m.authority = lambda: "mem0"
    m.search_user = lambda q, top_k=10: {"results": []}
    m.search_agent = lambda q, top_k=10: {"results": []}
    m.all_user = lambda top_k=500: {"results": []}
    m.all_agent = lambda top_k=500: {"results": []}
    m.add_user_fact = lambda text, metadata=None: {"ok": True}
    m.delete_item = lambda item_id: {"deleted": item_id}
    m.calls = calls
    return m


def make_resp(content: str = "", tool_calls=None):
    """llm.chat.completions.create の戻り値スタブ"""
    msg = mock.MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    msg.model_dump.return_value = {"role": "assistant", "content": content}
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


class FakeCompletions:
    """llm.chat.completions.create のスタブ。queue の先頭から応答/例外/関数を取り出す"""

    def __init__(self):
        self.queue = []
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        item = self.queue.pop(0) if self.queue else make_resp("(no queued response)")
        if isinstance(item, Exception):
            raise item
        if callable(item):
            res = item(kw)
            if asyncio.iscoroutine(res):
                res = await res
            return res
        return item


def llm_stub(fake: FakeCompletions | None = None) -> types.ModuleType:
    """companion.llm のスタブ"""
    m = types.ModuleType("companion.llm")
    fake = fake or FakeCompletions()
    m.llm = types.SimpleNamespace(chat=types.SimpleNamespace(completions=fake))
    m.current_params = lambda: ("test-model", "low")
    m.available_models = lambda: []
    m.fake = fake
    return m


def reflect_stub() -> types.ModuleType:
    """companion.reflect のスタブ"""
    calls = []

    async def maybe_store_self_episode(session_key):
        calls.append({"session": session_key})

    async def process_episode_material():
        calls.append({"process": True})

    m = types.ModuleType("companion.reflect")
    m.maybe_store_self_episode = maybe_store_self_episode
    m.process_episode_material = process_episode_material
    m.calls = calls
    m.run_reflection = mock.AsyncMock(return_value={"insights": []})
    return m


class IsolatedModules(contextlib.AbstractContextManager):
    """with IsolatedModules() as iso: iso.import_module("companion.db")

    companion.* を偽config + 任意スタブでimportし、終了時に sys.modules を元に戻す。
    stub_tools=True（既定）なら companion.tools を空パッケージスタブにする
    （False にすると実 __init__.py が読まれる）。
    """

    def __init__(
        self,
        tmp: Path | None = None,
        stubs: dict | None = None,
        stub_tools: bool = True,
    ):
        self.tmp = Path(tmp) if tmp else Path(tempfile.mkdtemp(prefix="companion_test_"))
        self.cfg = fake_config(self.tmp)
        self.stubs = dict(stubs or {})
        self.stub_tools = stub_tools
        self._saved = {}

    def __enter__(self):
        self._saved = {
            k: v
            for k, v in sys.modules.items()
            if k == "companion" or k.startswith("companion.")
        }
        for k in self._saved:
            sys.modules.pop(k, None)
        pkg = types.ModuleType("companion")
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules["companion"] = pkg
        if self.stub_tools:
            sys.modules["companion.tools"] = tools_pkg_stub()
        sys.modules["companion.config"] = self.cfg
        for name, mod in self.stubs.items():
            sys.modules[name] = mod
        return self

    def __exit__(self, *exc):
        for k in [
            k
            for k in sys.modules
            if k == "companion" or k.startswith("companion.")
        ]:
            sys.modules.pop(k, None)
        sys.modules.update(self._saved)
        return False

    def close(self):
        self.__exit__(None, None, None)

    def import_module(self, name: str):
        return importlib.import_module(name)
