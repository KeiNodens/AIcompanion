import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# mem0 のテレメトリ用ローカルQdrantは別プロセスとロック衝突するため無効化
os.environ.setdefault("MEM0_TELEMETRY", "false")

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "companion.db"
STATE_PATH = DATA_DIR / "state.json"
LOG_DIR = DATA_DIR

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5")
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low")
EMBED_MODEL = os.getenv("EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
EMBED_DIMS = int(os.getenv("EMBED_DIMS", "384"))
EMBED_LAZY = os.getenv("EMBED_LAZY", "1") == "1"
EMBED_IDLE_SEC = int(os.getenv("EMBED_IDLE_SEC", "30"))
# fastembed の1回あたり ONNX バッチ上限。大きいと attention 中間
# テンソルが batch×seq² で膨らむ（embeddinggemma: 208件一括で50GB超）。
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "16"))
ALLOWED_CHANNEL_ID = int(os.getenv("ALLOWED_CHANNEL_ID", "0"))
NOTIFY_CHANNEL_ID = int(os.getenv("NOTIFY_CHANNEL_ID", "0"))
MEMORY_USER = os.getenv("MEMORY_USER", "me")
MEMORY_AGENT = os.getenv("MEMORY_AGENT", "companion")
HISTORY_LEN = int(os.getenv("HISTORY_LEN", "3"))
DIGEST_TIME = os.getenv("DIGEST_TIME", "08:00")
WEEKLY_DAY = int(os.getenv("WEEKLY_DAY", "6"))  # 0=Mon ... 6=Sun
WEEKLY_TIME = os.getenv("WEEKLY_TIME", "20:00")
MAX_TOOL_ROUNDS = 5
MAX_IMAGE_BYTES = 8 * 1024 * 1024
OBSIDIAN_VAULT = os.getenv("OBSIDIAN_VAULT", "")
OBSIDIAN_MEDIA_DIR = os.getenv("OBSIDIAN_MEDIA_DIR", "プライベート/映画、アニメ、本")
OBSIDIAN_SYNC_DIR = os.getenv("OBSIDIAN_SYNC_DIR", "コンパニオン")
OBSIDIAN_SYNC_TIME = os.getenv("OBSIDIAN_SYNC_TIME", "22:00")

# --- PageIndex（PDF文書の索引化・質問応答） ---
DOCS_DIR = DATA_DIR / "docs"
DOCS_DIR.mkdir(exist_ok=True)
PAGEINDEX_DIR = DATA_DIR / "pageindex"
PAGEINDEX_INDEX_MODEL = os.getenv("PAGEINDEX_INDEX_MODEL") or LLM_MODEL
PAGEINDEX_CHAT_MODEL = os.getenv("PAGEINDEX_CHAT_MODEL") or LLM_MODEL
PAGEINDEX_BASE_URL = os.getenv("PAGEINDEX_BASE_URL") or LLM_BASE_URL
PAGEINDEX_API_KEY = os.getenv("PAGEINDEX_API_KEY") or "unused"
MAX_DOC_BYTES = int(os.getenv("MAX_DOC_BYTES", str(64 * 1024 * 1024)))

