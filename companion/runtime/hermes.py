"""Hermes body の REST 制御面クライアント（runtime-hermes.md Step 4・項目F）。

Core-as-model 構成で companion が body 側機能を呼ぶための最下層。
Hermes gateway の api_server（既定 :8642・Bearer 認証）を叩く。
Hermes 固有名（URL・API・モデル別名）を知るのはこのモジュールだけ。

使う面（全て install 済みソース・docs で確認済み）:
- GET  /health ・ GET /v1/capabilities    — 生存確認・機能一覧
- POST /v1/runs ・ GET /v1/runs/{id}      — 非同期 agent run。
  model に model_routes の別名を渡すと裏方モデルに pin される。
  session_id を同じ値で続けると body 側 transcript を引き継ぐ（操縦用）
- GET  /v1/runs/{id}/events               — run の SSE イベント流
  （Last-Event-ID で途中再開できる）
- POST /v1/runs/{id}/approval             — 身体側承認への応答
  （choice=once/deny）。承認待ちでなければ 409
- POST /v1/runs/{id}/steer ・ /stop       — 走行中 run への追加指示・停止
- /api/jobs CRUD・pause/resume/run        — body 側 cron の管理

run の制御系は同期実装（ツールは dispatch_tool から to_thread 経由）、
SSE だけはイベントを垂れ流すため async 実装にする。失敗は
BodyUnreachableError/BodyAPIError を投げ、彼女向け文言への整形は
呼び出し側（tools/body.py・bodyruns.py）の仕事。
"""
import hashlib
import json
import logging
import sqlite3

import httpx

from .. import hermes_conf
from ..config import BODY_RUN_MODEL, HERMES_API_BASE, HERMES_HOME

log = logging.getLogger("companion")

# 例外型は runtime/errors.py が正本（codex backend も同じ型を投げる）。
# 既存の import 経路（body.hermes.BodyAPIError 等）のため alias を維持する
from .errors import BodyAPIError, BodyUnreachableError  # noqa: F401,E402


def _client() -> httpx.Client:
    # 鍵は呼ぶたびに hermes_conf が解決する（env → Hermes config → Hermes .env）
    key, _src = hermes_conf.api_server_key()
    if not key:
        raise BodyUnreachableError(
            "Hermes API 鍵が見つかりません（.env の HERMES_API_KEY も "
            "Hermes config の platforms.api_server.extra.key も空）"
        )
    return httpx.Client(
        base_url=HERMES_API_BASE,
        headers={"Authorization": f"Bearer {key}"},
        timeout=15,
    )


def _req(method: str, path: str, **kw):
    try:
        with _client() as c:
            res = c.request(method, path, **kw)
    except BodyUnreachableError:
        raise
    except httpx.HTTPError as e:
        raise BodyUnreachableError(f"body API に繋がりません: {e}") from e
    if res.status_code >= 400:
        raise BodyAPIError(
            f"{method} {path} -> {res.status_code}: {res.text[:300]}",
            status_code=res.status_code,
        )
    try:
        return res.json()
    except ValueError:
        return {"raw": res.text}


def health() -> dict:
    """無認証の生存確認。api_server が上がっていれば ok"""
    try:
        with httpx.Client(base_url=HERMES_API_BASE, timeout=8) as c:
            res = c.get("/health")
    except httpx.HTTPError as e:
        raise BodyUnreachableError(f"body API に繋がりません: {e}") from e
    if res.status_code >= 400:
        raise BodyAPIError(f"/health -> {res.status_code}",
                           status_code=res.status_code)
    return res.json()


def capabilities() -> dict:
    """GET /v1/capabilities — 使える機能・エンドポイントの一覧"""
    return _req("GET", "/v1/capabilities")


def skills_list() -> list:
    """GET /v1/skills — body 側が現在認識している技能一覧
    （[{name, description, category}]。external_dirs 経由の写しも含まれる）"""
    return _req("GET", "/v1/skills").get("data") or []


