"""OpenAI互換API shim — engineを /v1/chat/completions で公開する。

ローカル小窓ページ（/ui）・AIRI等の外部クライアントがこの口を使う。
engine/メモリはbotプロセス内で共有するため、uvicornはbotのイベントループ上で動かす。

互換範囲の明示:
- 処理するのは messages 内「最後のuser発言」だけ。クライアントが送る過去の
  会話履歴はサーバー側の記憶と連結しない（会話履歴はサーバー管理）
- content parts は text と image_url を受け付ける。それ以外の型は
  「読み取れない添付」として本文に印を残す（往復自体は拒否しない）
- stream=true は「生成完了後の分割送信」（疑似ストリーミング）。トークン単位の
  真のストリーミングは未実装
- session フィールドで信頼済みクライアントがセッションを識別できる
  （省略時は "desktop" 互換）
- Hermes gateway 経由の場合は `x-companion-session` ヘッダ（config.yaml の
  providers.<name>.session_affinity_header）に会話IDが乗る。ヘッダ優先で
  body の session より先に使い、入口識別として source="hermes" を記録する。
  actor は COMPANION_ACTOR_MODE で決める: auto=ヘッダが指す Hermes 会話の
  user_id を state.db から解決（解決不能は非owner扱い）、
  legacy=従来通り一律 "desktop"、off=autoと同じ解決だが user_id を
  採用しない検証用。ヘッダの無い入口はローカル信頼面で "desktop"
- 127.0.0.1限定・外部公開しない。入口鍵は COMPANION_API_AUTH で制御する:
  off=なし / observe=照合結果をログに出すだけで通す /
  enforce=不一致を401で拒否（既定）。鍵は `x-companion-key` ヘッダか
  `Authorization: Bearer` で受け、解決は hermes_conf.companion_key()
  （env の COMPANION_API_KEY → Hermes config の
  providers.<name>.extra_headers.x-companion-key の順）
- /mcp（runtime-hermes-tooling.md §5 S3）は別面: 専用鍵
  COMPANION_MCP_KEY が解決できるときだけ FastMCP アプリを載せ、
  /v1 系の鍵では開かない（逆も同じ）。鍵未設定の /mcp 要求は
  面ごと無い扱いで404
"""
import asyncio
import collections
import hashlib
import hmac
import json
import logging
import re
import time
import uuid

from . import bodyruns, bridge, db, hermes_conf, profile_policy, redact, webui
from .config import (
    API_PORT,
    BODY_BRIDGE,
    BODY_BRIDGE_DM_TOOLS,
    BODY_BRIDGE_TOOLS,
    COMPANION_ACTOR_MODE,
    COMPANION_API_AUTH,
    COMPANION_MCP,
    LLM_MODEL,
)
from .engine import generate
from .runtime import body as body_rt
from .runtime import hermes as hermes_rt
from .tools import delegate
from .tools.actor import is_owner_actor

log = logging.getLogger("companion")


def _unsupported_note(p) -> str:
    """対応外content partを「添付があったが読めない」の印に変換する。
    400で往復ごと拒否すると添付つきの一言が全部失敗するため、発言は
    通して彼女に形式だけ伝える（turnsにも読めなかった事実が残る）"""
    name = ""
    for k in ("file", "document", "input_audio", "audio"):
        v = p.get(k) if isinstance(p, dict) else None
        if isinstance(v, dict):
            name = v.get("filename") or v.get("name") or ""
            if name:
                break
    if not name and isinstance(p, dict):
        name = p.get("name") or p.get("filename") or str(p.get("type") or "?")
    return f"（添付「{name or '?'}」は読み取れない形式です）"


