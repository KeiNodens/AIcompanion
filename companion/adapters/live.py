"""adapters.live —— P8-B 記録 lane（live path への「記録のみ」配線）

engine が確定した往復を canonical Event Log へ複写する lane。
enqueue は非ブロッキング（queue 投入のみ）で、実処理は専用 worker
thread で行う（SQLite 接続は作成スレッドに紐付くため open_core も
worker 内で開く）。失敗は state.json の degraded 印＋通知で可視化し、
live 応答は止めない（呼び出し側へ例外を伝播しない）。

照会権威は mem0 のまま —— この lane は記録専用で応答経路に入らない。
キューは揮発なので、停止直前の in-flight 分だけ失われうるが、正本
turns 表からの再転記で回復可能（L-MIG の transcription 材料）。
"""

import logging
import queue
import threading
import time
from datetime import datetime, timezone

from .. import config, db
from ..kernel.writer import open_core
from .intake import Intake

log = logging.getLogger("companion")

ADAPTER_ID = "companion-live"
ADAPTER_SPEC = {
    "adapter_id": ADAPTER_ID,
    "adapter_version": "1",
    "coverage": {"classes": ["conversation"]},
    "transform_version": "norm-v1",
}

_QUEUE_MAX = 1000           # 追いつかない backlog のメモリ上限
_OPEN_RETRY_SEC = 300.0     # open 失敗（sanity 不通等）後の再試行間隔
_DRAIN_BATCH = 200