def create_run(task: str, session_id: str | None = None,
               instructions: str | None = None) -> dict:
    """POST /v1/runs — body 側 agent に依頼を投げる。run_id が返る"""
    payload: dict = {"input": task, "model": BODY_RUN_MODEL}
    if session_id:
        payload["session_id"] = session_id
    if instructions:
        payload["instructions"] = instructions
    return _req("POST", "/v1/runs", json=payload)


def run_status(run_id: str) -> dict:
    """GET /v1/runs/{id} — 状態・出力・session_id 等の現在値。
    承認待ちのときは status='waiting_for_approval' と
    approval（approval.request イベントと同じペイロード）が入る"""
    return _req("GET", f"/v1/runs/{run_id}")


def run_approval(run_id: str, choice: str,
                 request_id: str | None = None) -> dict:
    """POST /v1/runs/{id}/approval — 身体側の承認要求への応答。
    choice は "once"（この操作だけ許可）か "deny"。承認待ちでない・
    期限切れなら 409 の BodyAPIError"""
    payload: dict = {"choice": choice}
    if request_id:
        payload["request_id"] = request_id
    return _req("POST", f"/v1/runs/{run_id}/approval", json=payload)


def run_steer(run_id: str, text: str) -> dict:
    """POST /v1/runs/{id}/steer — 走行中の run に追加指示を注入する。
    running でなければ 409"""
    return _req("POST", f"/v1/runs/{run_id}/steer", json={"input": text})


def run_stop(run_id: str) -> dict:
    """POST /v1/runs/{id}/stop — 走行中の run を停止する。無効なら 409"""
    return _req("POST", f"/v1/runs/{run_id}/stop")


def _parse_sse(lines):
    """SSE の行ストリームからイベントを取り出す純関数。

    ':' 始まりの行はコメント（keepalive 等）として無視し、id:/data:
    だけを拾う。data: は複数行を改行で結合して JSON として読む。
    seq は payload の "seq" を優先し、無ければ id: 行の数値を使う。
    (seq, payload) を yield する。
    """
    cur_id = None
    data_lines: list[str] = []

    def _emit():
        nonlocal cur_id, data_lines
        if not data_lines:
            cur_id = None
            return None
        raw = "\n".join(data_lines)
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw}
        seq = payload.get("seq") if isinstance(payload, dict) else None
        if seq is None and cur_id is not None:
            try:
                seq = int(cur_id)
            except ValueError:
                seq = None
        cur_id, data_lines = None, []
        return (seq, payload)

    for line in lines:
        line = line.rstrip("\r\n")
        if not line.strip():
            ev = _emit()
            if ev is not None:
                yield ev
            continue
        if line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if not sep:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            cur_id = value.strip()
        elif field == "data":
            data_lines.append(value)
        # event: 等の他フィールドは payload 内の "event" 名を使うので拾わない
    ev = _emit()
    if ev is not None:
        yield ev


async def run_events(run_id: str, last_seq: int = -1):
    """GET /v1/runs/{id}/events を SSE で読み、(seq, payload) を yield する。

    last_seq>=0 なら Last-Event-ID ヘッダでそこから再開する。
    Hermes は10秒ごとに ': keepalive' コメントを送るので read timeout は
    120s。404 は BodyAPIError(status_code=404)、接続不能は
    BodyUnreachableError を投げる。
    """
    key, _src = hermes_conf.api_server_key()
    if not key:
        raise BodyUnreachableError(
            "Hermes API 鍵が見つかりません（.env の HERMES_API_KEY も "
            "Hermes config の platforms.api_server.extra.key も空）"
        )
    headers = {}
    if last_seq >= 0:
        headers["Last-Event-ID"] = str(last_seq)
    timeout = httpx.Timeout(120.0, connect=15.0)
    try:
        async with httpx.AsyncClient(
            base_url=HERMES_API_BASE,
            headers={"Authorization": f"Bearer {key}", **headers},
            timeout=timeout,
        ) as c:
            async with c.stream(
                "GET", f"/v1/runs/{run_id}/events"
            ) as res:
                if res.status_code >= 400:
                    raise BodyAPIError(
                        f"GET /v1/runs/{run_id}/events -> {res.status_code}",
                        status_code=res.status_code,
                    )
                block: list[str] = []
                async for line in res.aiter_lines():
                    block.append(line)
                    if not line.strip():
                        for item in _parse_sse(block):
                            yield item
                        block.clear()
                # ストリーム末尾に空行で閉じられなかったイベントを拾う
                for item in _parse_sse(block):
                    yield item
    except BodyAPIError:
        raise
    except httpx.HTTPError as e:
        raise BodyUnreachableError(f"body events に繋がりません: {e}") from e


