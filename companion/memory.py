import asyncio
import gc
import logging
import os
import threading
import time
from datetime import datetime, timezone

from mem0 import Memory

from .config import (
    DATA_DIR,
    EMBED_DIMS,
    EMBED_IDLE_SEC,
    EMBED_LAZY,
    EMBED_MODEL,
    LLM_BASE_URL,
    LLM_MODEL,
    MEMORY_AGENT,
    MEMORY_USER,
)

log = logging.getLogger("companion")


class Mem0Seam:
    """mem0 呼出しの唯一の通過点（L-MIG Phase 1 の単一漏斗）。

    素の Memory 実体への全アクセスはここを通り、メソッドごとに calls
    に計数する（dual-run 観測・CI lint が「他経路で mem0 を触らない」
    ことを担保する根拠）。露出メソッドは使用箇所のものだけ。
    """

    def __init__(self, impl):
        self._impl = impl
        self.calls = {"add": 0, "search": 0, "update": 0, "get_all": 0,
                      "delete": 0, "other": 0}

    def add(self, *a, **k):
        self.calls["add"] += 1
        return self._impl.add(*a, **k)

    def search(self, *a, **k):
        self.calls["search"] += 1
        return self._impl.search(*a, **k)

    def update(self, *a, **k):
        self.calls["update"] += 1
        return self._impl.update(*a, **k)

    def get_all(self, *a, **k):
        self.calls["get_all"] += 1
        return self._impl.get_all(*a, **k)

    def delete(self, *a, **k):
        self.calls["delete"] += 1
        return self._impl.delete(*a, **k)

    def __getattr__(self, name):
        self.calls["other"] += 1
        return getattr(self._impl, name)


# -------------------------------------------------- authority（L-MIG）
def authority() -> str:
    """記憶の読み書き権威: mem0（従来）| dual_run | canonical。

    state.json の memory_authority を読む。env MEMORY_AUTHORITY は
    試験・スクリプト用の強制上書き。
    """
    env = os.getenv("MEMORY_AUTHORITY")
    if env:
        return env
    try:
        from . import db
        return db.load_state().get("memory_authority") or "mem0"
    except Exception:
        return "mem0"


def _grace_active() -> bool:
    """猶予期間中か（canonical→mem0 逆配送の有効窓）。"""
    try:
        from . import db
        until = db.load_state().get("mem0_grace_until") or ""
        return bool(until) and datetime.now(timezone.utc).isoformat() < until
    except Exception:
        return False


def _canonical_lane():
    """canonical 記録 lane（遅延 import で import 時副作用を避ける）。"""
    from .adapters import live
    return live.lane


def _canonical_ready() -> bool:
    """seal 判定: canonical が開いて録れるか。"""
    try:
        return _canonical_lane().probe()
    except Exception:
        return False


def _canonical_recall(query: str, scope: str, k: int = 15) -> str:
    """canonical 供給面からの想起（authority=canonical の読み経路）。

    context_assembly receipt が発行されてから blocks を返す
    （receipt-before-response —— SD-11 は canonical 側でも同じ約束）。
    接続は lane worker スレッド所有なので lane.call 経由で実行する。
    """
    lane = _canonical_lane()

    def _q():
        sup = lane.supply()
        if sup is None:
            return None
        return sup.supply_context(
            scopes=[scope], query=query, purpose="turn",
            consumer="engine", k=k)

    try:
        res = lane.call(_q)
    except Exception:
        log.exception("canonical recall failed")
        return "(まだ記憶はありません)"
    if res is None:
        return "(まだ記憶はありません)"
    lines = [f"- {b['state'].get('content', '')}"
             for b in res.get("blocks") or []
             if b.get("type") == "memory" and b.get("state")]
    return "\n".join(lines) if lines else "(まだ記憶はありません)"


def _canonical_items(scope: str, limit: int) -> list:
    """canonical record → mem0 item 形（{id, memory, metadata, created_at}）。

    呼び出し側の既存コードが mem0 item dict を期待するので、その形に
    写すだけの橋。供給経路ではなく監査用 browse（receipt 無し）。"""
    lane = _canonical_lane()

    def _q():
        sup = lane.supply()
        if sup is None:
            return []
        out = []
        for r in sup.browse(scope, family="memory", status="active",
                            limit=limit):
            st = r.get("state") or {}
            meta = dict(st.get("metadata") or {})
            if scope == "agent":
                meta.setdefault("agent_id", MEMORY_AGENT)
            out.append({"id": r.get("record_id") or st.get("record_id"),
                        "memory": st.get("content") or "",
                        "metadata": meta,
                        "created_at": st.get("created_at"),
                        "score": st.get("retrieval_strength")})
        return out

    try:
        return lane.call(_q) or []
    except Exception:
        log.exception("canonical browse failed")
        return []


