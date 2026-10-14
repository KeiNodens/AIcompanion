"""adapters.intake —— L-ING 第1相/第2相のオーケストレーション（FA §4.1）

Core が所有する単一の取込口。custom body（in-process adapter）も外部
producer も同一契約を通る（第二書込み路の構造閉塞）。

Phase1 `submit_envelope`（同期・1 tx ack）:
  envelope schema 検証 → 同期 redaction（kernel.envelope）→
  declared-class 照合 → envelope_key dedup → ingress_envelopes 生保存 →
  ingress_watermarks 前進 → normalize_queue enqueue → ingress_receipts 発行。
  全て writer.transaction() の1 tx。ack は receipt。再送は dedup hit で
  保存済み receipt をそのまま返す（byte-for-byte ではなく再計算だが
  内容は冪等）。

Phase2 `drain`（非同期正規化）:
  normalize_queue → normalizer レジストリ（(adapter_id, format_version)
  版管理）→ part → event 写像 → writer.append_event（ingress_key 冪等）。
  part 計数規則・origin 付与・注入 digest 照合（origin=injected 機械付与）
  ・dangling 参照 flag をここで行う。

本番配線（live path への「記録のみ」接続）は P8-B 承認後に行う。
"""

from datetime import datetime, timezone

from ..kernel import canon, envelope as env_kernel, evidence, ids, vocab


class IntakeError(ValueError):
    """envelope 受理の致命的違反（adapter 側の契約違反）。"""


# part.origin → event.actor の既定写像（adapter 経路由来の刻印であり、
# 申告値ではない。adapter spec の actor_map で上書き可）
DEFAULT_ACTOR_MAP = {
    "user": "master",
    "assistant": "companion",
    "tool": "system",
    "system": "system",
    "injected": "system",
    "runtime_observation": "system",
    "imported": "system",
}

# part.class → 既定 event_type（normalizer が細かく上書きしてよい）
DEFAULT_CLASS_EVENT = {
    "conversation": None,   # origin で message/response に分岐
    "tool": "tool_result",
    "job": "job_transition",
    "approval": "approval",
    "channel_meta": "runtime_observation",
    "observation": "runtime_observation",
}

# approval 系 class の供給に必須の capability（SD-26）
REQUIRED_CAP_FOR_CLASS = {"approval": "approval_decision"}


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