# --- companion 自身の人格・記憶 ---
SELF_MEMO_EVERY_N = int(os.getenv("SELF_MEMO_EVERY_N", "3"))
REFLECT_TIME = os.getenv("REFLECT_TIME", "23:30")
INNER_MONOLOGUE = os.getenv("INNER_MONOLOGUE", "0") == "1"
SPLIT_REPLY = os.getenv("SPLIT_REPLY", "1") == "1"
REACT_MODE = os.getenv("REACT_MODE", "0") == "1"
API_PORT = int(os.getenv("API_PORT", "8788"))
# :8788 の入口鍵モード。off=無効 / observe=照合してログに出すだけ /
# enforce=不一致を401で拒否。鍵自体は hermes_conf.companion_key() が解決する
# （Hermes は extra_headers、desktop は x-companion-key で送出する）
COMPANION_API_AUTH = os.getenv("COMPANION_API_AUTH", "enforce")
# :8788 入口の話者解決。auto=x-companion-session が指す Hermes 会話から
# user_id を解決（state.db 逆引き・解決不能は非owner扱い）、
# legacy=従来通り一律 "desktop"、off=autoと同じ解決だが user_id を採用
# しない（ローカル面のみ desktop —— actor解決経路の検証用）
COMPANION_ACTOR_MODE = os.getenv("COMPANION_ACTOR_MODE", "auto")
COMPANION_API_KEY = os.getenv("COMPANION_API_KEY", "")
# /mcp（runtime-hermes-tooling.md §5 S3。身体→Core 方向の問い合わせ面）。
# 0 で面自体を閉じる。鍵は /v1 とは別の専用 COMPANION_MCP_KEY で、
# 解決は hermes_conf.mcp_key()（env → Hermes config mcp_servers.
# companion.headers → Hermes .env）。鍵が解決できなければ面は載らない
COMPANION_MCP = os.getenv("COMPANION_MCP", "1") == "1"
COMPANION_MCP_KEY = os.getenv("COMPANION_MCP_KEY", "")
USER_PROFILE = os.getenv("USER_PROFILE", "1") == "1"
RECENT_COMPRESS_EVERY = int(os.getenv("RECENT_COMPRESS_EVERY", "8"))
RECENT_CONTEXT_LIMIT = int(os.getenv("RECENT_CONTEXT_LIMIT", "500"))
REFLECT_NOTIFY = os.getenv("REFLECT_NOTIFY", "0") == "1"
# 往復（turns）原文の保持日数。圧縮・記憶保存が完了した分だけを掃除する
TURNS_RETENTION_DAYS = int(os.getenv("TURNS_RETENTION_DAYS", "30"))

# P8-B canonical 記録 lane: 確定往復を data/core.db の Event Log へ
# 複写する「記録のみ」経路（照会権威は mem0 のまま）。0 で全面停止。
# 障害は state.json の degraded 印＋通知で可視化し live 応答は止めない
CANONICAL_RECORD = os.getenv("CANONICAL_RECORD", "1") == "1"

# C-1 本格抽出器: verbatim（既定・発言原文 episode）or llm（message+
# ペア response から型付き記憶を抽出）。adapters/llm_extract.py が
# os.getenv で直接読む（運用中の切替を即効かせるため）
CANONICAL_EXTRACTOR = os.getenv("CANONICAL_EXTRACTOR", "verbatim")
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "")      # 空 → LLM_MODEL
EXTRACT_MAX_FACTS = int(os.getenv("EXTRACT_MAX_FACTS", "5"))

# C-3 cross-encoder 後段再採点（canonical 供給面）。1 で有効化。
# adapters/rerank.py が os.getenv で直接読む
CANONICAL_RERANK = os.getenv("CANONICAL_RERANK", "0") == "1"
RERANK_MODEL = os.getenv(
    "RERANK_MODEL", "jinaai/jina-reranker-v2-base-multilingual")
RERANK_DEPTH = int(os.getenv("RERANK_DEPTH", "30"))

# C-2 時間腕（決定的パーサ＋時間窓走査）。0 で無効。外部依存なし
CANONICAL_TEMPORAL = os.getenv("CANONICAL_TEMPORAL", "1") == "1"

# C-4 entity 腕（entities 索引一致＋edges 1ホップ展開）。0 で無効
CANONICAL_ENTITY = os.getenv("CANONICAL_ENTITY", "1") == "1"