def _canonical_search_items(query: str, scope: str, k: int) -> list:
    """canonical 供給面からの検索 → mem0 item 形の列。"""
    lane = _canonical_lane()

    def _q():
        sup = lane.supply()
        if sup is None:
            return []
        res = sup.supply_context(
            scopes=[scope], query=query, purpose="turn",
            consumer="engine", k=k)
        return [{"id": (b.get("state") or {}).get("record_id"),
                 "memory": (b.get("state") or {}).get("content") or "",
                 "metadata": (b.get("state") or {}).get("metadata") or {},
                 "created_at": (b.get("state") or {}).get("created_at")}
                for b in res.get("blocks") or []
                if b.get("type") == "memory" and b.get("state")]

    try:
        return lane.call(_q) or []
    except Exception:
        log.exception("canonical search failed")
        return []


def _canonical_note(text, scope, mtype, importance, metadata):
    """canonical へのメモ書込み（admission 経由・producer=explicit）。"""
    lane = _canonical_lane()
    from .kernel import canon as _canon
    now = datetime.now(timezone.utc).isoformat()
    rid = _canon.hash_tuple("pcore.explicit_mem.v1", text, scope,
                            now)[:20]
    meta = dict(metadata or {})
    prop = {
        "record_family": "memory", "target_id": None,
        "new_state": {
            "record_id": f"mem_x_{rid}", "record_family": "memory",
            "scope": scope, "status": "active", "record_version": 1,
            "memory_type": meta.get("type") or mtype,
            "content": text,
            "content_hash": _canon.content_hash(text),
            "sensitivity": "normal",
            "importance": max(1, min(5, int(importance or 3))),
            "retrieval_strength": float(meta.get("confidence") or 0.5),
            "tier": "hot", "pinned": False,
            "metadata": meta,
            "created_at": now, "updated_at": now,
        },
        "basis_events": [],
        "generated_by": "explicit", "producer": "explicit",
    }

    def _q():
        adm = lane.admission()
        if adm is None:
            raise RuntimeError("canonical lane not open")
        return adm.submit(prop, actor="companion")

    return lane.call(_q)


def _canonical_self_note(text, importance, metadata):
    return _canonical_note(text, "agent", "insight", importance,
                           metadata)


def _canonical_user_fact(text, metadata):
    return _canonical_note(text, "user", "fact", 3, metadata)


# -------------------------------------------------- seam 経由の公開写し面
# L-MIG Phase 1: mem0 呼出しは全てこのファイルの公開関数を通る。
# 他モジュールが `memory.add/search/get_all/...` を直接叩くことは CI lint
# （tests/test_mem0_seam.py）で禁止する。

def search_user(query: str, top_k: int = 10):
    """user scope の記憶検索（authority で canonical に切替）。"""
    if authority() == "canonical":
        return {"results": _canonical_search_items(query, "user", top_k)}
    return memory.search(query, top_k=top_k,
                         filters={"user_id": MEMORY_USER})


def search_agent(query: str, top_k: int = 10):
    if authority() == "canonical":
        return {"results": _canonical_search_items(query, "agent",
                                                   top_k)}
    return memory.search(query, top_k=top_k,
                         filters={"agent_id": MEMORY_AGENT})


def all_user(top_k: int = 500):
    """user scope の全件列挙（週次 digest・export・obsidian 同期用）。"""
    if authority() == "canonical":
        return {"results": _canonical_items("user", top_k)}
    return memory.get_all(filters={"user_id": MEMORY_USER}, top_k=top_k)


def all_agent(top_k: int = 500):
    if authority() == "canonical":
        return {"results": _canonical_items("agent", top_k)}
    return memory.get_all(filters={"agent_id": MEMORY_AGENT},
                          top_k=top_k)


def add_user_fact(text: str, metadata=None):
    """user scope への verbatim fact 書込み（infer=False 相当）。"""
    if authority() == "canonical":
        out = _canonical_user_fact(text, metadata)
        if _grace_active():
            try:
                memory.add([{"role": "user", "content": text}],
                           user_id=MEMORY_USER, infer=False,
                           metadata=metadata)
            except Exception:
                log.exception("grace reverse delivery failed (fact)")
        return out
    return memory.add([{"role": "user", "content": text}],
                      user_id=MEMORY_USER, infer=False,
                      metadata=metadata)


