"""docs.py 単体テスト: companionパッケージ(mem0/qdrant)をimportせず config をスタブ注入。
実行: .venv\\Scripts\\python.exe scripts/check_docs_tool.py
ChatMock(:8000) が必要。data_test/ 配下だけを触るのでbot稼働中でもロック衝突しない。
"""
import os
import sys
import types
import importlib.util
from pathlib import Path

os.environ["MEM0_TELEMETRY"] = "false"

ROOT = Path(__file__).resolve().parent.parent
TEST = ROOT / "data_test"

cfg = types.ModuleType("companion.config")
cfg.DATA_DIR = TEST
cfg.DOCS_DIR = TEST / "docs"
cfg.PAGEINDEX_DIR = TEST / "pageindex"
cfg.PAGEINDEX_API_KEY = "unused"
cfg.PAGEINDEX_BASE_URL = "http://127.0.0.1:8000/v1"
cfg.PAGEINDEX_INDEX_MODEL = "gpt-5.6-luna"
cfg.PAGEINDEX_CHAT_MODEL = "gpt-5.6-luna"
cfg.EMBED_DIMS = 768
cfg.EMBED_IDLE_SEC = 30
cfg.EMBED_LAZY = True
cfg.EMBED_MODEL = "google/embeddinggemma-300m"
cfg.LLM_BASE_URL = "http://127.0.0.1:8000/v1"
cfg.LLM_MODEL = "gpt-5.6-luna"
cfg.MEMORY_AGENT = "companion"
cfg.MEMORY_USER = "me"

pkg = types.ModuleType("companion")
pkg.__path__ = [str(ROOT / "companion")]
tpkg = types.ModuleType("companion.tools")
tpkg.__path__ = [str(ROOT / "companion" / "tools")]
sys.modules.update(
    {"companion": pkg, "companion.tools": tpkg, "companion.config": cfg}
)

spec = importlib.util.spec_from_file_location(
    "companion.tools.docs", ROOT / "companion" / "tools" / "docs.py"
)
docs = importlib.util.module_from_spec(spec)
sys.modules["companion.tools.docs"] = docs
spec.loader.exec_module(docs)

pdf = str(TEST / "docs" / "test_doc.pdf")

print("== ingest ==", flush=True)
print(docs.doc_ingest(pdf, "test"), flush=True)
print("\n== list ==", flush=True)
print(docs.doc_list(), flush=True)
print("\n== toc ==", flush=True)
print(docs.doc_toc("test_doc")[:1500], flush=True)
print("\n== ask ==", flush=True)
print(docs.doc_ask("この文書は何についての文書ですか？一文で", "test_doc"), flush=True)