def _enabled() -> bool:
    """lane が動く条件: 明示フラグ OR 権威が dual_run/canonical。

    canonical 権威では lane が唯一の記録経路であり、dual_run では
    seal の前提になるため、権威旗が立っていればフラグ無しでも動かす。
    """
    if getattr(config, "CANONICAL_RECORD", False):
        return True
    import os
    auth = os.getenv("MEMORY_AUTHORITY")
    if not auth:
        try:
            auth = db.load_state().get("memory_authority") or "mem0"
        except Exception:
            auth = "mem0"
    return auth in ("dual_run", "canonical")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecordLane:
    """live 往復 → intake envelope の複写 lane（単一 worker・揮発キュー）。"""

    def __init__(self, path=None):
        self._path = path or (config.DATA_DIR / "core.db")
        self._q = queue.Queue(maxsize=_QUEUE_MAX)
        self._wlock = threading.Lock()
        self._worker = None
        self._open_blocked_until = 0.0
        self._writer = None
        self._intake = None
        self._supply = None
        self._admission = None
        self._extraction = None
        self._extractor_cache = None

    # -------------------------------------------------- live path 入口
    def submit_turn(self, turn_id, session_key, user_text, reply,
                    ts_iso=None, source=None) -> bool:
        """確定往復を記録キューへ。例外は飲み込む（live 応答を止めない）。"""
        if not _enabled() or turn_id is None:
            return False
        try:
            env = self._envelope(turn_id, session_key, user_text, reply,
                                 ts_iso, source)
            self._q.put_nowait(("submit", env))
        except queue.Full:
            self._degrade(f"record lane queue full ({_QUEUE_MAX})")
            return False
        except Exception as e:
            self._degrade(f"submit_turn: {e}")
            return False
        self._ensure_worker()
        return True

    def tick(self) -> None:
        """scheduler からの定期呼出: 滞留分の drain 予約＋worker 生存確認。

        正常系では submit 直後に drain まで済むので、ここで拾うのは
        中断・失敗で残った normalize_queue の滞留だけ。"""
        if not _enabled():
            return
        try:
            self._q.put_nowait(("drain", None))
        except queue.Full:
            pass
        self._ensure_worker()

    def shadow_tick(self, extractor=None, limit=100) -> None:
        """dual-run 用: worker 内で extraction→admission(shadow) を流す。

        extractor=None なら dualrun.verbatim_extractor（mem0 verbatim と
        同じ原料を canonical が持つ既定）。"""
        if not _enabled():
            return
        try:
            self._q.put_nowait(("shadow", (extractor, limit)))
        except queue.Full:
            pass
        self._ensure_worker()

    def status(self) -> dict:
        return {
            "enabled": _enabled(),
            "queued": self._q.qsize(),
            "worker_alive": bool(self._worker and self._worker.is_alive()),
            "open": self._writer is not None,
            "path": str(self._path),
        }

    # ------------------------------------------------- seal/共有面
    @property
    def writer(self):
        """開いた CoreWriter（唯一の canonical 書込み接続の共有窓口）。"""
        return self._writer

    def probe(self) -> bool:
        """canonical が開いて録れる状態か（seal 判定用）。"""
        return (self._writer is not None
                and self._worker is not None and self._worker.is_alive())

    def ensure_open(self, timeout=10.0) -> bool:
        """worker 起動＋open 完了を待つ（seal 起動ガードの入口）。"""
        if not _enabled():
            return False
        self._ensure_worker()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._writer is not None:
                return True
            if self._open_blocked_until > time.monotonic():
                return False
            time.sleep(0.05)
        return self._writer is not None

    def call(self, fn, timeout=30.0):
        """fn() を lane worker thread 内で同期実行して結果を返す。

        canonical の書込み接続は worker スレッド所有なので、engine 等の
        別スレッドからはこの口を通す（直接 self._writer._conn を触ると
        sqlite3.ProgrammingError になる）。worker 未起動/応答なしは
        RuntimeError を投げる（呼び出し側の既存エラー処理に委ねる）。
        """
        if not _enabled():
            raise RuntimeError("canonical lane disabled")
        self._ensure_worker()
        done = threading.Event()
        box = {}
        try:
            self._q.put_nowait(("call", (fn, done, box)))
        except queue.Full:
            raise RuntimeError("canonical lane queue full")
        if not done.wait(timeout):
            raise TimeoutError("canonical lane call timed out")
        if "err" in box:
            raise box["err"]
        return box.get("res")

    def supply(self):
        """canonical 供給面（lane の writer 上に構築・derived 接続済み）。

        worker thread 内で呼ぶこと（call 経由が正しい入口）。
        """
        if self._writer is None:
            return None
        if self._supply is None:
            from .derived import DerivedStore
            from .supply import Supply
            emb = None
            try:
                from .embedder import Fastembedder
                emb = Fastembedder()
            except Exception:
                log.exception("canonical embedder unavailable; "
                              "dense recall disabled")
            rk = None
            try:
                from .rerank import default_reranker
                rk = default_reranker()
            except Exception:
                log.exception("canonical reranker unavailable; "
                              "score-v1 order only")
            self._supply = Supply(
                self._writer, DerivedStore(self._writer), embedder=emb,
                reranker=rk)
        return self._supply

    def admission(self):
        if self._writer is None:
            return None
        if self._admission is None:
            from .admission import Admission
            self._admission = Admission(self._writer)
        return self._admission

    def extraction(self, extractor=None):
        if self._writer is None:
            return None
        if self._extraction is None:
            from .extraction import Extraction
            self._extraction = Extraction(
                self._writer, self.admission(), extractor=extractor)
        return self._extraction

    def wait_idle(self, timeout=10.0) -> bool:
        """テスト/運用補助: キュー処理完了まで待つ。間に合えば True。"""
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._q.unfinished_tasks

    def shutdown(self, timeout=3.0):
        with self._wlock:
            w = self._worker
        if w is None or not w.is_alive():
            return
        try:
            self._q.put_nowait(("stop", None))
        except queue.Full:
            pass
        w.join(timeout)

    # ------------------------------------------------------- envelope
    @staticmethod
    def _envelope(turn_id, session_key, user_text, reply, ts_iso, source):
        ts_iso = ts_iso or _now_iso()
        return {
            "envelope_id": f"turn:{turn_id}",
            "adapter_id": ADAPTER_ID,
            "format_version": "pcore.envelope.v1",
            "stream_id": str(session_key),
            "span": {"cursor_kind": "turn_id",
                     "start_cursor": turn_id, "end_cursor": turn_id},
            "occurred_range": {"start": ts_iso, "end": ts_iso},
            "payload_mode": "inline",
            "part_count": 2,
            "parts": [
                {"part_id": f"t{turn_id}:u", "class": "conversation",
                 "origin": "user", "body": user_text, "ts": ts_iso,
                 "turn_id": turn_id},
                {"part_id": f"t{turn_id}:a", "class": "conversation",
                 "origin": "assistant", "body": reply, "ts": ts_iso,
                 "turn_id": turn_id},
            ],
            "coverage": {
                "classes": ["conversation"],
                "session_hint": {"channel": source or "unknown",
                                 "session_id": str(session_key)},
            },
        }

    # --------------------------------------------------------- worker
    def _ensure_worker(self):
        with self._wlock:
            if self._worker is not None and self._worker.is_alive():
                return
            if time.monotonic() < self._open_blocked_until:
                return
            self._worker = threading.Thread(
                target=self._run, name="canonical-lane", daemon=True)
            self._worker.start()

    def _open(self):
        """core.db を worker thread 内で開く（sanity 含む fail-loud）。

        失敗時は degrade＋cooldown。呼び出し側は None 扱いで抜ける。"""
        try:
            w = open_core(self._path)
        except Exception as e:
            self._open_blocked_until = time.monotonic() + _OPEN_RETRY_SEC
            self._degrade(f"core.db open failed: {e}")
            return None
        self._writer = w
        ik = Intake(w)
        ik.register_adapter(dict(ADAPTER_SPEC))
        ik.register_normalizer(
            ADAPTER_ID, "pcore.envelope.v1", self._normalizer(ik))
        self._intake = ik
        return ik

    def _run(self):
        intake = self._open()
        if intake is None:
            return   # degrade 済み。cooldown 後の submit/tick で再試行
        while True:
            kind, payload = self._q.get()
            try:
                if kind == "stop":
                    return
                if kind == "submit":
                    intake.submit_envelope(payload)
                    intake.drain(_DRAIN_BATCH)
                elif kind == "drain":
                    intake.drain(_DRAIN_BATCH)
                elif kind == "shadow":
                    self._run_shadow(payload)
                elif kind == "call":
                    fn, done_ev, box = payload
                    try:
                        box["res"] = fn()
                    except Exception as e:
                        box["err"] = e
                    finally:
                        done_ev.set()
                self._recover()
            except Exception as e:
                self._degrade(str(e))
            finally:
                self._q.task_done()

    def _run_shadow(self, payload):
        """extraction→admission（dual_run では影、canonical では本経路）
        を lane 内で直列実行し、増分索引も追いかける。"""
        extractor, limit = payload or (None, 100)
        if extractor is None:
            # CANONICAL_EXTRACTOR=llm で本格抽出器（C-1）。クライアントを
            # 使い回すため lane にキャッシュする
            if self._extractor_cache is None:
                from .llm_extract import default_extractor
                self._extractor_cache = default_extractor(self._writer)
            extractor = self._extractor_cache
        ext = self.extraction(extractor)
        if ext is not None:
            ext.run(limit=limit)
        try:
            from .derived import DerivedStore
            emb = None
            try:
                from .embedder import Fastembedder
                emb = Fastembedder()
            except Exception:
                pass
            DerivedStore(self._writer).index_pending(embedder=emb)
        except Exception:
            log.exception("derived index_pending failed")

    @staticmethod
    def _normalizer(intake):
        """既定 normalizer＋correlation.turn_id（events.turn_id 列の材料）。"""
        def _norm(spec, erow, part):
            evs = intake._default_normalizer(spec, erow, part)
            for ev in evs or []:
                corr = dict(ev.get("correlation") or {})
                corr["turn_id"] = part.get("turn_id")
                ev["correlation"] = corr
            return evs
        return _norm

    # -------------------------------------------------- degraded 印
    def _degrade(self, err: str):
        log.error("canonical record lane degraded: %s", err)
        at = _now_iso()
        try:
            db.update_state(mutator=lambda s: s.__setitem__(
                "canonical_lane",
                {"degraded": True, "error": str(err)[:300], "at": at}))
        except Exception:
            log.exception("degraded flag write failed")
        try:
            cid = db.resolve_notify_channel()
            if cid:
                db.enqueue_notification(
                    "canonical_lane:" + at[:10], cid,
                    "⚠️ canonical 記録で障害が出てるよ"
                    "（会話は止めてない）: " + str(err)[:150])
        except Exception:
            log.exception("degraded notify failed")

    def _recover(self):
        try:
            st = db.load_state().get("canonical_lane") or {}
        except Exception:
            return
        if st.get("degraded"):
            log.info("canonical record lane recovered")
            try:
                db.update_state(mutator=lambda s: s.__setitem__(
                    "canonical_lane",
                    {"degraded": False, "recovered_at": _now_iso()}))
            except Exception:
                log.exception("recover flag write failed")


# モジュール単位の共有 lane（engine から使う）
lane = RecordLane()