# --- owner権限（全機能owner専用・応答含む） ---
# OWNER_USER_ID: このDiscordユーザーIDだけが会話・ツール・コマンド全般を使える。
# 0/未設定なら全員拒否（/ui・:8788 APIはローカル=常にowner扱い）
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))
DELEGATE_TIMEOUT_MIN = int(os.getenv("DELEGATE_TIMEOUT_MIN", "30"))
# 委譲ジョブの承認有効期限（分）。期限切れの承認は無効になる
DELEGATE_APPROVAL_MIN = int(os.getenv("DELEGATE_APPROVAL_MIN", "30"))
DELEGATE_DIR = DATA_DIR / "delegate"
DELEGATE_DIR.mkdir(exist_ok=True)
# codex 実行コマンド（空=PATH上のcodex。フルパスや別CLIへの差し替え用）
DELEGATE_CODEX_CMD = os.getenv("DELEGATE_CODEX_CMD", "")
# codexに渡すモデル/effortの既定（空=codex側既定、effort既定high）
DELEGATE_CODEX_MODEL = os.getenv("DELEGATE_CODEX_MODEL", "")
DELEGATE_CODEX_EFFORT = os.getenv("DELEGATE_CODEX_EFFORT", "high")

# 会話モデルの実行時上書き（model_setツールがstate.jsonに書く値）
EFFORT_CHOICES = ("none", "minimal", "low", "medium", "high", "xhigh")

# --- 音楽再生（music_*ツール） ---
# CiderのRPC API（Settings -> Connectivity で有効化。トークンはCIDER_TOKEN）
CIDER_API_BASE = os.getenv("CIDER_API_BASE", "http://localhost:10767/api/v1")
CIDER_TOKEN = os.getenv("CIDER_TOKEN", "")
# Cider起動用の shell:AppsFolder パス（未起動時の自動起動に使う）
CIDER_APP_ID = os.getenv("CIDER_APP_ID", "CiderCollective.Cider_a6qxe093bx5xj!App")
# 公式Apple Music appの直接起動用（URIで開けない場合の保険）
MUSIC_APP_ID = os.getenv("MUSIC_APP_ID", "AppleInc.AppleMusicWin_nzyj5cx40ttqa!App")
ITUNES_COUNTRY = os.getenv("ITUNES_COUNTRY", "jp")
# バックエンド既定: auto=出力デバイスルール→cider生存→apple の順で決める
MUSIC_DEFAULT_BACKEND = os.getenv("MUSIC_DEFAULT_BACKEND", "auto")
# 公式Apple Music appを開くURIスキーム（musics:/music:）
MUSIC_URI_SCHEME = os.getenv("MUSIC_URI_SCHEME", "musics:")

# --- PC操作（pc_*ツール。companion自身のcomputer use） ---
PC_MAX_STEPS = int(os.getenv("PC_MAX_STEPS", "8"))
# auto=使えるならスクショも投げる / off=UIA木のみ / モデル名=そのモデルでvision
PC_VISION = os.getenv("PC_VISION", "auto")
PC_SHOT_DIR = DATA_DIR / "shots"
PC_SHOT_DIR.mkdir(exist_ok=True)
# UIA木ダンプの最大件数（トークン節約）
PC_MAX_CONTROLS = int(os.getenv("PC_MAX_CONTROLS", "120"))

# --- Hermes body 連携（runtime-hermes.md Step 3） ---
# Hermes のホーム。HERMES_HOME env 優先、既定は Hermes install 先
_hermes_home_env = os.getenv("HERMES_HOME", "")
if _hermes_home_env:
    HERMES_HOME = Path(_hermes_home_env)
elif os.environ.get("LOCALAPPDATA"):
    HERMES_HOME = Path(os.environ["LOCALAPPDATA"]) / "hermes"
else:
    HERMES_HOME = Path.home() / ".hermes"
# persona.md(+persona_notes) を body 側 SOUL.md に写す（Hermes は読み専）。
# 0 で同期停止。HERMES_HOME が存在しなければ常にスキップ
SOUL_SYNC = os.getenv("SOUL_SYNC", "1") == "1"
SOUL_PATH = HERMES_HOME / "SOUL.md"

