"""adapters.cutover —— L-MIG Phase 2 の cutover 実行（権威移動の刻印）。

G1〜G5 合格後に一度だけ実行される。やること:

- canonical に `authority_transition` イベントを刻む（監査痕は Event Log が担う）
- state.json の memory_authority を "canonical" に、猶予期限
  mem0_grace_until を刻む（memory.py がこの旗を読んで経路を切替える）
- 旧系凍結は「読み取り専用化＋旗」: qdrant/history.db を filesystem 上
  read-only にし、state.json に legacy_frozen_at を刻む。削除はしない

rollback は `rollback()` が旗を "mem0" に戻すだけ。canonical 側の刻印は
巻き戻さない（append-only の約束 —— 戻った事実は transition event と
旗の往復で分かる）。
"""

from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class Cutover:
    """cutover / 凍結 / rollback の実行面（全て writer・state 更新を注入で受ける）。"""

    def __init__(self, writer, *, update_state=None, load_state=None):
        self._w = writer
        # db.update_state / db.load_state を注入（adapter は db に直接依存しない）
        self._set = update_state or _default_update_state
        self._get = load_state or _default_load_state

    # -------------------------------------------------- authority flag
    def authority(self) -> str:
        return (self._get() or {}).get("memory_authority") or "mem0"

    def set_authority(self, mode: str, **extra):
        patch = {"memory_authority": mode}
        patch.update(extra)
        self._set(patch)

    # -------------------------------------------------- cutover
    def perform(self, *, from_mode: str, run_id: str | None = None,
                grace_days: int = 14, now: datetime | None = None) -> dict:
        """権威を canonical に移す。

        - authority_transition イベント刻印（from→to・run_id・刻印時刻）
        - state.json: memory_authority="canonical"、cutover_at、
          mem0_grace_until=now+grace_days（猶予の逆配送窓）
        """
        if self.authority() == "canonical":
            return {"ok": True, "idempotent": True,
                    "authority": "canonical"}
        now = now or _now()
        grace_until = _iso(now + timedelta(days=grace_days))
        self._w.append_event(
            event_type="authority_transition",
            occurred_at=_iso(now),
            actor="system",
            payload={
                "from": from_mode, "to": "canonical",
                "run_id": run_id,
                "grace_until": grace_until,
            },
            source_meta={"origin": "migration", "lane": "cutover"},
        )
        self.set_authority(
            "canonical",
            cutover_at=_iso(now), cutover_run_id=run_id,
            mem0_grace_until=grace_until, legacy_frozen_at=_iso(now),
        )
        return {"ok": True, "authority": "canonical",
                "grace_until": grace_until}

    # -------------------------------------------------- freeze（旧系読み取り専用化）
    def freeze_legacy(self, *, data_dir) -> dict:
        """qdrant/・history.db を filesystem read-only にする（削除しない）。

        返り値は凍結したパスの一覧。既に read-only のファイルはスキップ。
        """
        frozen = []
        targets = [os.path.join(str(data_dir), "qdrant"),
                   os.path.join(str(data_dir), "history.db")]
        for t in targets:
            if not os.path.exists(t):
                continue
            if os.path.isdir(t):
                for root, dirs, files in os.walk(t):
                    for d in dirs:
                        _ro(os.path.join(root, d))
                    for f in files:
                        p = os.path.join(root, f)
                        _ro(p)
                        frozen.append(p)
                _ro(t)
            else:
                _ro(t)
            frozen.append(t)
        self._set({"legacy_frozen_at": _iso(_now())})
        return {"frozen": frozen}

    # -------------------------------------------------- rollback
    def rollback(self, *, reason: str = "operator") -> dict:
        """権威旗を mem0 に戻す（canonical 側刻印は巻き戻さない）。

        猶予窓・凍結刻印は残す（凍結解除は手動判断 —— read-only 化した
        ファイルを元に戻すかどうかは運用判断）。
        """
        prev = self.authority()
        self.set_authority("mem0", rolled_back_at=_iso(_now()),
                           rollback_reason=reason)
        return {"ok": True, "from": prev, "to": "mem0"}


def _ro(path: str):
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass


def _default_update_state(patch: dict):
    from .. import db
    return db.update_state(**patch)


def _default_load_state() -> dict:
    from .. import db
    return db.load_state()
