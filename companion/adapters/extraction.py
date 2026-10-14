"""adapters.extraction —— Event Log → proposal の水位追跡抽出（write-path §3）

水位（`watermarks(processor)`）以降の events を順に走査し、
`kernel.extract.material_ok` の許可リストを通ったイベントだけを
extractor（adapter 注入・LLM 可）に渡す。戻ってきた proposal はすべて
`Admission.submit`（admission の単一の門）に流し、抽出器は memory を
直接書かない。

- 処理済み位置は `extraction_scanned` marker event＋watermark 前進で刻む
- 失敗は extraction_attempts に計数し、`POISON_MAX_ATTEMPTS` 超過で
  `extraction_poisoned` event＋claim=stalled を残して水位を進める
- claim: drain 時に `claims(processor=<name>)` が発行されている前提。
  処理結果に応じて stored/no_lesson/cancelled へ遷移（claim_events が権威）
"""

from datetime import datetime, timezone

from ..kernel import canon, extract, ids


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


class Extraction:
    """水位追跡抽出プロセッサ。

    extractor: callable(event_dict) -> list[proposal] | None
      event_dict = {seq, event_id, event_type, actor, occurred_at,
                    payload, source_meta}
    """

    def __init__(self, writer, admission, *, name="extraction",
                 extractor=None, clock=None):
        self.writer = writer
        self.admission = admission
        self.name = name
        self.extractor = extractor or (lambda ev: [])
        self._clock = clock or _now

    def _watermark(self, conn) -> int:
        row = conn.execute(
            "SELECT last_done_seq FROM watermarks WHERE processor=?",
            (self.name,)).fetchone()
        return row["last_done_seq"] if row else 0

    def _set_watermark(self, conn, seq: int, now: str):
        conn.execute(
            "INSERT INTO watermarks(processor, last_done_seq, updated_at)"
            " VALUES (?,?,?) ON CONFLICT(processor) DO UPDATE SET"
            " last_done_seq=excluded.last_done_seq,"
            " updated_at=excluded.updated_at",
            (self.name, seq, now))

    def _claim(self, conn, event_id: str, seq: int, now: str):
        cid = ids.record_id("claim",
                            canon.hash_bytes("pcore.claim.v1",
                                             f"{self.name}:{event_id}"
                                             .encode())[:24])
        conn.execute(
            "INSERT INTO claims(claim_id, event_id, processor, status,"
            " seq, updated_at) VALUES (?,?,?,'claimed',?,?)"
            " ON CONFLICT(claim_id) DO NOTHING",
            (cid, event_id, self.name, seq, now))
        return cid

    def _claim_transition(self, conn, claim_id: str, to: str,
                          reason: str, now: str):
        row = conn.execute(
            "SELECT status FROM claims WHERE claim_id=?",
            (claim_id,)).fetchone()
        if row is None:
            return
        conn.execute(
            "INSERT INTO claim_events(claim_id, from_status, to_status,"
            " reason, ts) VALUES (?,?,?,?,?)",
            (claim_id, row["status"], to, reason, now))
        conn.execute(
            "UPDATE claims SET status=?, updated_at=? WHERE claim_id=?",
            (to, now, claim_id))

    def run(self, limit: int = 100) -> dict:
        """水位以降のイベントを走査・抽出・admission 投入。1 回の run は
        1 tx にまとめず、event 単位の失敗隔離のため逐次 tx にする。
        （まとめて poison になるのを避けるため per-event で commit）"""
        now = self._clock()
        with self.writer.transaction() as conn:
            wm = self._watermark(conn)
            rows = conn.execute(
                "SELECT seq, event_id, event_type, actor, occurred_at,"
                " payload_json, source_meta_json FROM events WHERE seq>?"
                " ORDER BY seq LIMIT ?", (wm, limit)).fetchall()
        scanned = proposed = skipped = poisoned = 0
        verdict_counts = {}
        last_done = wm
        for r in rows:
            now = self._clock()
            desc = {
                "seq": r["seq"], "event_id": r["event_id"],
                "event_type": r["event_type"], "actor": r["actor"],
                "occurred_at": r["occurred_at"],
                "payload": canon.loads(r["payload_json"]),
                "source_meta": canon.loads(r["source_meta_json"] or "{}"),
            }
            desc["origin"] = (desc["source_meta"] or {}).get("origin")
            ok, _reason = extract.material_ok(desc)
            with self.writer.transaction() as conn:
                if not ok:
                    self._set_watermark(conn, r["seq"], now)
                    skipped += 1
                    last_done = r["seq"]
                    continue
                cid = self._claim(conn, r["event_id"], r["seq"], now)
            try:
                proposals = self.extractor(desc) or []
            except Exception as e:  # poison カウント＋隔離
                with self.writer.transaction() as conn:
                    conn.execute(
                        "INSERT INTO extraction_attempts(processor, seq,"
                        " attempts, last_error, updated_at) VALUES (?,?,1,?,?)"
                        " ON CONFLICT(processor, seq) DO UPDATE SET"
                        " attempts=attempts+1, last_error=excluded.last_error,"
                        " updated_at=excluded.updated_at",
                        (self.name, r["seq"], str(e)[:200], now))
                    att = conn.execute(
                        "SELECT attempts FROM extraction_attempts"
                        " WHERE processor=? AND seq=?",
                        (self.name, r["seq"])).fetchone()["attempts"]
                    if extract.poisoned(att):
                        self.writer._append_locked(
                            "extraction_poisoned",
                            {"processor": self.name, "event_id":
                             r["event_id"], "seq": r["seq"],
                             "attempts": att, "error": str(e)[:200]},
                            actor="system")
                        self._claim_transition(conn, cid, "stalled",
                                               "poison", now)
                        self._set_watermark(conn, r["seq"], now)
                        poisoned += 1
                        last_done = r["seq"]
                if not extract.poisoned(att):
                    # 未達なら水位をここで止める（後続イベントを飛び越さない
                    # —— 水位は「連続して処理/隔離済み」の最後の seq）
                    break
                continue
            verdicts = []
            for prop in proposals:
                out = self.admission.submit(prop)
                verdicts.append(out["verdict"])
                verdict_counts[out["verdict"]] = (
                    verdict_counts.get(out["verdict"], 0) + 1)
            with self.writer.transaction() as conn:
                to = "stored" if proposals else "no_lesson"
                self._claim_transition(conn, cid, to,
                                       ",".join(sorted(set(verdicts)))
                                       or "empty", now)
                self._set_watermark(conn, r["seq"], now)
            proposed += len(proposals)
            last_done = r["seq"]
        with self.writer.transaction() as conn:
            self.writer._append_locked(
                "extraction_scanned",
                {"processor": self.name, "from_seq": wm,
                 "to_seq": last_done, "scanned": len(rows),
                 "skipped": skipped, "proposed": proposed,
                 "poisoned": poisoned},
                actor="system")
        return {"processor": self.name, "from_seq": wm, "to_seq": last_done,
                "scanned": len(rows), "skipped": skipped,
                "proposed": proposed, "poisoned": poisoned,
                "verdicts": verdict_counts}
