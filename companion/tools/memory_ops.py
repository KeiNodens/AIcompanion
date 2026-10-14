from ..memory import add_user_fact, format_memories, search_user


def mem_save(fact: str) -> str:
    add_user_fact(fact)
    return "記憶しました"


def mem_search(query: str) -> str:
    res = search_user(query, top_k=10)
    return format_memories(res)