# ---------- body 側 cron（/api/jobs） ----------

def jobs_list() -> list:
    return _req("GET", "/api/jobs").get("jobs") or []


def job_create(name: str, schedule: str, prompt: str,
               deliver: str = "local", repeat: int | None = None) -> dict:
    payload: dict = {
        "name": name, "schedule": schedule, "prompt": prompt, "deliver": deliver,
    }
    if repeat is not None:
        payload["repeat"] = repeat
    return _req("POST", "/api/jobs", json=payload).get("job") or {}


def job_delete(job_id: str) -> bool:
    return bool(_req("DELETE", f"/api/jobs/{job_id}").get("ok"))


def _job_action(job_id: str, action: str) -> dict:
    return _req("POST", f"/api/jobs/{job_id}/{action}").get("job") or {}


def job_pause(job_id: str) -> dict:
    return _job_action(job_id, "pause")


def job_resume(job_id: str) -> dict:
    return _job_action(job_id, "resume")


def job_run_now(job_id: str) -> dict:
    return _job_action(job_id, "run")


# ---------- 発言者解決（x-companion-session → Hermes 会話の user/source） ----------

def _state_db() -> sqlite3.Connection | None:
    """HERMES_HOME/state.db を read-only で開く。無い・開けないなら None"""
    path = HERMES_HOME / "state.db"
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _gwk_peers(conn) -> dict:
    """declared scope（gwk_<sha256[:24]>）→ {source, session_key} の逆引き表。

    Hermes は会話に x-companion-session として
    sha256(f"{source}|{session_key}|{generation}")[:24] を載せる
    （agent/prompt_cache_scope.py の declared_conversation_scope）。
    generation は conversation_generations の現行値、無ければ ""。
    """
    generations: dict[tuple[str, str], str] = {}
    try:
        for r in conn.execute(
            "SELECT source, session_key, generation"
            " FROM conversation_generations"
        ):
            try:
                gen = int(r["generation"] or 0)
            except (TypeError, ValueError):
                gen = 0
            if r["session_key"]:
                key = (str(r["source"] or ""), str(r["session_key"]))
                generations[key] = str(gen) if gen > 0 else ""
    except sqlite3.Error:
        pass  # 古い schema にテーブルが無いだけ
    peers: set[tuple[str, str]] = set(generations)
    try:
        for r in conn.execute(
            "SELECT DISTINCT source, session_key FROM sessions"
            " WHERE session_key IS NOT NULL AND session_key != ''"
        ):
            peers.add((str(r["source"] or ""), str(r["session_key"])))
    except sqlite3.Error:
        pass
    out = {}
    for source, key in peers:
        gen = generations.get((source, key), "")
        digest = hashlib.sha256(
            f"{source}|{key}|{gen}".encode("utf-8")
        ).hexdigest()[:24]
        out.setdefault(digest, {"source": source, "session_key": key})
    return out