def _extract_user_input(messages):
    """最後のuser発言から (text, images) を取り出す"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content, []
        if isinstance(content, list):
            text_parts, images, notes = [], [], []
            for p in content:
                if not isinstance(p, dict):
                    notes.append("（読み取れない形式の添付があります）")
                    continue
                t = p.get("type")
                if t == "text":
                    text_parts.append(p.get("text") or "")
                elif t == "image_url":
                    u = p.get("image_url")
                    images.append(u.get("url") if isinstance(u, dict) else u)
                else:
                    notes.append(_unsupported_note(p))
            return "\n".join(text_parts + notes), images
    return "", []


SESSION_HEADER = "x-companion-session"
KEY_HEADER = "x-companion-key"

# observe モードの警告は同じ理由につきこの秒数で間引く
_AUTH_WARN_INTERVAL = 600.0
_auth_warned: dict[str, float] = {}
_unconfigured_logged = False


def _presented_key(headers) -> str:
    """リクエストに載った入口鍵。x-companion-key 優先、Bearer も受ける"""
    h = headers.get(KEY_HEADER)
    if h and h.strip():
        return h.strip()
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _auth_verdict(headers) -> str:
    """入口鍵との照合結果: ok / missing / invalid / unconfigured"""
    key, _src = hermes_conf.companion_key()
    if not key:
        return "unconfigured"
    presented = _presented_key(headers)
    if not presented:
        return "missing"
    ok = hmac.compare_digest(presented.encode("utf-8"), key.encode("utf-8"))
    return "ok" if ok else "invalid"


def _mcp_verdict(headers) -> str:
    """/mcp 専用鍵との照合結果。/v1 系の鍵は受けない面分離"""
    key, _src = hermes_conf.mcp_key()
    if not key:
        return "unconfigured"
    presented = _presented_key(headers)
    if not presented:
        return "missing"
    ok = hmac.compare_digest(presented.encode("utf-8"), key.encode("utf-8"))
    return "ok" if ok else "invalid"


# マスターの「承認 1234」発言（身体側承認の決裁コード）。全角数字は半角化する。
# Hermes 経由の発言は [Triggering message id: ...] 等の装飾行が前に付く
# ことがあるので、メッセージ全体一致ではなく「単独行として現れる」条件で
# 探す（re.MULTILINE ＋ search。行内空白は水平方向だけ: \s だと改行を跨ぐ）
_APPROVAL_RE = re.compile(
    r"^[ \t　]*(承認|許可|却下|拒否|approve|deny)[ \t　]*[#＃]?[ \t　]*"
    r"([0-9０-９]{4})[ \t　]*\r?$",
    re.IGNORECASE | re.MULTILINE,
)
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_APPROVE_WORDS = {"承認", "許可", "approve"}


def _actor_for_api(request) -> str | None:
    """API入口の話者判定（COMPANION_ACTOR_MODE）。

    x-companion-session が無い入口はローカル信頼面（/ui・
    スクリプト）で actor="desktop"。ヘッダがある Hermes 経路は:
    - auto: state.db で会話の user_id を解決（解決不能・リモート面で
      user_id 無し → None=非owner）
    - legacy: 従来通り一律 "desktop"
    - off: auto と同じ解決をするが user_id を採用しない
      （ローカル面のみ "desktop"）——解決経路の検証用
    """
    affinity = request.headers.get(SESSION_HEADER, "")
    if not affinity:
        return "desktop"
    mode = (COMPANION_ACTOR_MODE or "auto").strip().lower()
    if mode == "legacy":
        return "desktop"
    actor = body_rt.resolve_actor(affinity)
    if mode == "off":
        return "desktop" if actor == "desktop" else None
    return actor


def _maybe_approval_decision(user_text: str, actor) -> str | None:
    """「承認 1234」形式の発言を pending 承認（身体/委譲）の決裁に
    引き当てる。呼び出し側で owner 判定済み（非ownerはここに来ない）。

    pending の承認コードに一致すれば決裁を実行し、彼女に渡す会話文に
    差し替える（コード自体は文脈に残さない）。該当コードが無ければ
    None を返して素通し（普通の発言として処理される）。"""
    m = _APPROVAL_RE.search(user_text or "")
    if not m:
        return None
    code = m.group(2).translate(_FULLWIDTH_DIGITS)
    approve = m.group(1).lower() in _APPROVE_WORDS
    decided_by = actor or "api"
    pending = bodyruns.find_pending_by_code(code)
    if pending is not None:
        _ok, msg = bodyruns.decide(
            code=code, approve=approve, decided_by=decided_by,
        )
        summary = pending.get("request") or ""
        return (
            f"（マスターが身体の承認待ち「{summary[:100]}」を"
            f"{'承認' if approve else '却下'}した。結果: {msg}）"
        )
    job = delegate.find_pending_by_code(code)
    if job is not None:
        if approve:
            msg = delegate.delegate_approve(job["id"], decided_by)
        else:
            msg = delegate.delegate_deny(job["id"], decided_by)
        return (
            f"（マスターがコーディング依頼「{job['request'][:100]}」を"
            f"{'承認' if approve else '却下'}した。結果: {msg}）"
        )
    return None


# ---------------------------------------------------------------------------
# 形式橋（runtime-hermes-tooling.md §3）: Hermes が tools 付きで呼んだ
# via_hermes リクエストは、身体ツールの呼出しを tool_calls として返し、
# 結果が届くまで engine を保留する。session ごとに直列化するロックは
# engine._session_locks とは別物（保留中も新規判定を先に行うため）。
# ---------------------------------------------------------------------------
_bridge_locks = collections.defaultdict(asyncio.Lock)

# 再送の冪等化: key → {"task": Task}（処理中）または
# {"event": (kind, payload), "expires": monotonic}（完了後の返しうる結果）
_replay: dict[str, dict] = {}
_REPLAY_TTL = 120.0

# 保持中の保留が無いのに tool 結果だけ届いた（companion 再起動等）ときの応答
_ORPHANED_CONTINUATION = {
    "reply": "（ごめん、身体の操作の途中で私の側が途切れちゃった。"
             "もう一度お願いできる？）",
    "inner": "",
    "tools": [],
    "model": LLM_MODEL,
}


def _bridge_patterns() -> list[str]:
    return [p for p in BODY_BRIDGE_TOOLS.split(",") if p.strip()]


def _bridge_dm_patterns() -> list[str]:
    return [p for p in BODY_BRIDGE_DM_TOOLS.split(",") if p.strip()]


def _is_dm_identity(ident) -> bool:
    """書込系橋開放の条件: Hermes 会話が DM（runtime-hermes-tooling-impl.md
    §16.1 決裁条件1 — 応答者チェックが無いので共有面には見せない）。

    Hermes の session_key は `<ns>:<platform>:<chat_type>:…` で DM は
    chat_type=='dm'（ns 自体が "agent:main" の形でコロンを含むので
    chat_type は index 3）。session_key を持たない入口や state.db
    解決不能は False（fail closed）。"""
    parts = str((ident or {}).get("session_key") or "").split(":")
    return len(parts) >= 4 and parts[3] == "dm"


def _msg_text(content) -> str:
    """role:"tool"/"user" の content をテキスト化（list は text 部分を連結）"""
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text") or "")
            for p in content if isinstance(p, dict)
        )
    return "" if content is None else str(content)


def _continuation_results(susp: "bridge.Suspension",
                          messages: list) -> dict | None:
    """継続リクエストなら {tool_call_id: 結果text} を返す。違えば None。

    tool_call_id で照合するのが本筋。id が合わないときは最後の
    assistant(tool_calls) の関数名が emit 順と一致し、直後の tool
    メッセージ数も同じなら位置で対応付ける。最後の tool の後ろに user
    発言（Hermes の /steer）があれば末尾結果に追記する。"""
    results: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id") in susp.pending:
            results[m["tool_call_id"]] = _msg_text(m.get("content"))
    if not results:
        last_asst = None
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                last_asst = m
                break
        if last_asst is None:
            return None
        sent = [name for _id, name in susp.order]
        got = [
            (tc.get("function") or {}).get("name")
            for tc in last_asst.get("tool_calls") or []
        ]
        if not sent or got != sent:
            return None
        tail = [
            m for m in messages[messages.index(last_asst) + 1:]
            if m.get("role") == "tool"
        ]
        if len(tail) != len(susp.order):
            return None
        for (cid_, _name), m in zip(susp.order, tail):
            results[cid_] = _msg_text(m.get("content"))
    last_tool = max(
        (i for i, m in enumerate(messages) if m.get("role") == "tool"),
        default=-1,
    )
    steer = [
        t for m in messages[last_tool + 1:]
        if m.get("role") == "user"
        for t in [_msg_text(m.get("content"))] if t
    ]
    if steer and susp.order:
        last_id = susp.order[-1][0]
        results[last_id] = (
            results.get(last_id, "（結果なし）")
            + "\n\n[マスターからの追加指示] " + "\n".join(steer)
        )
    return results


async def _next_event(susp: "bridge.Suspension"):
    """保留中の次のイベントを待つ。終了系なら _suspended から外す"""
    ev = await susp.events.get()
    if ev[0] != "calls" and bridge._suspended.get(susp.session_key) is susp:
        bridge._suspended.pop(susp.session_key, None)
    return ev


async def _generate_reply(session_key, user_text, images, via_hermes, actor,
                          profile=None):
    """従来経路の1往復（承認コード横取り → generate）"""
    replaced = await asyncio.to_thread(
        _maybe_approval_decision, user_text, actor
    )
    if replaced is not None:
        user_text = replaced
    return await generate(
        session_key, user_text or "（空の入力）", actor=actor,
        images=images or None, profile=profile,
        source="hermes" if via_hermes else "api",
    )


async def _hermes_event(body, messages, session_key, user_text, images, actor):
    """via_hermes リクエストの処理本体。(kind, payload) を返す。
    kind: "calls"（tool_calls 応答）/ "done"（最終応答）/ "error" """
    hermes_tools = body.get("tools")
    bridgeable = (
        BODY_BRIDGE and isinstance(hermes_tools, list) and len(hermes_tools) > 0
    )
    ident = await asyncio.to_thread(
        hermes_rt.session_identity, session_key
    )
    profile = (ident or {}).get("profile_name")
    if not bridgeable:
        return ("done", await _generate_reply(
            session_key, user_text, images, True, actor, profile=profile
        ))
    async with _bridge_locks[session_key]:
        susp = bridge._suspended.get(session_key)
        if susp is not None:
            # 継続判定: tool_call_id 照合（失敗時は位置フォールバック）
            results = _continuation_results(susp, messages)
            if results is not None:
                for cid_, fut in susp.pending.items():
                    if not fut.done():
                        fut.set_result(results.get(cid_, "（結果なし）"))
                return await _next_event(susp)
            # 継続でない → 残った保留を閉じる（Hermes 側で中断等）
            susp.abort()
            if susp.task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(susp.task), 30)
                except Exception:
                    pass
            if bridge._suspended.get(session_key) is susp:
                bridge._suspended.pop(session_key, None)
        if messages and messages[-1].get("role") == "tool":
            # 途切れた継続（companion 再起動等）: 記録せず謝って返す
            return ("done", dict(_ORPHANED_CONTINUATION))
        # 新規ターン: ここで初めて承認コード横取りを適用する
        # （継続リクエストには適用しない）
        replaced = await asyncio.to_thread(
            _maybe_approval_decision, user_text, actor
        )
        if replaced is not None:
            user_text = replaced
        patterns, owner_patterns, dm_patterns = \
            profile_policy.bridge_patterns(profile)
        if is_owner_actor(actor):
            # ファイル読み系は owner 会話にだけ（共有chの非owner発言で
            # ローカルファイルを読み出されないようにする）
            patterns = patterns + owner_patterns
        if _is_dm_identity(ident):
            # DM 会話だけ書込系を見せる（実行承認は Hermes 側が担う）
            patterns = patterns + dm_patterns
        schemas, names = bridge.build_schemas(hermes_tools, patterns)
        if not schemas:
            return ("done", await generate(
                session_key, user_text or "（空の入力）", actor=actor,
                images=images or None, source="hermes", profile=profile,
            ))
        susp = bridge.Suspension(session_key)
        bb = bridge.BodyBridge(schemas=schemas, names=names, emit=susp.emit)
        susp.task = asyncio.create_task(
            generate(
                session_key, user_text or "（空の入力）", actor=actor,
                images=images or None, source="hermes", bridge=bb,
                profile=profile,
            )
        )
        susp.task.add_done_callback(susp.on_task_done)
        bridge._suspended[session_key] = susp
        return await _next_event(susp)


def _replay_key(session_key, messages, actor=None) -> str:
    """同一リクエスト判定: session + actor + メッセージ数 + 末尾2件の内容"""
    tail = messages[-2:] if isinstance(messages, list) else []
    h = hashlib.sha256()
    h.update(str(session_key).encode("utf-8"))
    h.update(b"\0")
    h.update(str(actor).encode("utf-8"))
    h.update(b"\0")
    h.update(str(len(messages)).encode("ascii"))
    h.update(b"\0")
    h.update(
        json.dumps(tail, ensure_ascii=False, sort_keys=True, default=str)
        .encode("utf-8")
    )
    return h.hexdigest()


async def _hermes_request(body, messages, session_key, user_text, images,
                          actor):
    """再送の冪等化。処理中は同じタスクの結果を待ち、完了後120秒は
    描画前のイベント（"calls"/"done"）を返す"""
    key = _replay_key(session_key, messages, actor)
    now = time.monotonic()
    for k, ent in list(_replay.items()):
        # 受け手が切断して完了済みタスクのまま残った分も掃く
        stale_task = ent.get("task") is not None and ent["task"].done()
        if stale_task or (ent.get("task") is None and ent["expires"] < now):
            _replay.pop(k, None)
    ent = _replay.get(key)
    if ent is not None:
        if ent.get("task") is not None:
            return await asyncio.shield(ent["task"])
        return ent["event"]
    task = asyncio.create_task(
        _hermes_event(body, messages, session_key, user_text, images, actor)
    )
    _replay[key] = {"task": task}
    try:
        ev = await asyncio.shield(task)
    except Exception:
        _replay.pop(key, None)
        raise
    _replay[key] = {"event": ev, "expires": time.monotonic() + _REPLAY_TTL}
    return ev


def _session_key(body, headers) -> tuple[str, bool]:
    """信頼済みクライアントのセッション識別。省略・異常値はdesktop互換に倒す。

    (session_key, via_hermes) を返す。Hermes gateway は自前の会話IDを
    session_affinity_header で送るので、Discord/CLI等の入口ごとの会話が
    companion 側でも別セッションとして保たれる。"""
    h = headers.get(SESSION_HEADER)
    if isinstance(h, str) and h.strip():
        return h.strip()[:64], True
    s = body.get("session")
    if not isinstance(s, str) or not s.strip():
        return "desktop", False
    return s.strip()[:64], False


def _chunk_payload(cid, content=None, role=None, finish=None, extra=None,
                   model=None, tool_calls=None):
    delta = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model or LLM_MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if extra:
        payload.update(extra)
    return payload


def _render_done(cid: str, stream: bool, result: dict, actor=None):
    """最終応答の描画（従来どおり companion フィールドを付ける）。

    pending_approvals（承認待ちの内容要約）は owner 系応答にだけ載せる。
    非owner話者への応答に承認待ち内容を混ぜない"""
    from fastapi.responses import StreamingResponse

    reply = result["reply"]
    used_model = result.get("model") or LLM_MODEL
    pending = (
        [
            {**p, "kind": "delegate"}
            for p in delegate.pending_approvals()
        ]
        + bodyruns.pending_approvals()
        if is_owner_actor(actor)
        else []
    )
    extra = {
        "companion": {
            "inner": result.get("inner") or "",
            "tools": result.get("tools") or [],
            # ローカルUI が承認ボタンを出すための未承認依頼
            # （delegate と身体承認の混成。身体側のコードは載せない）
            "pending_approvals": pending,
        }
    }
    if stream:
        async def gen():
            head = json.dumps(
                _chunk_payload(cid, role="assistant", model=used_model),
                ensure_ascii=False,
            )
            yield f"data: {head}\n\n"
            step = 24
            for i in range(0, len(reply), step):
                payload = json.dumps(
                    _chunk_payload(cid, content=reply[i : i + step],
                                   model=used_model),
                    ensure_ascii=False,
                )
                yield f"data: {payload}\n\n"
            yield "data: " + json.dumps(
                _chunk_payload(cid, finish="stop", extra=extra,
                               model=used_model),
                ensure_ascii=False,
            ) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")
    return {
        "id": cid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": used_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        **extra,
    }


def _render_calls(cid: str, stream: bool, calls: list):
    """橋渡し中間応答: body tool の tool_calls だけを返す。
    companion 独自フィールド・途中ラウンドの content は出さない"""
    from fastapi.responses import StreamingResponse

    tool_calls = [
        {
            "id": c["id"],
            "type": "function",
            "function": {"name": c["name"], "arguments": c["arguments"]},
        }
        for c in calls
    ]
    if stream:
        async def gen():
            head = json.dumps(
                _chunk_payload(
                    cid, role="assistant",
                    tool_calls=[
                        {"index": i, **tc} for i, tc in enumerate(tool_calls)
                    ],
                ),
                ensure_ascii=False,
            )
            yield f"data: {head}\n\n"
            yield "data: " + json.dumps(
                _chunk_payload(cid, finish="tool_calls"),
                ensure_ascii=False,
            ) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")
    return {
        "id": cid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": LLM_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


_APPROVAL_EVENT_KINDS = {
    "pre_approval_request": "requested",
    "request": "requested",
    "post_approval_response": "answered",
    "response": "answered",
}


def create_app():
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    @asynccontextmanager
    async def _lifespan(app):
        # MCP session manager は run() 中だけリクエストを処理できる。
        # /mcp を載せたときだけ lifespan で起動する
        mgr = getattr(app.state, "mcp_session_manager", None)
        if mgr is None:
            yield
            return
        async with mgr.run():
            yield

    app = FastAPI(lifespan=_lifespan)

    @app.middleware("http")
    async def companion_auth(request: Request, call_next):
        """入口鍵のゲート。COMPANION_API_AUTH = off|observe|enforce。

        /healthz は常に素通し。/ と /ui（ローカル小窓ページ）は器だけで
        データを持たないので素通しし、中身のAPI呼び出しは個別に鍵を要求する。
        /mcp 配下は専用鍵で照合し、鍵未設定なら面ごと無い扱いで404
        （素通ししない）。それ以外の鍵未設定（unconfigured）はどのモードでも
        素通しする。判定結果は request.state.auth に残す"""
        global _unconfigured_logged
        path = request.url.path
        if path in ("/healthz", "/", "/ui"):
            return await call_next(request)
        is_mcp = path == "/mcp" or path.startswith("/mcp/")
        verdict = (
            _mcp_verdict(request.headers)
            if is_mcp
            else _auth_verdict(request.headers)
        )
        request.state.auth = verdict
        if is_mcp and verdict == "unconfigured":
            return JSONResponse(
                {"error": {"message": "not found", "type": "not_found"}},
                status_code=404,
            )
        mode = (COMPANION_API_AUTH or "observe").strip().lower()
        if verdict == "unconfigured":
            if not _unconfigured_logged:
                _unconfigured_logged = True
                log.info(
                    ":8788 入口鍵が未設定（COMPANION_API_AUTH=%s）。認証は素通し",
                    mode,
                )
        elif verdict == "ok":
            log.debug(":8788 auth ok: %s %s", request.method, request.url.path)
        elif mode == "enforce":
            return JSONResponse(
                {"error": {"message": "invalid or missing API key",
                           "type": "unauthorized"}},
                status_code=401,
            )
        elif mode == "observe":
            now = time.monotonic()
            if now - _auth_warned.get(verdict, -_AUTH_WARN_INTERVAL) >= _AUTH_WARN_INTERVAL:
                _auth_warned[verdict] = now
                log.warning(
                    ":8788 auth %s: %s %s",
                    verdict, request.method, request.url.path,
                )
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    async def root():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/ui")

    @app.get("/ui", include_in_schema=False)
    async def ui_page():
        """ローカル小窓ページ（旧 PySide6 窓の軽量置換）。器は無認証、
        中身の /v1 API はブラウザが鍵付きで直接叩く"""
        from fastapi.responses import HTMLResponse

        return HTMLResponse(webui.page())

    @app.get("/v1/models")
    async def models():
        return {
            "object": "list",
            "data": [
                {"id": "companion", "object": "model", "created": 0, "owned_by": "local"}
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        try:
            body = await req.json()
        except Exception:
            return JSONResponse(
                {"error": {"message": "invalid JSON body", "type": "bad_request"}},
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": {"message": "body must be a JSON object", "type": "bad_request"}},
                status_code=400,
            )
        messages = body.get("messages") or []
        user_text, images = _extract_user_input(messages)
        cid = f"chatcmpl-{uuid.uuid4().hex[:20]}"
        session_key, via_hermes = _session_key(body, req.headers)
        # state.db 参照を含むのでループを塞がないよう thread へ
        actor = await asyncio.to_thread(_actor_for_api, req)
        if not is_owner_actor(actor) and _APPROVAL_RE.search(user_text or ""):
            # 非ownerの承認コード発言: コードを消費させない（実在するかも
            # 教えない一律の断り）。普通の発言は素通しで会話だけは返す
            return _render_done(cid, bool(body.get("stream")), {
                "reply": "その承認コードはマスター本人しか使えないよ",
                "inner": "", "tools": [], "model": LLM_MODEL,
            }, actor)
        try:
            if via_hermes:
                # Hermes 経由: 形式橋の継続/保留と再送の冪等化を通す
                ev = await _hermes_request(
                    body, messages, session_key, user_text, images, actor
                )
            else:
                # 橋対象外: 従来どおり（承認コード横取り → generate）
                ev = ("done", await _generate_reply(
                    session_key, user_text, images, False, actor
                ))
        except Exception as e:
            log.exception("server generate failed")
            return JSONResponse(
                {"error": {"message": str(e), "type": "internal_error"}},
                status_code=500,
            )
        if ev[0] == "error":
            log.error("bridge turn failed: %s", ev[1])
            return JSONResponse(
                {"error": {"message": str(ev[1]), "type": "internal_error"}},
                status_code=500,
            )
        if ev[0] == "calls":
            return _render_calls(cid, bool(body.get("stream")), ev[1])
        return _render_done(cid, bool(body.get("stream")), ev[1], actor)

    @app.get("/v1/delegate/pending")
    async def delegate_pending():
        """ローカルUIが承認待ちを確認する口。127.0.0.1限定のAPI上にのみ公開"""
        return {"pending": delegate.pending_approvals()}

    @app.post("/v1/delegate/{job_id}/approve")
    async def delegate_approve_ep(job_id: int):
        """ローカルUI承認ボタン。LLMのtool引数からは届かない別経路"""
        result = await asyncio.to_thread(delegate.delegate_approve, job_id, "desktop")
        return {"result": result}

    @app.post("/v1/delegate/{job_id}/deny")
    async def delegate_deny_ep(job_id: int):
        """ローカルUI却下ボタン。approve と同じ信頼面の対称経路"""
        result = await asyncio.to_thread(delegate.delegate_deny, job_id, "desktop")
        return {"result": result}

    @app.get("/v1/body/approvals")
    async def body_approvals_pending():
        """身体側の承認待ち一覧（承認コードは含めない）"""
        return {"pending": await asyncio.to_thread(bodyruns.pending_approvals)}

    @app.post("/v1/body/approvals/{ap_id}")
    async def body_approval_decide(ap_id: int, req: Request):
        """ローカルUI の承認ボタンからの決裁。decision=approve|deny"""
        try:
            data = await req.json()
        except Exception:
            data = None
        decision = (data or {}).get("decision") if isinstance(data, dict) else None
        if decision not in ("approve", "deny"):
            return JSONResponse(
                {"error": {"message": "decision must be approve|deny",
                           "type": "bad_request"}},
                status_code=400,
            )
        ok, msg = await asyncio.to_thread(
            bodyruns.decide, approval_id=ap_id,
            approve=(decision == "approve"), decided_by="desktop",
        )
        return {"ok": ok, "result": msg}

    @app.post("/v1/bridge/approval_event")
    async def bridge_approval_event(req: Request):
        """R5a: Hermes 承認 hook の監査写し受け口。

        正本は Hermes 側の会話内承認（承認可否は Hermes が決める。ここに
        届く時点で進行中か決着済み）。Core は turn_tool_calls(origin=
        'body_approval') に写すだけで、body_run の Core 承認とは別物。
        """
        try:
            body = await req.json()
        except Exception:
            return JSONResponse(
                {"error": {"message": "invalid JSON body",
                           "type": "bad_request"}},
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": {"message": "body must be a JSON object",
                           "type": "bad_request"}},
                status_code=400,
            )
        event = str(body.get("event") or "").strip()
        kind = _APPROVAL_EVENT_KINDS.get(event)
        if kind is None:
            return JSONResponse(
                {"error": {"message": "event must be pre_approval_request"
                                      " or post_approval_response",
                           "type": "bad_request"}},
                status_code=400,
            )
        pattern_key = str(body.get("pattern_key") or "").strip()[:200]
        if not pattern_key:
            return JSONResponse(
                {"error": {"message": "pattern_key is required",
                           "type": "bad_request"}},
                status_code=400,
            )
        hermes_sk = str(body.get("session_key") or "").strip()[:256]
        session_id = str(body.get("session_id") or "").strip()[:128]
        tool_call_id = str(body.get("tool_call_id") or "").strip()[:256]
        turn_id = str(body.get("turn_id") or "").strip()[:128]
        ref = tool_call_id or (f"hturn:{turn_id}" if turn_id else "")
        status = ("requested" if kind == "requested"
                  else str(body.get("choice") or "answered").strip()[:64])
        args = {
            "event": event,
            "surface": str(body.get("surface") or "")[:64],
            "session_id": session_id,
            "session_key": hermes_sk,
            "turn_id": turn_id,
        }
        for k in ("pattern_keys", "coalesced"):
            if body.get(k) is not None:
                args[k] = body[k]
        if body.get("decided_by") is not None:
            args["decided_by"] = str(body["decided_by"])[:64]
        session_key = await asyncio.to_thread(
            hermes_rt.approval_affinity, session_id, hermes_sk)
        if not session_key:
            session_key = (f"hermes:{hermes_sk[:64]}"
                           if hermes_sk else "unknown")
        recorded = await asyncio.to_thread(
            db.record_body_approval_event,
            session_key=session_key, event=event,
            pattern_key=pattern_key, ref=ref, status=status,
            args=args,
            excerpt=redact.scrub_text(
                body.get("command_excerpt") or body.get("description") or ""),
        )
        return {"ok": True, "recorded": recorded}

    # /mcp（S3。身体→Core の問い合わせ面）: 専用鍵が解決できるときだけ載せる。
    # 鍵が無ければ面自体を作らないので未認証の露出は構造上あり得ない。
    # FastMCP の sub-app を mount("/mcp") すると /mcp→/mcp/ の 307 で
    # POST が落ちるため、ASGI handler を /mcp の Route に直接載せる
    if COMPANION_MCP and hermes_conf.mcp_key()[0]:
        from mcp.server.fastmcp.server import StreamableHTTPASGIApp

        from . import mcp_server
        mcp_srv = mcp_server.build_mcp()
        mcp_srv.streamable_http_app()  # session_manager を作る副作用
        app.state.mcp_session_manager = mcp_srv.session_manager
        app.add_route(
            "/mcp",
            StreamableHTTPASGIApp(mcp_srv.session_manager),
            methods=["GET", "POST", "DELETE"],
        )
        log.info("companion MCP mounted at /mcp")

    return app


async def serve():
    import uvicorn

    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=API_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    log.info("companion API listening on http://127.0.0.1:%d", API_PORT)
    await server.serve()