def delete_item(item_id: str):
    """項目削除（旧系専用面。canonical 権威では tombstone 化が要承認の
    ためここでは処理しない —— skipped を返す）。"""
    if authority() == "canonical":
        return {"skipped": "canonical mode", "id": item_id}
    return memory.delete(item_id)


class LazyEmbedder:
    """遅延ロード + アイドル時自動アンロードする埋め込みラッパー。

    fastembed の ONNX モデルは常駐すると1GB級のRAMを占有するため、
    発言処理の直前にロードし、EMBED_IDLE_SEC 秒使われなければ解放する。
    """

    def __init__(self, model_name, dims):
        self._model_name = model_name
        self.embedding_dims = dims
        self._model = None
        self._lock = threading.Lock()
        self._timer = None

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding
            t0 = time.time()
            self._model = TextEmbedding(model_name=self._model_name)
            log.info("embed model loaded in %.1fs", time.time() - t0)

    def _unload(self):
        with self._lock:
            self._model = None
            self._timer = None
            gc.collect()
            log.info("embed model unloaded (idle)")

    def _touch(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(EMBED_IDLE_SEC, self._unload)
        self._timer.daemon = True
        self._timer.start()

    def embed(self, text, memory_action=None):
        with self._lock:
            self._ensure()
            vec = list(self._model.embed(text.replace("\n", " ")))[0].tolist()
            self._touch()
            return vec

    def embed_batch(self, texts, memory_action="add"):
        return [self.embed(t, memory_action) for t in texts]


def _create_memory():
    """mem0本体の生成。import時ではなく初回使用時に呼ばれる"""
    m = Memory.from_config(
        {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "companion",
                    "path": str(DATA_DIR / "qdrant"),
                    "embedding_model_dims": EMBED_DIMS,
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
                "config": {"model": EMBED_MODEL, "embedding_dims": EMBED_DIMS},
            },
            "history_db_path": str(DATA_DIR / "history.db"),
            "custom_instructions": (
                "日本語で保存してください。特に重要視して記憶すべきもの: "
                "約束・予定・行きたい場所・人からもらった/あげた贈り物・"
                "出来事・気分の変化・人間関係。ユーザーの好みや基本情報も記録。"
                "メッセージ冒頭の[YYYY-MM-DD]はその発言日時です。記憶には"
                "可能な限り絶対日付（例: 2025年8月、2025-09-29）を含めてください。"
            ),
        }
    )
    if EMBED_LAZY:
        m.embedding_model = LazyEmbedder(EMBED_MODEL, EMBED_DIMS)
        gc.collect()
        log.info("lazy embedder enabled (idle unload after %ds)", EMBED_IDLE_SEC)
    return Mem0Seam(m)


class _LazyMemory:
    """import時にQdrant等を開かない遅延プロキシ。

    初めて属性に触れたときだけ実体を生成する。テストや別入口からの
    importが本番データに結びつかないようにするための境界。
    """

    _impl = None
    _init_lock = threading.Lock()

    def _get(self):
        if self._impl is None:
            with self._init_lock:
                if self._impl is None:
                    self._impl = _create_memory()
        return self._impl

    def __getattr__(self, name):
        return getattr(self._get(), name)


memory = _LazyMemory()


def format_memories(results) -> str:
    items = results.get("results", []) if isinstance(results, dict) else (results or [])
    lines = [f"- {m.get('memory', m)}" for m in items]
    return "\n".join(lines) if lines else "(まだ記憶はありません)"


async def recall(query: str) -> str:
    if authority() == "canonical":
        return await asyncio.to_thread(
            _canonical_recall, query, "user", 15)
    res = await asyncio.to_thread(
        memory.search, query, top_k=15, filters={"user_id": MEMORY_USER}
    )
    return format_memories(res)


