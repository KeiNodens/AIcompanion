"""kernel.admit —— 単一 admission の決定的検査列（write-path §4・FA §4.3）

記憶・レコードへの全書込み効果は proposal → admit() の単一の門を通る。
LLM はこの検査列に入らない（提案を作る側と判定側の分離 —— A-2）。

proposal 形（write-path §3.1＋SD-12 stale 規約）:
  { record_family, op?, target_id? (None=新規), new_state: <完全 snapshot>,
    basis_events: [{event_id, cite?}], basis_seq, expected_fingerprint?,
    generated_by, generator: {method, model, version}, producer,
    provenance_gap?, importance?, review_hint?,
    suppression_key? （省略時は new_state の content_hash から導出） }

ctx（adapter が prefetch した読み取り専用ビュー）:
  { scopes: {active scope 集合},
    events: {event_id: basis 描述子（kernel.evidence.basis_origin の入力）},
    records: {(scope, record_id): state_json dict},
    by_content_hash: {(scope, content_hash): [(record_id, status)]},
    tombstones: [{pattern_hash, pattern, scope, status}],
    terminal_candidates: {(scope, suppression_key): {status, until}},
    quarantine_ledger: [...],      # quarantined 値の警告台帳（照合視界）
    links: [(source_id, target_id, link_type, blocking)],
    history_hashes: {(scope, record_id): [superseded content_hash 列]},
    policy_digests: {policy_ref: digest},
    now_ts: ISO ts }

verdict（candidate_review イベント payload の中身）:
  { verdict, checks: [{check, result, detail}], evidence_basis,
    gate_inputs, inputs_hash, fired_rules }
判定順序（SD-45/F-09）: tombstone 類似 → suppression TTL。
pending 相当があれば pending を優先。最終 verdict の優先度:
  stale > reject > quarantine > pending > noop_duplicate > accept
"""

from . import canon, evidence, vocab

ADMIT_VERSION = "admit-v1"

# 実装時パラメータ（運用値 —— phase8-impl.md に記録）
TOMBSTONE_SIMILARITY = 0.8      # trigram Jaccard の pending 閾値
REVIEW_IMPORTANCE_MIN = 4       # review.required を立てる importance 閾値
REVIEW_FAMILIES = frozenset({"identity"})   # 常に review 対象の族

# 規則駆動 producer（decay_sweep 等）は review.required 免除＋証拠資格
# 検査も免除（SD-72・write-path §5.2）。規則は新知識を主張しないため
# basis 空でも quarantine にならない。verdict receipt への刻印は常に行う。
# suspect は含めない（LLM 疑いレーンは pending へ流すのが設計）。
RULE_PRODUCERS = frozenset({
    "decay_sweep", "tier_cycle", "prediction_sweep", "reflection",
    "migration", "import",
})

# 人間権威語彙（basis/correlation が actor=master に到達必須 —— SD-15）
AUTHORITY_OPS = frozenset({"correct"})
AUTHORITY_MARKERS = (
    "マスターが言", "マスターの指示", "本人が言", "オーナーが言",
    "ユーザー本人", "master said", "owner said",
)

# 相対時刻語（自己完結化規約の判定側 —— 機械項目のみ。SD-77）
_RELATIVE_TIME = (
    "明日", "昨日", "今日", "さっき", "先ほど", "今週", "先週", "来週",
    "あとで", "そのうち", "いつか", "最近", "この前", "今朝", "今夜",
    "tomorrow", "yesterday", "today", "recently",
)

# verdict 優先度（大きいほど優先）
_VERDICT_RANK = {
    "accept": 0, "noop_duplicate": 1, "pending": 2,
    "quarantine": 3, "reject": 4, "stale": 5,
}


class AdmitError(ValueError):
    """proposal 形の致命的違反（検査列に乗せる以前の拒否）。"""


def fingerprint(state) -> str:
    """CAS 用の対象状態指紋（None は「未存在」の指紋）。"""
    return canon.hash_obj("pcore.fingerprint.v1", state)


def suppression_key_of(proposal: dict) -> str:
    """終端再提案を抑制する鍵（既定: 対象 scope＋内容の正規化 digest）。"""
    st = proposal.get("new_state") or {}
    text = st.get("content") or st.get("text") or st.get("value") \
        or canon.dumps(st)
    return canon.hash_tuple("pcore.suppression.v1",
                            st.get("scope", ""), canon.norm_text(str(text)))