def approval_affinity(session_id: str, session_key: str) -> str | None:
    """承認 hook の (session_id, session_key) から companion が見る
    セッション識別（x-companion-session の値）を逆算する。

    gwk_<sha256[:24]> は f"{source}|{session_key}|{generation}" のハッシュ
    （agent/prompt_cache_scope.py declared_conversation_scope と同じ式）。
    session_key が会話ルートの宣言キー、session_id は物理 sessions.id。
    解決できないときは物理 id、それも無ければ None。監査写しの関連付け
    用なので、確からしくない値を作るより None/物理id に倒す。
    """
    session_id = (session_id or "").strip()
    session_key = (session_key or "").strip()
    conn = _state_db()
    if conn is None:
        return session_id or None
    try:
        source = ""
        if session_id:
            row = conn.execute(
                "SELECT source, session_key FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if row is not None:
                source = str(row["source"] or "")
                if not session_key:
                    session_key = str(row["session_key"] or "")
        if not session_key:
            return session_id or None
        # 宣言キーを持つ (source, generation) 候補。generation 行が無い
        # 会話（旧 schema・api_server 由来）は世代なしの "" で digest する
        cands: list[tuple[str, str]] = []
        try:
            for r in conn.execute(
                "SELECT source, generation FROM conversation_generations"
                " WHERE session_key=?",
                (session_key,),
            ):
                try:
                    gen = int(r["generation"] or 0)
                except (TypeError, ValueError):
                    gen = 0
                cands.append((str(r["source"] or ""),
                              str(gen) if gen > 0 else ""))
        except sqlite3.Error:
            pass
        if not cands:
            try:
                for r in conn.execute(
                    "SELECT DISTINCT source FROM sessions WHERE session_key=?",
                    (session_key,),
                ):
                    cands.append((str(r["source"] or ""), ""))
            except sqlite3.Error:
                pass
        if source:
            cands = [c for c in cands if c[0] == source] or cands
        if not cands:
            return session_id or None
        src, gen = cands[0]
        digest = hashlib.sha256(
            f"{src}|{session_key}|{gen}".encode("utf-8")
        ).hexdigest()[:24]
        return f"gwk_{digest}"
    except sqlite3.Error:
        log.warning("Hermes state.db の承認 affinity 解決に失敗", exc_info=True)
        return session_id or None
    finally:
        conn.close()


def _session_columns(conn) -> set:
    """sessions テーブルの列名集合。profile_name 等の無い schema でも
    照会を壊さないための判別用"""
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    except sqlite3.Error:
        return set()


def session_identity(affinity: str) -> dict | None:
    """x-companion-session の値が指す Hermes 会話の
    {source, user_id, session_id, session_key, profile_name}。
    見つからなければ None。

    affinity の値は Hermes の resolve_affinity_key() が付けるもので、
    - "gwk_<digest>": host宣言の会話スコープ → state.db で逆引き
    - それ以外: 会話ルートの sessions.id → 直接引く

    user_id はその会話行に最後に刻まれた発言者（DM では固定）。
    profile_name は profile 別ポリシー（profile_policy.py）の判定に使う
    （列の無い schema では None）。
    state.db は Hermes プロセスと共有するが read-only（mode=ro）で
    開くだけなので書き込み側を妨げない。"""
    affinity = (affinity or "").strip()
    if not affinity:
        return None
    conn = _state_db()
    if conn is None:
        return None
    try:
        cols = _session_columns(conn)
        sel = ("id, source, user_id, session_key"
               + (", profile_name" if "profile_name" in cols else ""))

        def _ident(row, source, session_key):
            return {
                "source": source,
                "user_id": row["user_id"],
                "session_id": row["id"],
                "session_key": session_key,
                "profile_name": (
                    row["profile_name"] if "profile_name" in cols else None
                ),
            }

        if affinity.startswith("gwk_"):
            peer = _gwk_peers(conn).get(affinity[4:])
            if peer is None:
                return None
            row = conn.execute(
                f"SELECT {sel} FROM sessions"
                " WHERE source=? AND session_key=?"
                " ORDER BY started_at DESC LIMIT 1",
                (peer["source"], peer["session_key"]),
            ).fetchone()
            if row is not None:
                return _ident(row, row["source"], row["session_key"])
            # 会話行が掃除済みでも peer の存在は確からしい（発言者は不明）
            return {"source": peer["source"], "user_id": None,
                    "session_id": None, "session_key": peer["session_key"],
                    "profile_name": None}
        row = conn.execute(
            f"SELECT {sel} FROM sessions WHERE id=?",
            (affinity,),
        ).fetchone()
        if row is None:
            return None
        return _ident(row, row["source"], row["session_key"])
    except sqlite3.Error:
        log.warning("Hermes state.db の actor 解決に失敗", exc_info=True)
        return None
    finally:
        conn.close()