async def store_turn(user_msg: str, assistant_msg: str, ts=None) -> None:
    """往復をユーザー記憶へ保存する。失敗は呼び出し側が拾えるよう伝播させる

    authority 別の書込み経路:
    - mem0（従来）: mem0 add（infer=既定、mem0 側 LLM 抽出）
    - dual_run: seal ゲート（canonical 不録なら書かない=raise）＋
      mem0 verbatim（infer=False —— canonical shadow と同じ原料）
    - canonical: canonical は記録 lane が担う。mem0 への書込みは
      猶予期間だけ verbatim 逆配送（best-effort）
    """
    stamp = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts or "")[:10]
    stamped = f"[{stamp}] {user_msg}" if stamp else user_msg
    auth = authority()
    if auth == "dual_run":
        if not _canonical_ready():
            raise RuntimeError(
                "canonical not recording; mem0 write path sealed")
        await asyncio.to_thread(
            memory.add,
            [
                {"role": "user", "content": stamped},
                {"role": "assistant", "content": assistant_msg},
            ],
            user_id=MEMORY_USER,
            infer=False,
        )
        return
    if auth == "canonical":
        if _grace_active():
            try:
                await asyncio.to_thread(
                    memory.add,
                    [
                        {"role": "user", "content": stamped},
                        {"role": "assistant", "content": assistant_msg},
                    ],
                    user_id=MEMORY_USER,
                    infer=False,
                )
            except Exception:
                log.exception("grace reverse delivery failed (turn)")
        return   # canonical 側は記録 lane が往復を既に刻んでいる
    await asyncio.to_thread(
        memory.add,
        [
            {"role": "user", "content": stamped},
            {"role": "assistant", "content": assistant_msg},
        ],
        user_id=MEMORY_USER,
    )


# ---------- companion 自身の記憶（agent scope） ----------

def add_self_note(text: str, importance: int | None = None, metadata: dict | None = None):
    meta = dict(metadata or {})
    if importance is not None:
        meta["importance"] = importance
        meta.setdefault("confidence", importance / 5.0)
    # 経験した瞬間=最後に思い出した時点。減衰の起点にする
    meta.setdefault("last_recalled_at", datetime.now(timezone.utc).isoformat())
    if authority() == "canonical":
        out = _canonical_self_note(text, importance, meta)
        if _grace_active():
            try:
                memory.add(
                    [{"role": "user", "content": text}],
                    agent_id=MEMORY_AGENT,
                    infer=False,
                    metadata=meta or None,
                )
            except Exception:
                log.exception("grace reverse delivery failed (self_note)")
        return out
    return memory.add(
        [{"role": "user", "content": text}],
        agent_id=MEMORY_AGENT,
        infer=False,
        metadata=meta or None,
    )


def _boost_confidence(item: dict) -> None:
    """想起された記憶を強化: confidence += 0.1 * (1 - c)（connectome式の減衰つき成長）。

    mem0 の update(metadata=) は既存payloadにマージされる。text未指定でも内部で
    再埋め込みが走るが、直前のsearchでembedderは暖機済みなのでコストは小さい。
    想起日時を記録し、減衰は「最後に思い出した時点」から計る。
    """
    mid = item.get("id")
    if not mid:
        return
    meta = dict(item.get("metadata") or {})
    c = float(meta.get("confidence") or 0.5)
    meta["confidence"] = round(min(1.0, c + 0.1 * (1.0 - c)), 3)
    meta["last_recalled_at"] = datetime.now(timezone.utc).isoformat()
    try:
        memory.update(mid, metadata=meta)
    except Exception:
        log.debug("confidence boost failed for %s", mid)


def _boost_all(items: list[dict]) -> None:
    for it in items:
        _boost_confidence(it)


def _self_score(item: dict) -> float:
    """relevance(mem0 score) × importance×confidence × recency の簡易リランク"""
    rel = float(item.get("score") or 0)
    meta = item.get("metadata") or {}
    imp = float(meta.get("importance", 3)) / 5.0
    conf = float(meta.get("confidence", 0.5))
    created = item.get("created_at") or ""
    recency = 0.5
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
        recency = 1.0 / (1.0 + age_days / 30.0)
    except Exception:
        pass
    return rel * (0.5 + imp * (0.5 + conf)) * (0.5 + recency)


async def recall_self(query: str, top_k: int = 8) -> str:
    if authority() == "canonical":
        return await asyncio.to_thread(
            _canonical_recall, query, "agent", top_k)
    res = await asyncio.to_thread(
        memory.search, query, top_k=top_k * 2, filters={"agent_id": MEMORY_AGENT}
    )
    items = res.get("results", []) if isinstance(res, dict) else (res or [])
    items.sort(key=_self_score, reverse=True)
    top = items[:top_k]
    if top:
        # boostは返信生成を遅らせないようバックグラウンドで
        asyncio.get_running_loop().run_in_executor(None, _boost_all, top)
    lines = [f"- {m.get('memory', m)}" for m in top]
    return "\n".join(lines) if lines else "(まだ記憶はありません)"