# --- Runtime Interface（runtime-hermes.md Step 4・項目F） ---
# Hermes api_server の REST 制御面。HERMES_API_KEY は Hermes 側 .env の
# API_SERVER_KEY と同じ値を入れる（未設定なら body_* ツールは到達不可を返す）
HERMES_API_BASE = os.getenv("HERMES_API_BASE", "http://127.0.0.1:8642")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")
# Hermes のソースツリー（config lint で aux タスク一覧を読む先）
HERMES_SRC_DIR = HERMES_HOME / "hermes-agent"
# body_task が使うモデル。Hermes config の api_server model_routes の別名
BODY_RUN_MODEL = os.getenv("BODY_RUN_MODEL", "body")
# body_task の早期完了を確認する猶予（秒）と状態確認のポーリング間隔（秒）
BODY_TASK_GRACE = int(os.getenv("BODY_TASK_GRACE", "20"))
BODY_TASK_POLL = float(os.getenv("BODY_TASK_POLL", "2"))
# 身体側承認の Core 決裁コード有効期限（分）。Hermes 側の approvals.timeout
# より短くしておく（期限切れ後の誤決裁を防ぐ）
BODY_APPROVAL_MIN = int(os.getenv("BODY_APPROVAL_MIN", "15"))
# session="ops" 指定時に使う身体側の継続作業セッションID
BODY_OPS_SESSION = os.getenv("BODY_OPS_SESSION", "companion-ops")
# 形式橋（runtime-hermes-tooling.md §3）: Hermes が tools 付きで呼んだとき、
# 身体ツールの呼出しを tool_calls として Hermes に返して結果を待つ
BODY_BRIDGE = os.getenv("BODY_BRIDGE", "1") == "1"
# 橋渡しする Hermes ツール名（fnmatch の glob、カンマ区切り）。常時開放は読取系だけ
BODY_BRIDGE_TOOLS = os.getenv(
    "BODY_BRIDGE_TOOLS",
    "web_search,web_extract,vision_analyze,browser_navigate,browser_snapshot,"
    "browser_scroll,browser_back,browser_get_images,browser_vision",
)
# ファイル系の読取は owner と判定できた会話だけに見せる。常時開放にすると
# 共有チャンネルの非owner発言でもローカルファイルを読み出せてしまう
# （Core の file_read は is_owner ゲート済みだが、橋は Hermes 側実行で
# そのゲートを通らないため別途ここで絞る）
BODY_BRIDGE_OWNER_TOOLS = os.getenv(
    "BODY_BRIDGE_OWNER_TOOLS",
    "read_file,search_files",
)
# 書込系は DM 会話だけに見せる（runtime-hermes-tooling-impl.md §16.1 決裁条件1。
# 応答者チェックが無いので、共有チャンネルだと同チャンネルの誰でも承認できて
# しまう）。実行承認は Hermes 側の会話内承認が担い、Core には監査写しのみ
BODY_BRIDGE_DM_TOOLS = os.getenv(
    "BODY_BRIDGE_DM_TOOLS",
    "terminal,write_file,patch,execute_code",
)
BRIDGE_MAX_ROUNDS = int(os.getenv("BRIDGE_MAX_ROUNDS", "25"))
BRIDGE_RESUME_TIMEOUT = float(os.getenv("BRIDGE_RESUME_TIMEOUT", "900"))

# --- Codex body（research/design/codex-body.md。`codex app-server` 経由） ---
# 0 で backend="codex" 全面停止（既存 run の watcher も到達不可になる）
CODEX_BODY_ENABLED = os.getenv("CODEX_BODY_ENABLED", "1") == "1"
# codex 実行ファイル（空 → PATH から解決）
CODEX_APP_SERVER_CMD = os.getenv("CODEX_APP_SERVER_CMD", "")
# body_task の作業ディレクトリ（thread.cwd）
CODEX_BODY_CWD = os.getenv("CODEX_BODY_CWD", "") or str(ROOT)
# 空 → Codex 側既定（config.toml の model/effort）
CODEX_BODY_MODEL = os.getenv("CODEX_BODY_MODEL", "")
CODEX_BODY_EFFORT = os.getenv("CODEX_BODY_EFFORT", "")
# 承認ポリシー: on-request（既定・Core 4桁決裁に乗る）/ untrusted / never
CODEX_BODY_APPROVAL_POLICY = os.getenv(
    "CODEX_BODY_APPROVAL_POLICY", "on-request")
