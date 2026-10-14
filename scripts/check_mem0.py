"""Smoke test: mem0 init + local embedder + qdrant store + infer=False add/search.

LLM is never called (infer=False path stores text directly), so this works
without ChatMock running.
"""

from pathlib import Path

from mem0 import Memory

DATA = Path(__file__).resolve().parent.parent / "data_test"

memory = Memory.from_config(
    {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "companion_test",
                "path": str(DATA / "qdrant"),
                "embedding_model_dims": 384,
            },
        },
        "llm": {
            "provider": "openai",
            "config": {
                "model": "gpt-5",
                "api_key": "unused",
                "openai_base_url": "http://127.0.0.1:1/v1",  # unreachable on purpose
            },
        },
        "embedder": {
            "provider": "fastembed",
            "config": {
                "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                "embedding_dims": 384,
            },
        },
        "history_db_path": str(DATA / "history.db"),
    }
)

r = memory.add(
    [{"role": "user", "content": "先週Aさんの誕生日に本をプレゼントした。あと大濠公園に行きたい。"}],
    user_id="me",
    infer=False,
)
print("ADD:", r)

res = memory.search("Aさんに去年何をプレゼントしたっけ", top_k=5, filters={"user_id": "me"})
items = res.get("results", res) if isinstance(res, dict) else res
for it in items:
    print("HIT:", it.get("memory") if isinstance(it, dict) else it, "| score:", it.get("score") if isinstance(it, dict) else "?")

res2 = memory.search("行きたい場所", top_k=5, filters={"user_id": "me"})
items2 = res2.get("results", res2) if isinstance(res2, dict) else res2
for it in items2:
    print("HIT2:", it.get("memory") if isinstance(it, dict) else it, "| score:", it.get("score") if isinstance(it, dict) else "?")