class Intake:
    """単一 intake writer。CoreWriter を保持し、Phase1/Phase2 を司る。"""

    def __init__(self, writer, *, clock=None):
        self.writer = writer
        self._clock = clock or _now
        self._adapters = {}       # adapter_id -> spec dict
        self._normalizers = {}    # (adapter_id, format_version) -> fn
        self._injected_digests = set()

    # ------------------------------------------------------------ 登録
    def register_adapter(self, spec: dict):
        """adapter 登録。coverage.classes/capabilities は宣言として保持し、
        declared-class 照合・conformance 検査の基準にする。"""
        aid = spec.get("adapter_id")
        if not aid:
            raise IntakeError("adapter spec requires adapter_id")
        cov = spec.get("coverage") or {}
        spec.setdefault("coverage", {
            "classes": sorted(set(cov.get("classes") or ["conversation"]))})
        spec.setdefault("capabilities", [])
        spec.setdefault("transform_version", "norm-v1")
        spec.setdefault("actor_map", {})
        spec.setdefault("durable_spool", False)
        self._adapters[aid] = spec

    def register_normalizer(self, adapter_id: str, format_version: str, fn):
        self._normalizers[(adapter_id, format_version)] = fn

    # ------------------------------------------------------------ Phase1
    def submit_envelope(self, env: dict) -> dict:
        """Phase1: envelope の durable 化＋receipt を 1 tx で ack。"""
        adapter_id = env.get("adapter_id")
        spec = self._adapters.get(adapter_id)
        if spec is None:
            raise IntakeError(f"unregistered adapter: {adapter_id!r}")

        now = self._clock()
        errs = env_kernel.validate_envelope(env)

        # 同期 redaction＋sanitize（mask 前の値は残さない）
        parts = env.get("parts") or []
        clean_parts, redactions, flags = [], [], []
        for p in parts:
            cp, rec, fl = env_kernel.redact_part(p)
            clean_parts.append(cp)
            if rec:
                redactions.append({"part_id": cp.get("part_id"), **rec})
            flags.extend(fl)

        # declared-class 照合（宣言外 part は flag＋抽出対象外）
        declared = spec["coverage"]["classes"]
        undeclared = env_kernel.check_declared_classes(
            clean_parts, declared)
        for i in undeclared:
            clean_parts[i] = dict(clean_parts[i], undeclared_class=True)
        caps = set(spec.get("capabilities") or [])
        for i, p in enumerate(clean_parts):
            req = REQUIRED_CAP_FOR_CLASS.get(p.get("class"))
            if req and req not in caps and i not in undeclared:
                undeclared.append(i)
                clean_parts[i] = dict(p, undeclared_class=True,
                                      detail=f"missing capability: {req}")

        digest = env_kernel.payload_digest(clean_parts)
        span = env.get("span") or {}
        ekey = env_kernel.envelope_key(
            adapter_id, env.get("stream_id"), span.get("start_cursor"),
            span.get("end_cursor"), digest)

        with self.writer.transaction() as conn:
            # dedup: envelope_key 既受理 → 保存済み receipt を再返す
            row = conn.execute(
                "SELECT envelope_id, status FROM ingress_envelopes"
                " WHERE envelope_key=?", (ekey,)).fetchone()
            if row is not None:
                rcpt = conn.execute(
                    "SELECT * FROM ingress_receipts WHERE envelope_key=?"
                    " ORDER BY seq DESC LIMIT 1", (ekey,)).fetchone()
                return {"accepted": True, "dedup": True,
                        "envelope_key": ekey,
                        "receipt": dict(rcpt) if rcpt else None}

            status = "quarantined" if errs else "stored"
            conn.execute(
                "INSERT INTO ingress_envelopes(envelope_key, envelope_id,"
                " adapter_id, adapter_version, format_version, stream_id,"
                " span_json, coverage_json, payload_mode, payload_inline,"
                " locator_json, payload_digest, occurred_range_json,"
                " parts_json, received_at, recorded_at, seq, status,"
                " detail_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?)",
                (ekey, env.get("envelope_id"), adapter_id,
                 spec.get("adapter_version"),
                 env.get("format_version"), env.get("stream_id"),
                 canon.dumps(span), canon.dumps(env.get("coverage")),
                 env.get("payload_mode", "inline"),
                 env.get("payload_inline"),
                 canon.dumps(env.get("locator"))
                 if env.get("locator") else None,
                 digest, canon.dumps(env.get("occurred_range")),
                 canon.dumps(clean_parts),
                 env.get("received_at"), now,
                 self.writer._peek_next_seq(), status,
                 canon.dumps({"flags": sorted(set(flags)),
                              "redactions": redactions,
                              "undeclared_parts": undeclared,
                              "schema_errors": errs})))

            # 水位前進（cursor は span.end_cursor。確定境界のみ —— adapter が
            # 不安定末尾を送ってこないことを契約とする）
            cur = span.get("end_cursor")
            conn.execute(
                "INSERT INTO ingress_watermarks(adapter_id, stream_id,"
                " cursor_kind, cursor_value, last_envelope_key, updated_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(adapter_id, stream_id) DO UPDATE SET"
                " cursor_kind=excluded.cursor_kind,"
                " cursor_value=excluded.cursor_value,"
                " last_envelope_key=excluded.last_envelope_key,"
                " updated_at=excluded.updated_at",
                (adapter_id, env.get("stream_id"),
                 span.get("cursor_kind", "seq"),
                 None if cur is None else str(cur), ekey, now))

            if not errs:
                conn.execute(
                    "INSERT INTO normalize_queue(envelope_key, priority,"
                    " attempts, state, created_at, updated_at)"
                    " VALUES (?,?,0,'pending',?,?)",
                    (ekey, int(env.get("priority", 0)), now, now))

            verdict = ("quarantined" if errs else
                       "gap_declared" if (env.get("coverage") or {}).get("gaps")
                       else "received")
            receipt_id = ids.record_id("rcpt", f"{ekey[:24]}{now[:8]}")
            detail = {"errors": errs, "redacted_fields":
                      [f for r in redactions for f in r["fields"]],
                      "undeclared_parts": undeclared,
                      "flags": sorted(set(flags)),
                      "parts": len(clean_parts),
                      "gaps": (env.get("coverage") or {}).get("gaps") or []}
            conn.execute(
                "INSERT INTO ingress_receipts(receipt_id, envelope_key,"
                " envelope_id, batch_id, verdict, detail_json, seq,"
                " created_at) VALUES (?,?,?,?,?,?,?,?)",
                (receipt_id, ekey, env.get("envelope_id"),
                 env.get("batch_id"), verdict, canon.dumps(detail),
                 self.writer._peek_next_seq(), now))

            # gap 宣言は ingress_gap marker event として刻む
            for g in (env.get("coverage") or {}).get("gaps") or []:
                self.writer._append_locked(
                    "ingress_gap",
                    {"adapter_id": adapter_id,
                     "stream_id": env.get("stream_id"),
                     "gap": g, "envelope_key": ekey},
                    actor="system")

            return {"accepted": not errs, "dedup": False,
                    "envelope_key": ekey,
                    "receipt": {"receipt_id": receipt_id, "verdict": verdict,
                                "detail": detail}}

    # ------------------------------------------------------------ Phase2
    def drain(self, limit: int = 100) -> list:
        """normalize_queue の pending を lease→正規化→append→done。

        part 計数規則: 入力 part 数 = 発行 event 数＋skip 数で一致しない
        envelope は failed に落とす（silent drop 対治）。undeclared part は
        skipped_part marker event＋その part のみ隔離扱い。"""
        now = self._clock()
        results = []
        with self.writer.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM normalize_queue WHERE state='pending'"
                " ORDER BY priority DESC, queue_id LIMIT ?", (limit,)
            ).fetchall()
            for q in rows:
                conn.execute(
                    "UPDATE normalize_queue SET state='leased',"
                    " attempts=attempts+1, claim_token=?, updated_at=?"
                    " WHERE queue_id=?",
                    (f"lease-{q['queue_id']}-{now}", now, q["queue_id"]))
                try:
                    res = self._normalize_one(conn, q["envelope_key"], now)
                    state = "done" if res["ok"] else "failed"
                    conn.execute(
                        "UPDATE normalize_queue SET state=?, updated_at=?"
                        " WHERE queue_id=?", (state, now, q["queue_id"]))
                    results.append(res)
                except Exception as e:  # poison: 隔離して次へ
                    conn.execute(
                        "UPDATE normalize_queue SET state='failed',"
                        " updated_at=? WHERE queue_id=?",
                        (now, q["queue_id"]))
                    results.append({"envelope_key": q["envelope_key"],
                                    "ok": False, "error": str(e)})
        return results

    def _normalize_one(self, conn, ekey: str, now: str) -> dict:
        erow = conn.execute(
            "SELECT * FROM ingress_envelopes WHERE envelope_key=?",
            (ekey,)).fetchone()
        if erow is None:
            return {"envelope_key": ekey, "ok": False,
                    "error": "envelope row missing"}
        spec = self._adapters.get(erow["adapter_id"]) or {}
        normalizer = self._normalizers.get(
            (erow["adapter_id"], erow["format_version"]),
            self._default_normalizer)
        parts = canon.loads(erow["parts_json"]) or []
        injected = self._load_injected_digests(conn)

        emitted, skipped = 0, []
        for i, p in enumerate(parts):
            if p.get("undeclared_class"):
                skipped.append(i)
                self.writer._append_locked(
                    "skipped_part",
                    {"envelope_key": ekey, "part_id": p.get("part_id"),
                     "reason": "undeclared_class",
                     "class": p.get("class")},
                    actor="system")
                continue
            # 注入内容 digest 照合（本線の echo 検出 —— origin 申告は補助）
            body = p.get("body") if isinstance(p.get("body"), str) else None
            if body and evidence.origin_digest(body) in injected:
                p = dict(p, origin="injected")
            for ev in normalizer(spec, erow, p) or []:
                ik = env_kernel.ingress_key(
                    erow["adapter_id"], erow["stream_id"],
                    p.get("part_id") or f"{i}",
                    spec.get("transform_version", "norm-v1"))
                rec = self.writer._append_locked(
                    ev["event_type"], ev.get("payload") or {},
                    actor=ev.get("actor")
                    or self._actor_for(spec, p),
                    occurred_at=ev.get("occurred_at") or p.get("ts"),
                    session=ev.get("session"),
                    correlation=ev.get("correlation"),
                    source_meta={"adapter_id": erow["adapter_id"],
                                 "envelope_key": ekey,
                                 "part_id": p.get("part_id"),
                                 "origin": p.get("origin"),
                                 "transform_version":
                                     spec.get("transform_version"),
                                 **(ev.get("source_meta") or {})},
                    ingress_key=ik)
                emitted += 1
                # sonder 型 claim（下流行先=extraction のみ P8-3 時点）。
                # 「届いたが記憶にならなかった内訳」の権威は claim_events
                if not rec.get("dedup"):
                    cid = ids.record_id(
                        "claim", canon.hash_bytes(
                            "pcore.claim.v1",
                            f"extraction:{rec['event_id']}".encode())[:24])
                    conn.execute(
                        "INSERT INTO claims(claim_id, event_id, processor,"
                        " status, seq, updated_at) VALUES (?,?,'extraction',"
                        " 'claimed',?,?) ON CONFLICT(claim_id) DO NOTHING",
                        (cid, rec["event_id"], rec["seq"], now))
        ok = (emitted + len(skipped)) == len(parts)
        if not ok:
            conn.execute(
                "UPDATE ingress_envelopes SET status='quarantined',"
                " detail_json=? WHERE envelope_key=?",
                (canon.dumps({"reason": "part_count_mismatch",
                              "parts": len(parts), "emitted": emitted,
                              "skipped": skipped}), ekey))
        else:
            conn.execute(
                "UPDATE ingress_envelopes SET status='normalized'"
                " WHERE envelope_key=?", (ekey,))
        return {"envelope_key": ekey, "ok": ok, "emitted": emitted,
                "skipped": skipped}

    def _actor_for(self, spec: dict, part: dict) -> str:
        amap = {**DEFAULT_ACTOR_MAP, **(spec.get("actor_map") or {})}
        return amap.get(part.get("origin") or "user", "system")

    def _load_injected_digests(self, conn) -> set:
        """context_assembly receipt の注入内容 digest 集合（echo 本線）。"""
        out = set()
        for r in conn.execute(
                "SELECT payload_json FROM events"
                " WHERE event_type='context_assembly'"):
            try:
                pl = canon.loads(r["payload_json"])
            except Exception:
                continue
            for d in (pl.get("injected_digests") or []):
                out.add(d)
        return out

    def _default_normalizer(self, spec, erow, part):
        """既定の part→event 写像（format_version=pcore.envelope.v1）。"""
        cls = part.get("class") or "conversation"
        origin = part.get("origin") or "user"
        if cls == "conversation":
            et = "message" if origin in ("user",) else (
                "response" if origin == "assistant" else "message")
        else:
            et = DEFAULT_CLASS_EVENT.get(cls, "runtime_observation")
        payload = {"body": part.get("body"), "origin": origin,
                   "part_class": cls}
        if part.get("tool_call_id"):
            payload["tool_call_id"] = part["tool_call_id"]
        corr = {}
        if part.get("ref_ids"):
            corr["cause_event_id"] = None
            corr["dangling_refs"] = part["ref_ids"]
        sess = {}
        hint = (canon.loads(erow["coverage_json"] or "{}")
                .get("session_hint"))
        if hint:
            sess = dict(hint) if isinstance(hint, dict) else {}
        return [{"event_type": et, "payload": payload,
                 "occurred_at": part.get("ts"),
                 "session": sess or None, "correlation": corr or None}]

    # ------------------------------------------------------ conformance
    def conformance_report(self, adapter_id: str, emitted_classes) -> dict:
        """adapter の coverage.classes 宣言 ↔ 実 emit の乖離検査。

        emitted_classes: adapter が実際に emit した part.class の集合
        （外部観測・テストで与える）。宣言にあるのに一度も emit されない
        class ＝宣言過剰、宣言外の emit ＝declared-class 違反。"""
        spec = self._adapters.get(adapter_id)
        if spec is None:
            return {"ok": False, "error": f"unregistered: {adapter_id}"}
        declared = set(spec["coverage"]["classes"])
        emitted = set(emitted_classes)
        return {
            "ok": not (emitted - declared),
            "adapter_id": adapter_id,
            "declared": sorted(declared),
            "emitted": sorted(emitted),
            "undeclared_emitted": sorted(emitted - declared),
            "declared_never_emitted": sorted(declared - emitted),
            "capabilities": spec.get("capabilities") or [],
        }