def _content_text(state: dict) -> str:
    for f in ("content", "text", "value", "title", "claim"):
        v = state.get(f)
        if isinstance(v, str) and v:
            return v
    return canon.dumps({k: v for k, v in state.items()
                        if k not in ("provenance", "subject", "attachments",
                                     "migration", "external_ref")})


# ------------------------------------------------------------- 個別検査

def _trigrams(s: str) -> set:
    s = canon.norm_text(s)
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def trigram_jaccard(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _secret_hit(text: str) -> bool:
    from . import envelope as _env
    for rx, _kind in _env._SECRET_PATTERNS:
        if rx.search(text):
            return True
    return False


def _links_make_cycle(links, new_edge) -> bool:
    """blocking 辺の DAG 検査（new_edge=(src,tgt) を足すと循環するか）。"""
    adj = {}
    for s, t, lt, blocking in links:
        if blocking:
            adj.setdefault(s, set()).add(t)
    s, t = new_edge
    adj.setdefault(s, set()).add(t)
    # t から s へ到達できれば循環
    stack, seen = [t], set()
    while stack:
        n = stack.pop()
        if n == s:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack.extend(adj.get(n, ()))
    return False


# --------------------------------------------------------------- 検査列

def evaluate(proposal: dict, ctx: dict) -> dict:
    """決定的検査列を全て走らせ verdict を返す（all-or-nothing 全列挙）。"""
    checks = []

    def rec(name, result, detail=""):
        checks.append({"check": name, "result": result, "detail": detail})

    st = proposal.get("new_state") or {}
    family = proposal.get("record_family") or st.get("record_family")
    scope = st.get("scope")
    target_id = proposal.get("target_id") or st.get("record_id")
    op = proposal.get("op")
    records = ctx.get("records") or {}
    prev_state = records.get((scope, target_id)) if target_id else None
    prev_hash_state = (
        {k: v for k, v in prev_state.items()} if prev_state else None)

    # ---- stale CAS（SD-12）: expected_fingerprint/basis_seq の再照合
    exp_fp = proposal.get("expected_fingerprint")
    if exp_fp is not None:
        cur_fp = fingerprint(prev_state)
        if cur_fp != exp_fp:
            rec("stale_cas", "fail",
                f"fingerprint mismatch: expected {exp_fp},"
                f" current {cur_fp}")
        else:
            rec("stale_cas", "pass")
    basis_seq = proposal.get("basis_seq")
    if basis_seq is not None and ctx.get("head_seq") is not None:
        if basis_seq < ctx["head_seq"] and prev_state is not None:
            last_mut = (ctx.get("mutation_seqs") or {}).get(
                (scope, target_id))
            if last_mut is not None and last_mut > basis_seq:
                rec("stale_cas", "fail",
                    f"basis_seq {basis_seq} < last_mutation_seq {last_mut}")

    # ---- schema / shape
    errs = []
    if not isinstance(st, dict) or not st:
        errs.append("new_state missing")
    else:
        for f in ("record_id", "record_family", "scope", "status",
                  "record_version", "created_at", "updated_at"):
            if f not in st:
                errs.append(f"new_state.{f} missing")
        if family and st.get("record_family") != family:
            errs.append("record_family mismatch")
        for f in ("status", "record_family", "kind", "memory_type", "tier",
                  "sensitivity"):
            v = st.get(f)
            if vocab.is_reserved_value(v or ""):
                errs.append(f"reserved {f} rejected: {v}")
        if st.get("status") and st["status"] not in vocab.MEMORY_STATUS \
                and family == "memory":
            errs.append(f"memory status unknown: {st['status']}")
        if st.get("memory_type") and family == "memory" \
                and st["memory_type"] not in vocab.MEMORY_TYPES:
            errs.append(f"memory_type unknown: {st['memory_type']}")
    if not proposal.get("producer"):
        errs.append("producer missing")
    if not proposal.get("generated_by"):
        errs.append("generated_by missing")
    if scope and scope not in (ctx.get("scopes") or set()):
        errs.append(f"scope not registered: {scope}")
    if errs:
        rec("schema_shape", "fail", "; ".join(errs))
    else:
        rec("schema_shape", "pass")

    # ---- secret scan（提案本文の秘密混入）
    text = _content_text(st) if isinstance(st, dict) else ""
    rec("secret_scan", "fail" if _secret_hit(text) else "pass",
        "secret pattern in proposal body" if _secret_hit(text) else "")

    # ---- basis 実在性（source_events FK）
    basis_ids = [b.get("event_id") for b in proposal.get("basis_events") or []
                 if isinstance(b, dict)]
    events = ctx.get("events") or {}
    missing = [e for e in basis_ids if e not in events]
    rec("basis_existence",
        "fail" if missing else "pass",
        f"missing basis events: {missing}" if missing else "")

    # ---- evidence（証拠資格・汚染伝播・cite）
    basis_infos = []
    for b in proposal.get("basis_events") or []:
        if not isinstance(b, dict):
            continue
        info = dict(events.get(b.get("event_id")) or {})
        info["event_id"] = b.get("event_id")
        if b.get("cite"):
            info["cite"] = b["cite"]
        basis_infos.append(info)
    suspicion = bool(proposal.get("suspicion"))
    if suspicion or proposal.get("producer") in RULE_PRODUCERS:
        # 規則駆動 producer は「新知識の主張」ではなく規則再計算のため
        # 証拠資格検査を免除（basis 空でも quarantine にしない —— SD-72）。
        # suspicion レーンも同じく basis 空で pending へ流すのが設計。
        # basis 語彙は閉集合なので規則系は前 record の格を継承し、
        # suspicion は self_derived（system 生成の疑い）に畳む。
        inherited = ((prev_state or {}).get("provenance") or {}) \
            .get("evidence_basis")
        ev = {"disposition": "admit",
              "evidence_basis": ("self_derived" if suspicion
                                 else inherited or "self_derived"),
              "reasons": ["suspicion_lane" if suspicion
                          else "rule_producer"], "cite_missing": []}
    else:
        ev = evidence.evaluate_basis(proposal, basis_infos, scope or "")
        if missing:
            ev = {**ev, "disposition": "quarantine",
                  "reasons": ev.get("reasons", [])
                  + ["missing basis events"]}
    if ev["cite_missing"]:
        rec("cite_required", "fail",
            f"cite required for tool/system basis: {ev['cite_missing']}")
    else:
        rec("cite_required", "pass")
    rec("evidence",
        "fail" if ev["disposition"] == "quarantine" else "pass",
        "; ".join(ev.get("reasons") or []))

    # ---- 権威主張照合（SD-15）
    auth = op in AUTHORITY_OPS or any(m in text for m in AUTHORITY_MARKERS)
    if auth:
        master_reach = any(i.get("actor") == "master" for i in basis_infos)
        rec("authority_claim",
            "pass" if master_reach else "pending",
            "no actor=master basis for authority claim" if not master_reach
            else "")
    else:
        rec("authority_claim", "pass")

    # ---- tombstone 照合（完全一致=reject・類似=pending。SD-07/SD-45）
    chash = canon.content_hash(text) if text else None
    tomb_hit, tomb_sim = None, None
    for t in ctx.get("tombstones") or []:
        if t.get("status") != "active" or t.get("scope") != scope:
            continue
        if chash and t.get("pattern_hash") == canon.pattern_hash(text):
            tomb_hit = t
            break
        pat = t.get("pattern")
        if pat:
            sim = trigram_jaccard(text, pat)
            if sim >= TOMBSTONE_SIMILARITY \
                    and (tomb_sim is None or sim > tomb_sim[0]):
                tomb_sim = (sim, t)
    if tomb_hit:
        rec("tombstone", "fail", "pattern_hash exact match")
    elif tomb_sim:
        rec("tombstone", "pending",
            f"similar to tombstone (jaccard={tomb_sim[0]:.3f})")
    else:
        rec("tombstone", "pass")

    # ---- suppression（終端再提案の TTL 抑制 —— tombstone 照合の後）
    skey = proposal.get("suppression_key") or suppression_key_of(proposal)
    term = (ctx.get("terminal_candidates") or {}).get((scope, skey))
    if term and term.get("until"):
        rec("suppression", "fail",
            f"suppressed until {term['until']} (status={term.get('status')})")
    elif term:
        rec("suppression", "pending",
            f"terminal candidate exists (status={term.get('status')})")
    else:
        rec("suppression", "pass")

    # ---- content_hash dedup（scope 必須照合 —— SD-13）
    dup = None
    for rid, stt in (ctx.get("by_content_hash") or {}).get(
            (scope, chash), []):
        dup = (rid, stt)
        break
    if dup and dup[0] == target_id:
        # op 付き更新（param_update/demote 等）の自己照合は重複ではない
        rec("dedup", "pass")
    elif dup and dup[1] in ("superseded", "invalidated"):
        rec("dedup", "pending",
            f"matches previously corrected/invalidated record {dup[0]}")
    elif dup and dup[1] == "rejected":
        rec("dedup", "pending",
            f"matches terminal candidate {dup[0]}（以前棄却済み）")
    elif dup:
        rec("dedup", "noop",
            f"identical active record {dup[0]}（確証として記録のみ）")
    else:
        rec("dedup", "pass")

    # ---- 証拠 source 被覆（Q-O82）: 受理済み memory・非終端 pending
    # candidate の source 集合が proposal basis を完全包含 → 被覆=受理不要。
    # 部分包含は被覆としない（束ね要約の遮断を防ぐ —— 構造矛盾側へ流れる）。
    covered = None
    if basis_ids:
        for owner, srcset in (
                ctx.get("source_covers") or {}).get(scope, []):
            if set(basis_ids) <= srcset:
                covered = owner
                break
    if covered:
        rec("basis_coverage", "noop",
            f"basis fully covered by existing {covered}")
    else:
        rec("basis_coverage", "pass")

    # ---- 汚染伝播（SD-60・2段）: quarantine 確定 proposal の basis 辺を辿った
    # 集合を ctx.contaminated_events で受け取り、そこに乗る basis は
    # suspicion lane（pending —— quarantine ではなく再検討扱い）へ。
    contaminated = ctx.get("contaminated_events") or set()
    c_hit = [e for e in basis_ids if e in contaminated]
    rec("contamination",
        "pending" if c_hit else "pass",
        f"basis touches contaminated events: {c_hit}" if c_hit else "")

    # ---- 構造矛盾（同一 subject・同一属性で値が異なる）
    contra = []
    subj = st.get("subject")
    attr = st.get("attribute") or st.get("kind") or st.get("memory_type")
    if subj and attr:
        nval = st.get("value") or st.get("content")
        for (rscope, rid), rst in records.items():
            if rscope != scope or rid == target_id:
                continue
            if rst.get("status") not in ("active", "demoted"):
                continue
            if rst.get("subject") == subj and (
                    rst.get("attribute") or rst.get("kind")
                    or rst.get("memory_type")) == attr:
                oval = rst.get("value") or rst.get("content")
                if nval and oval and canon.norm_text(str(nval)) \
                        != canon.norm_text(str(oval)):
                    contra.append(rid)
    rec("contradiction",
        "pending" if contra else "pass",
        f"structural conflict with {contra}" if contra else "")

    # ---- concreteness（自己完結化規約の判定側 —— 機械項目のみ）
    # 規則駆動 producer は新規本文を書かない（param_update/resolve は
    # 既存本文の運搬）ので相対時刻残渣検査は意味を持たず免除 —— 免除
    # しないと sweep が永久に pending で滞留する。
    vague = [] if proposal.get("producer") in RULE_PRODUCERS \
        else [w for w in _RELATIVE_TIME if w in text]
    rec("concreteness",
        "pending" if vague else "pass",
        f"relative-time residue: {vague}" if vague else "")

    # ---- oscillation（supersede で過去版に戻す A→B→A）
    hist = (ctx.get("history_hashes") or {}).get((scope, target_id), [])
    if op in ("supersede", "merge") and chash and chash in hist:
        rec("oscillation", "pending",
            "new content matches a previously superseded version")
    else:
        rec("oscillation", "pass")

    # ---- 侵食 floor（フィールド・列挙 member の除去/空白化 —— SD-56）
    removed = []
    if prev_hash_state is not None:
        for k, v in prev_hash_state.items():
            if k in ("record_version", "updated_at", "last_mutation_seq"):
                continue
            if k not in st or st.get(k) in (None, "", [], {}):
                removed.append(k)
    if removed:
        declared = set(proposal.get("removed_fields") or [])
        linked = op in ("supersede", "invalidate", "merge") \
            or proposal.get("linked_tombstone")
        undeclared = [k for k in removed if k not in declared]
        if undeclared and not linked:
            rec("erosion_floor", "pending",
                f"undeclared field removal: {undeclared}")
        else:
            rec("erosion_floor", "pass",
                f"declared removal: {removed}" if removed else "")
    else:
        rec("erosion_floor", "pass")

    # ---- link 循環（blocking 辺の DAG 検査）
    new_links = proposal.get("new_links") or []
    cyc = [e for e in new_links
           if len(e) >= 2 and _links_make_cycle(
               ctx.get("links") or [], (e[0], e[1]))]
    rec("link_cycle", "fail" if cyc else "pass",
        f"blocking edge cycle via {cyc}" if cyc else "")

    # ---- param_policy_ref 管理値の再計算照合（申告値を信用しない）
    pref = st.get("param_policy_ref")
    if pref:
        known = (ctx.get("policy_digests") or {}).get(pref)
        if known is None:
            rec("param_policy", "pending",
                f"param_policy_ref not registered: {pref}")
        else:
            rec("param_policy", "pass")
    else:
        rec("param_policy", "pass")

    # ---- review.required（限定 review —— D-B1/D-E2）
    importance = st.get("importance") or proposal.get("importance") or 0
    rule_producer = proposal.get("producer") in RULE_PRODUCERS
    review_req = not rule_producer and (
        importance >= REVIEW_IMPORTANCE_MIN
        or family in REVIEW_FAMILIES
        or bool(proposal.get("review_hint")))
    rec("review_required",
        "pending" if review_req else "pass",
        (f"importance={importance} family={family}" if review_req
         else "rule_producer" if rule_producer else ""))

    # ---- suspicion lane（kernel が pending に写す —— write-path §8.1）
    rec("suspicion_lane",
        "pending" if proposal.get("suspicion") else "pass")

    # ---- archive 遷移は auto_apply しない（FA §6.2・Q-O47）。
    # tier_change.to=='archive' の proposal は必ず pending ——
    # 翌朝承認面で master が決裁するまで留まる。
    tc = proposal.get("tier_change") or {}
    rec("tier_archive_pending",
        "pending" if tc.get("to") == "archive" else "pass")

    # ------------------------------------------------ verdict 集計
    fired = [c["check"] for c in checks if c["result"] not in ("pass",)]
    has = {c["check"]: c["result"] for c in checks}
    if has.get("stale_cas") == "fail":
        verdict = "stale"
    elif any(c["result"] == "fail" for c in checks):
        verdict = "reject" if has.get("schema_shape") == "fail" \
            or has.get("secret_scan") == "fail" \
            or has.get("tombstone") == "fail" \
            or has.get("suppression") == "fail" \
            or has.get("link_cycle") == "fail" else "quarantine"
    elif any(c["result"] == "pending" for c in checks):
        verdict = "pending"
    elif has.get("dedup") == "noop" or has.get("basis_coverage") == "noop":
        verdict = "noop_duplicate"
    else:
        verdict = "accept"

    gate_inputs = {
        "proposal_hash": canon.hash_obj("pcore.proposal.v1", proposal),
        "basis_seq": basis_seq,
        "fingerprint": fingerprint(prev_state),
        "tombstone_digest": canon.hash_obj(
            "pcore.tombstone_set.v1",
            sorted((t.get("pattern_hash") or "")
                   for t in ctx.get("tombstones") or [])),
        "policy_ref": pref,
    }
    return {
        "verdict": verdict,
        "admit_version": ADMIT_VERSION,
        "checks": checks,
        "fired_rules": fired,
        "evidence_basis": ev.get("evidence_basis"),
        "gate_inputs": gate_inputs,
        "inputs_hash": canon.hash_obj("pcore.gate_inputs.v1", gate_inputs),
        "suppression_key": skey,
    }


def gate_inputs_fresh(candidate_state: dict, ctx: dict) -> dict:
    """決裁・昇格時点での gate.inputs 再計算（SD-12: stale 決裁の拒否用）。"""
    records = ctx.get("records") or {}
    scope = candidate_state.get("scope")
    target = (candidate_state.get("proposal") or {}).get("target_id")
    prev_state = records.get((scope, target)) if target else None
    gi = dict(candidate_state.get("gate_inputs") or {})
    gi["fingerprint"] = fingerprint(prev_state)
    gi["tombstone_digest"] = canon.hash_obj(
        "pcore.tombstone_set.v1",
        sorted((t.get("pattern_hash") or "")
               for t in ctx.get("tombstones") or []))
    return gi
