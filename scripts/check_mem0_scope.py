"""Spike: mem0 OSS agent_id scoping.

user_id と agent_id が別スコープとして分離されることを確認する。
LLM不要（infer=False で直接書き込み）。
"""
from pathlib import Path

from mem0 import Memory

DATA = Path(__file__).resolve().parent.parent / "data_test"

memory = Memory.from_config(
    {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "companion_scope_test",
                "path": str(DATA / "qdrant_scope"),
                "embedding_model_dims": 384,
            },
        },
        "llm": {
            "provider": "openai",
            "config": {
                "model": "gpt-5",
                "api_key": "unused",
                "openai_base_url": "http://127.0.0.1:1/v1",
            },
        },
        "embedder": {
            "provider": "fastembed",
            "config": {
                "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                "embedding_dims": 384,
            },
        },
        "history_db_path": str(DATA / "history_scope.db"),
    }
)

# ユーザーの記憶
memory.add("マスターはコーヒーより紅茶が好き", user_id="me", infer=False)
# コンパニオン自身の記憶
memory.add("私はマスターに「紅茶派なんですね」と言って微笑まれた", agent_id="companion", infer=False)
# metadata 付き書き込み（importance想定）
memory.add(
    "マスターと初めて映画の話をした",
    agent_id="companion",
    infer=False,
    metadata={"importance": 4},
)

print("--- user scope ---")
res = memory.search("紅茶", top_k=10, filters={"user_id": "me"})
for it in res.get("results", []):
    print("user hit:", it["memory"])

print("--- agent scope ---")
res = memory.search("紅茶", top_k=10, filters={"agent_id": "companion"})
for it in res.get("results", []):
    print("agent hit:", it["memory"], "| meta:", it.get("metadata"))

print("--- agent get_all ---")
res = memory.get_all(filters={"agent_id": "companion"})
for it in res.get("results", []):
    print("agent all:", it["memory"])

print("DONE")