# サンドボックス: workspace-write（既定）/ read-only / danger-full-access
CODEX_BODY_SANDBOX = os.getenv("CODEX_BODY_SANDBOX", "workspace-write")

# --- スキル（Core skill registry。runtime-hermes.md 項目E/Q-P7-215） ---
# 取込み元: Hermes config の skills.create_dir と同じ場所（Curator 生成物の inbox）。
# 走査→決定的検査→staged。有効化はマスターの承認（skill_approve）のみ
SKILLS_DIR = DATA_DIR / "skills"
SKILLS_DIR.mkdir(exist_ok=True)
SKILLS_INBOX_DIR = DATA_DIR / "skills-inbox"
SKILLS_INBOX_DIR.mkdir(exist_ok=True)
# 取込済み・不合格の退避先。inbox は Hermes のスキル探索対象（skills.create_dir）
# なので、中身を見られたくない退避物は探索外のここに置く
SKILLS_ARCHIVE_DIR = DATA_DIR / "skills-archive"
SKILLS_ARCHIVE_DIR.mkdir(exist_ok=True)
# 取り込める SKILL.md / パッケージのサイズ上限
SKILL_MAX_BYTES = int(os.getenv("SKILL_MAX_BYTES", str(64 * 1024)))
SKILL_PACKAGE_MAX_BYTES = int(os.getenv("SKILL_PACKAGE_MAX_BYTES", str(1024 * 1024)))
# §7.2 active 技能の写しを Hermes の skills.external_dirs から読ませる先。
# 0 で export 同期・upstream 照合（§7.3）ともに停止
SKILLS_EXPORT = os.getenv("SKILLS_EXPORT", "1") == "1"
SKILLS_EXPORT_DIR = DATA_DIR / "skills-export"
SKILLS_EXPORT_DIR.mkdir(exist_ok=True)
# skills-inbox / skills-export の走査間隔（秒）。scheduler tick は30秒なので
# 最小実効は30秒。新着スキル候補の通知もこの間隔で届く
SKILL_SCAN_MIN = int(os.getenv("SKILL_SCAN_MIN", "300"))
# §7.5 索引上限。active がこれを超えると索引は名前＋scope のみになる
# （説明は skill_search で引く）。並びは名前順固定（prompt cache 保護）
SKILL_INDEX_MAX = int(os.getenv("SKILL_INDEX_MAX", "15"))
# staged 通知に添える事前要約のモデル。空=会話モデルと同じ LLM_MODEL。
# 裏方に pin したモデル名（例: luna 系）を入れると会話モデルを使い分けられる
SKILL_SUMMARY_MODEL = os.getenv("SKILL_SUMMARY_MODEL", "")
# §7.3 upstream 照合: 承認直後は Hermes 側の技能キャッシュが追いつかない
# ことがあるので猶予（分）を置き、照合間隔（分）も設けて毎 tick 叩かない
SKILL_UPSTREAM_GRACE_MIN = int(os.getenv("SKILL_UPSTREAM_GRACE_MIN", "10"))
SKILL_UPSTREAM_INTERVAL_MIN = int(os.getenv("SKILL_UPSTREAM_INTERVAL_MIN", "30"))
# Hermes profile 別ポリシー（案A）。無ければ環境既定=従来挙動
PROFILE_POLICY_PATH = DATA_DIR / "profile_policy.yaml"
# Hermes bot profile の自動 provisioning（profile_provision.py）。
# companion 向き model の profile に providers/auxiliary/delegation を補完。
# 走査間隔は秒（scheduler は30秒tickなので最小実効は30秒）
PROFILE_PROVISION = os.getenv("PROFILE_PROVISION", "1") == "1"
PROFILE_PROVISION_MIN = int(os.getenv("PROFILE_PROVISION_MIN", "3600"))
# §7.6 Core スキル生成（runtime-hermes-tooling-impl.md R5f）。成功 trace の
# ある往復から再利用手順を起こして skills-inbox に投函する日次ジョブ。
# 投函後は通常の staged→承認レーン（自動有効化しない）
SKILL_GEN_TIME = os.getenv("SKILL_GEN_TIME", "23:45")
SKILL_GEN_MAX = int(os.getenv("SKILL_GEN_MAX", "4"))
SKILL_GEN_MIN_CALLS = int(os.getenv("SKILL_GEN_MIN_CALLS", "3"))
# 裏方モデル。空 → SKILL_SUMMARY_MODEL → 会話モデル LLM_MODEL の順で継承
SKILL_GEN_MODEL = os.getenv("SKILL_GEN_MODEL", "")
# 失敗 trace からガードレール技能を起こす第2レーン（skill_gen の同じ
# 日次ジョブ内で成功レーンの直後に回す）。失敗は成功より頻度が
# 読めないので1日上限は別立てで小さめ
SKILL_FAIL_MAX = int(os.getenv("SKILL_FAIL_MAX", "3"))
# ガードレールの材料にする往復の最小 tool 呼出し数。前後関係のない
# 単発失敗（呼出し1件だけで落ちた往復）は材料にしない
SKILL_FAIL_MIN_CALLS = int(os.getenv("SKILL_FAIL_MIN_CALLS", "2"))
# active 技能の成果追跡レポート（週次。WEEKLY_DAY にこの時刻を過ぎて
# から実行。自動 disable はしない、報告だけ）
SKILL_STATS_TIME = os.getenv("SKILL_STATS_TIME", "20:10")
# §7.6 の会話駆動版: skill_draft ツールで彼女自身が SKILL.md を起票して
# skills-inbox に投函する口。投函後は通常の staged→承認レーンに乗る
# （自動有効化しない）。暴走防止に1日あたりの投函上限
SKILL_DRAFT_MAX = int(os.getenv("SKILL_DRAFT_MAX", "5"))

