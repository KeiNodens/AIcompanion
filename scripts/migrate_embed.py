"""埋め込みモデル切替の記憶移行: 旧ベクトル空間の記憶テキストを新モデルで再登録する"""
import shutil
import sys
from pathlib import Path

from mem0 import Memory

DATA = Path(__file__).resolve().parent.parent / "data"
QDRANT = DATA / "qdrant"
USER = "me"

OLD_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
OLD_DIMS = 384
NEW_MODEL = "google/embeddinggemma-300m"
NEW_DIMS = 768

import os
from dotenv import load_dotenv
load_dotenv()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5.6-luna")


def make(model, dims):
    return Memory.from_config(
        {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "companion",
                    "path": str(QDRANT),
                    "embedding_model_dims": dims,
                },
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "model": LLM_MODEL,
                    "api_key": "unused",
                    "openai_base_url": LLM_BASE_URL,
                    "reasoning_effort": "low",
                    "temperature": 0.3,
                },
            },
            "embedder": {
                "provider": "fastembed",
                "config": {"model": model, "embedding_dims": dims},
            },
            "history_db_path": str(DATA / "history.db"),
        }
    )


# 1. 旧空間から全記憶テキストを取得
old = make(OLD_MODEL, OLD_DIMS)
items = old.get_all(filters={"user_id": USER}, top_k=10000)
facts = [(i["memory"], i.get("metadata") or {}) for i in items.get("results", [])]
print(f"old memories: {len(facts)}")
del old

# 2. 旧ベクトルストアを削除（history.dbは残す）
if facts:
    shutil.rmtree(QDRANT)

# 3. 新モデル（初回はDLされる）で再登録
new = make(NEW_MODEL, NEW_DIMS)
for text, meta in facts:
    new.add(text, user_id=USER, metadata=meta, infer=False)
print(f"re-registered: {len(facts)}")

# 4. 疎通確認
res = new.search("プレゼント", filters={"user_id": USER}, top_k=3)
for r in res.get("results", []):
    print("hit:", r["memory"][:80])
print("MIGRATION DONE")