_DECAY_FLOOR = 0.05
_DECAY_GRACE_DAYS_PER_IMP = 7.0   # importance 1 → 7日、5 → 35日の猶予
_DECAY_WEEKLY_FACTOR = 0.9        # 猶予超過後は週あたり10%減（日割り複利）
_DECAY_MIN_DELTA = 0.005          # これ未満の変化は書き込まない（同日再実行で churn しない）


def _meta_dt(meta: dict, key: str):
    v = meta.get(key)
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _decay_targets(items, now=None) -> list[dict]:
    """減衰対象と予定値を計算する共通部（書き込みはしない）。

    基準は作成日ではなく「最後に思い出した時刻」。想起日時が不明な記憶は
    対象にしない（移行時に policy_started_at を刻んで起点にする）。
    猶予を超えた分だけ last_decay_at 起点の増分を掛けるので、繰り返し
    実行しても同じ分を二重に減衰しない。
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for it in items:
        meta = dict(it.get("metadata") or {})
        try:
            conf = float(meta.get("confidence") or 0.5)
            imp = float(meta.get("importance") or 3)
        except (TypeError, ValueError):
            continue
        anchor = _meta_dt(meta, "last_recalled_at") or _meta_dt(meta, "policy_started_at")
        if anchor is None:
            continue
        grace = max(7.0, imp * _DECAY_GRACE_DAYS_PER_IMP)
        idle = max(0.0, (now - anchor).total_seconds() / 86400.0)
        if idle <= grace:
            continue
        mark = _meta_dt(meta, "last_decay_at")
        since_mark = max(0.0, (now - (max(anchor, mark) if mark else anchor)).total_seconds() / 86400.0)
        new_conf = max(_DECAY_FLOOR, round(conf * (_DECAY_WEEKLY_FACTOR ** (since_mark / 7.0)), 3))
        if conf - new_conf < _DECAY_MIN_DELTA:
            continue
        out.append({
            "id": it.get("id"),
            "memory": it.get("memory"),
            "importance": imp,
            "confidence": conf,
            "new_confidence": new_conf,
            "idle_days": round(idle, 1),
            "grace_days": grace,
            "meta": meta,
        })
    return out


async def demote_stale() -> int:
    """思い出されない自己記憶の confidence を減衰させる。

    importance が高いほど猶予が長い。再実行・再起動で同じ記憶を
    二重に減衰しないよう last_decay_at からの増分だけ掛ける。
    """
    stamp = datetime.now(timezone.utc).isoformat()
    n = 0
    for t in _decay_targets(await self_memories_recent(10000)):
        meta = dict(t["meta"])
        meta["confidence"] = t["new_confidence"]
        meta["last_decay_at"] = stamp
        try:
            await asyncio.to_thread(memory.update, t["id"], metadata=meta)
            n += 1
        except Exception:
            log.debug("demote failed for %s", t["id"])
    if n:
        log.info("demoted %d stale self-memories", n)
    return n


async def simulate_decay(limit: int = 10000) -> list[dict]:
    """書き込まない試算。対象・現在値・減衰後の値を返す（適用は要バックアップ＋別承認）"""
    items = await self_memories_recent(limit)
    return [{k: v for k, v in t.items() if k != "meta"} for t in _decay_targets(items)]


def migrate_decay_policy() -> dict:
    """既存の自己記憶に policy_started_at を一度だけ刻む移行用（手動・要バックアップ）。

    last_recalled_at は不明なので付けない。作成日を想起日と誤認して
    一気に減衰させないため、ポリシー開始日を起点にする。
    """
    res = memory.get_all(filters={"agent_id": MEMORY_AGENT}, top_k=10000)
    items = res.get("results", []) if isinstance(res, dict) else (res or [])
    stamp = datetime.now(timezone.utc).isoformat()
    n = 0
    for it in items:
        meta = dict(it.get("metadata") or {})
        if meta.get("policy_started_at"):
            continue
        meta["policy_started_at"] = stamp
        memory.update(it.get("id"), metadata=meta)
        n += 1
    return {"stamped": n, "total": len(items)}


async def self_memories_recent(limit: int = 50) -> list[dict]:
    res = await asyncio.to_thread(
        memory.get_all, filters={"agent_id": MEMORY_AGENT}, top_k=limit
    )
    items = res.get("results", []) if isinstance(res, dict) else (res or [])
    return items