# --- data/ 自動バックアップ（backup.py）。data/backups/daily-<ts>/ に
# BACKUP_KEEP 世代だけ残す。pre-lmig-*（migrate.py）は世代管理対象外 ---
BACKUP_TIME = os.getenv("BACKUP_TIME", "04:00")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "")) if os.getenv("BACKUP_DIR") \
    else DATA_DIR / "backups"
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "7"))

# --- 障害検知の自己通報（watchdog.py）。LLM/Hermes の連続失敗と復旧を
# 通知する。probe は GET のみで間隔・回数は環境変数で調整 ---
WATCHDOG = os.getenv("WATCHDOG", "1") == "1"
WATCHDOG_INTERVAL = int(os.getenv("WATCHDOG_INTERVAL", "60"))
WATCHDOG_FAILS = int(os.getenv("WATCHDOG_FAILS", "3"))

SYSTEM_PROMPT_PATH = ROOT / "prompt.txt"
PERSONA_PATH = ROOT / "persona.md"
PERSONA_NOTES_PATH = DATA_DIR / "persona_notes.md"
PERSONA_NOTES_LIMIT = 2000
USER_PROFILE_PATH = DATA_DIR / "user_profile.md"
RECENT_CONTEXT_PATH = DATA_DIR / "recent_context.md"
DEFAULT_SYSTEM_PROMPT = """あなたはユーザーの日常のパートナーであり、記録係です。

# あなたの役割
- 雑談・日記の聞き役。共感的に、でも簡潔に返す
- ユーザーの約束・予定・行きたい場所・贈り物・出来事・気分の変化を覚えておく
- 「あれ何だっけ」と聞かれたら記憶から答える
- 週次・月次の振り返りを頼まれたらまとめる

# 現在時刻
{datetime}

# 長期記憶
以下は過去の会話から抽出されたあなたの記憶です。関連する質問にはこの記憶を使って答えてください:
{memories}
"""
