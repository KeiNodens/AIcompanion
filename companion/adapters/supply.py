"""adapters.supply —— canonical からの読み出し・供給（read-path.md）

- 想起 = vec0 ∪ fts_or 候補 union → canonical 権威フィルタ（records の
  scope/status/sensitivity を再検査 —— SD-23 第二層）→ score-v1 リランク
- derived が無い/遅れている場合は canonical 併合走査に fallback（SD-22）
- 供給経路は `supply_context` のみ。内部の `_recall`/`_structural` は
  receipt を発行しない（応答へ混ざる供給は必ず context_assembly receipt
  が先行 —— SD-11）
- abstention: `kernel.supply.abstain_reason`（no_match/below_threshold/
  withheld。scope_denied は外部語彙では no_match に畳む）
"""

import json
import logging
import math
import os
from datetime import datetime, timezone

from ..kernel import canon, supply as sup

log = logging.getLogger("companion")

# recall に使わない status（read-path §5: active のみが既定）
_INJECT_FAMILIES_STRUCTURAL = ("identity", "usermodel", "state")

# C-3: CE 再採点を掛ける上位候補プール深さ（RERANK_DEPTH で上書き可）
_RERANK_DEPTH = 30

# C-4: entity 腕の edges 1ホップ展開 cap（溢れは link_expansion_capped）
_ENTITY_EXPAND_MAX = 10

# 時間腕のカバレッジ選出で窓から読む候補プール上限
_TEMPORAL_POOL_CAP = 120

# recency reserve（Mem++ event-reserve 相当）: fused rank で負けても
# scope 内最新の記憶を予約枠で必ず供給する機械的保証の枠数
_RECENCY_RESERVE = 3


def _now() -> str:
    return canon.canon_ts(datetime.now(timezone.utc))


def _cosine(a, b) -> float:
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


class Supply:
    """供給の不純な orchestration。canonical read は writer 接続、
    derived read は DerivedStore の active 世代接続。"""

    def __init__(self, writer, derived, *, embedder=None, clock=None,
                 deriver=None, reranker=None):
        self.writer = writer
        self.derived = derived          # DerivedStore または None
        self.embedder = embedder        # Embedder または None（dense 無し経路）
        # C-3: cross-encoder 後段再採点（None なら score-v1 順のまま）
        self.reranker = reranker
        # 揮発値の読み時導出（SD-54: anchor+規則+receipt からの正値）。
        # None なら格納 anchor 値をそのまま使う（sweep 未配線の既定）。
        self.deriver = deriver
        self._clock = clock or _now

    # --------------------------------------------------------- recall
    def _recall(self, scope: str, query: str, k: int = sup.RECALL_TOPK,
                visible_tiers=None):
        """候補 union → canonical 再検査 → score-v1。receipt は発行しない。
        戻り値 {items, abstain, stats}（items は admission 済み = supply
        admit 通過分のみ。弾かれた数は omitted 計数へ）。"""
        cand = {}   # rid -> {dense_sim, lex_norm}
        used_gen = None
        ent_stats = None

        # ---- dense 経路（vec0 or blob fallback）
        dcon, gen = (self.derived.open_active()
                     if self.derived else (None, None))
        lex_max = 0.0
        try:
            if dcon is not None:
                used_gen = gen
                qv = (self.embedder.embed([query])[0]
                      if self.embedder else None)
                if qv is not None and self.derived.vec0:
                    has = dcon.execute(
                        "SELECT name FROM sqlite_master WHERE name='mem_vec'"
                    ).fetchone()
                    if has:
                        for r in dcon.execute(
                                "SELECT record_id, distance FROM mem_vec"
                                " WHERE embedding MATCH ? AND k=? AND scope=?"
                                " ORDER BY distance",
                                (json.dumps(qv), k * 3, scope)):
                            cand.setdefault(r["record_id"], {})[
                                "dense_sim"] = 1.0 - r["distance"]
                elif qv is not None:
                    for r in dcon.execute(
                            "SELECT record_id, vec_json FROM mem_vec_blob"
                            " WHERE scope=?", (scope,)):
                        cand.setdefault(r["record_id"], {})["dense_sim"] = \
                            _cosine(qv, json.loads(r["vec_json"]))

                # ---- lexical 経路（trigram OR / LIKE fallback）
                terms = self.derived.fts_terms(query)
                if terms is None:
                    q = canon.norm_text(query)
                    esc = (q.replace("\\", "\\\\").replace("%", "\\%")
                           .replace("_", "\\_"))
                    for r in dcon.execute(
                            "SELECT s.record_id FROM mem_search s WHERE"
                            " s.scope=? AND s.norm_text LIKE ? ESCAPE '\\'",
                            (scope, f"%{esc}%")):
                        cand.setdefault(r["record_id"], {})["lexical"] = True
                        cand[r["record_id"]]["lex_norm"] = 1.0
                elif terms:
                    expr = " OR ".join(f'"{t}"' for t in terms)
                    try:
                        for r in dcon.execute(
                                "SELECT s.record_id, -bm25(mem_fts) score"
                                " FROM mem_fts f JOIN mem_search s"
                                " ON s.rowid=f.rowid WHERE mem_fts MATCH ?"
                                " AND s.scope=? ORDER BY score DESC LIMIT ?",
                                (expr, scope, k * 3)):
                            c = cand.setdefault(r["record_id"], {})
                            c["lexical"] = True
                            c["lex_norm"] = r["score"]
                            lex_max = max(lex_max, r["score"])
                    except Exception:
                        pass

                # ---- entity 腕（C-4: クエリ中の entity → 索引一致 →
                # edges 1ホップ展開。索引は derived 内なのでこの経路のみ）
                nq = canon.norm_text(query)
                if nq and os.getenv("CANONICAL_ENTITY", "1").strip() \
                        not in ("0", "off", "false"):
                    hits = dcon.execute(
                        "SELECT entity_norm, record_id FROM entities"
                        " WHERE scope=? AND instr(?, entity_norm) > 0",
                        (scope, nq)).fetchall()
                    hop1 = {r["record_id"] for r in hits}
                    matched_ents = sorted({r["entity_norm"] for r in hits})
                    for rid in hop1:
                        c = cand.setdefault(rid, {})
                        c["entity"] = True
                        c["lexical"] = True
                        c["lex_norm"] = max(c.get("lex_norm") or 0.0,
                                            (lex_max or 1.0) * 0.9)
                    # 1ホップ展開（edges 索引・cap 付き。溢れは
                    # link_expansion_capped 計数へ）
                    hop2 = []
                    if hop1:
                        ph = ",".join("?" for _ in hop1)
                        for r in dcon.execute(
                                "SELECT source_id, target_id FROM edges"
                                " WHERE scope=? AND (source_id IN ("
                                f"{ph}) OR target_id IN ({ph}))",
                                (scope, *hop1, *hop1)):
                            for nid in (r["source_id"], r["target_id"]):
                                if nid not in hop1:
                                    hop2.append(nid)
                    hop2 = sorted(set(hop2))
                    capped = 0
                    if len(hop2) > _ENTITY_EXPAND_MAX:
                        capped = len(hop2) - _ENTITY_EXPAND_MAX
                        hop2 = hop2[:_ENTITY_EXPAND_MAX]
                    for rid in hop2:
                        c = cand.setdefault(rid, {})
                        c["entity_hop"] = 2
                        c["lexical"] = True
                        c["lex_norm"] = max(c.get("lex_norm") or 0.0,
                                            (lex_max or 1.0) * 0.5)
                    if hits or hop2:
                        ent_stats = {"matched": matched_ents,
                                     "hop1": len(hop1), "hop2": len(hop2),
                                     "capped": capped}
        finally:
            if dcon is not None:
                dcon.close()

        # ---- canonical fallback（derived 無し/空: scope+status 権威側で
        # 併合走査 —— SD-22 の退路）
        if dcon is None:
            from .derived import embed_source_text
            q = canon.norm_text(query)
            for r in self.writer._conn.execute(
                    "SELECT record_id, state_json FROM records WHERE"
                    " record_family='memory' AND scope=?", (scope,)):
                st = canon.loads(r["state_json"])
                text = st.get("content") or st.get("text") or ""
                c = cand.setdefault(r["record_id"], {})
                if q and canon.norm_text(text).find(q) >= 0:
                    c["lexical"] = True
                    c["lex_norm"] = 1.0
                if self.embedder:
                    c["dense_sim"] = _cosine(
                        self.embedder.embed([query])[0],
                        self.embedder.embed(
                            [embed_source_text(st)])[0])
            lex_max = 1.0

        # ---- temporal 腕（C-2: 日付・相対・季節表現 → 時間窓候補。
        # 決定的パーサが窓を引けたときだけ発動。canonical records の
        # 有効時刻を直接走査するので derived 世代差の影響を受けない）
        tw = None
        if os.getenv("CANONICAL_TEMPORAL", "1").strip() not in \
                ("0", "off", "false"):
            from . import temporal
            tw = temporal.parse_time_window(query, self._clock())
        if tw:
            hits = 0
            # 時刻源: 記述された出来事の時刻（occurred_at）を最優先、
            # 無ければ有効開始・記録時刻へ。null の行を as-of から
            # 黙って落とさないための fallback 連結（Mem++ の罠を回避）
            ts_col = ("COALESCE("
                      "json_extract(state_json,'$.occurred_at'),"
                      " valid_from, created_at)")
            cover = os.getenv("CANONICAL_TEMPORAL_COVERAGE",
                              "1").strip() not in ("0", "off", "false")
            lim = k * 3
            fetch = _TEMPORAL_POOL_CAP if cover else lim
            rows = self.writer._conn.execute(
                f"SELECT record_id, {ts_col} ts FROM records WHERE"
                " record_family='memory' AND scope=?"
                f" AND {ts_col} >= ? AND {ts_col} < ?"
                f" ORDER BY {ts_col} DESC LIMIT ?",
                (scope, tw["start"], tw["end"], fetch)).fetchall()
            rids = ([r["record_id"] for r in rows] if not cover else
                    temporal.coverage_pick(
                        [(r["record_id"], r["ts"]) for r in rows],
                        tw, lim))
            for rid in rids:
                c = cand.setdefault(rid, {})
                c["temporal"] = True
                c["lexical"] = True
                # 強い lexical 相当の信号として扱う（FTS bm25 の正規化
                # 尺度に合わせ、無ければ 0.9 固定）
                c["lex_norm"] = max(c.get("lex_norm") or 0.0,
                                    (lex_max or 1.0) * 0.9)
                hits += 1
            tw["hits"] = hits

        # ---- canonical 権威フィルタ（post-filter re-check）＋ supply_admit
        items, omitted = [], {"scope_blocked": 0, "status_filtered": 0,
                              "sensitivity_blocked": 0}
        if ent_stats and ent_stats.get("capped"):
            omitted["link_expansion_capped"] = \
                omitted.get("link_expansion_capped", 0) \
                + ent_stats["capped"]
        ids = sorted(cand)
        rows = {}
        if ids:
            ph = ",".join("?" for _ in ids)
            for r in self.writer._conn.execute(
                    f"SELECT scope, record_id, state_json FROM records"
                    f" WHERE record_family='memory' AND record_id IN ({ph})",
                    ids):
                rows[r["record_id"]] = r
        lex_norm = lex_max or 1.0
        for rid in ids:
            row = rows.get(rid)
            if row is None:
                continue  # derived に残存するが canonical から消えた
            st = canon.loads(row["state_json"])
            ok, code = sup.supply_admit(st, scope=scope,
                                        visible_tiers=visible_tiers)
            if not ok:
                omitted[code] = omitted.get(code, 0) + 1
                continue
            c = cand[rid]
            strength = self.deriver(st) if self.deriver else \
                st.get("retrieval_strength")
            items.append({
                "record_id": rid, "scope": row["scope"], "state": st,
                "dense_sim": c.get("dense_sim"),
                "lexical": bool(c.get("lexical")),
                "temporal": bool(c.get("temporal")),
                "entity": bool(c.get("entity")),
                "entity_hop": c.get("entity_hop"),
                "retrieval_strength": strength,
                "score": sup.score_v1(
                    dense_sim=c.get("dense_sim"),
                    lex_norm=(c.get("lex_norm") or 0.0) / lex_norm
                    if c.get("lexical") else 0.0,
                    retrieval_strength=strength),
            })
        items.sort(key=lambda x: -x["score"])

        # 棄権判定は検索信号（dense/lexical）ベース —— CE 順位の影響を
        # 受けないので先に確定し、棄権時は CE 計算を省く
        reason = sup.abstain_reason(items[:k])

        # ---- C-3: cross-encoder 後段再採点（score-v1 上位プールの順位を
        # 精密モデルで組み替える。失敗時は score-v1 順のまま供給する）
        rerank_stats = None
        if reason is None and self.reranker is not None \
                and len(items) > 1:
            depth = int(os.getenv("RERANK_DEPTH", str(_RERANK_DEPTH)))
            pool = items[:max(1, depth)]
            try:
                texts = [(i["state"].get("content")
                          or i["state"].get("text") or "")
                         for i in pool]
                scores = self.reranker.rerank(query, texts)
                for it, s in zip(pool, scores):
                    it["rerank_score"] = float(s)
                pool.sort(key=lambda x: -x["rerank_score"])
                items[:len(pool)] = pool
                rerank_stats = {"model": self.reranker.model_stamp(),
                                "pool": len(pool)}
            except Exception:
                log.exception("canonical rerank failed; "
                              "keeping score-v1 order")

        # ---- recency reserve（rank で負けても最新記憶が必ず供給面に
        # 残る機械的保証。重み加点では枠を予約する —— 小さい重みは
        # dense に飲まれる degenerate-weight の教訓に倣う）
        reserve_items = []
        if reason is None:
            res_n = min(int(os.getenv("CANONICAL_RECENCY_RESERVE",
                                      str(_RECENCY_RESERVE))), k)
            if res_n > 0:
                have = {i["record_id"] for i in items[:k]}
                # 1) admit 済みだが rank 負けした残りから、記憶時刻の
                #    新しい順に（関連しかけた新しい記憶を優先）
                leftover = sorted(
                    (i for i in items if i["record_id"] not in have),
                    key=lambda i: (i["state"].get("valid_from") or
                                   i["state"].get("created_at") or ""),
                    reverse=True)
                for it in leftover[:res_n]:
                    it["recency_reserve"] = True
                    reserve_items.append(it)
                    have.add(it["record_id"])
                # 2) 候補プールが浅い時は canonical 直読で保証を埋める
                if len(reserve_items) < res_n:
                    for r in self.writer._conn.execute(
                            "SELECT state_json FROM records WHERE"
                            " record_family='memory' AND scope=?"
                            " ORDER BY COALESCE(valid_from, created_at)"
                            " DESC LIMIT ?",
                            (scope, res_n * 4 + len(have))):
                        if len(reserve_items) >= res_n:
                            break
                        st = canon.loads(r["state_json"])
                        rid = st.get("record_id")
                        if not rid or rid in have:
                            continue
                        ok, code = sup.supply_admit(
                            st, scope=scope, visible_tiers=visible_tiers)
                        if not ok:
                            omitted[code] = omitted.get(code, 0) + 1
                            continue
                        have.add(rid)
                        reserve_items.append({
                            "record_id": rid, "scope": scope,
                            "state": st, "score": 0.0,
                            "recency_reserve": True})

        stats = {"candidates": len(ids), "derived_gen": used_gen}
        if ent_stats:
            stats["entity"] = ent_stats
        if tw:
            stats["temporal"] = tw
        if rerank_stats:
            stats["rerank"] = rerank_stats
        if reserve_items:
            stats["recency_reserve"] = len(reserve_items)
        return {"items": (items[:k] + reserve_items) if reason is None
                else [],
                "abstain": reason, "omitted": omitted,
                "stats": stats}

    def _structural(self, scope: str):
        """identity/usermodel/state の構造参照（毎回安定供給・検索しない）。
        supply_admit は per-item で通す。"""
        out, omitted = [], {"scope_blocked": 0, "status_filtered": 0,
                            "sensitivity_blocked": 0}
        for r in self.writer._conn.execute(
                "SELECT scope, record_id, record_family, state_json"
                " FROM records WHERE scope=? AND record_family IN"
                " ('identity','usermodel','state')", (scope,)):
            st = canon.loads(r["state_json"])
            ok, code = sup.supply_admit(st, scope=scope)
            if not ok:
                omitted[code] = omitted.get(code, 0) + 1
                continue
            out.append({"type": r["record_family"], "id": r["record_id"],
                        "state": st})
        return out, omitted

    # ------------------------------------------------------ supply（唯一の供給面）
    def supply_context(self, *, scopes, query=None, purpose="turn",
                       consumer="engine", k=None, correlation=None,
                       via="live"):
        """会話組み立て用の供給。context_assembly receipt を必ず刻んでから
        blocks を返す（receipt-before-response —— SD-11）。

        scopes: 明示必須（省略不可 —— D-E3）。query が None なら構造参照のみ。
        """
        if not scopes:
            raise ValueError("scope is required (no implicit all-scope read)")
        if purpose not in sup.PURPOSES:
            raise ValueError(f"purpose must be one of {sorted(sup.PURPOSES)}")
        k = k or sup.RECALL_TOPK
        # deep_recall は全 tier を供給対象にする（cold/archive は blurred
        # 供給形として印を付ける —— FA §6.2 の層語彙）
        visible = (sup.ALL_TIERS if purpose == "deep_recall"
                   else sup.DEFAULT_VISIBLE_TIERS)
        now = self._clock()
        with self.writer.transaction() as conn:
            head = conn.execute(
                "SELECT MAX(seq) FROM events").fetchone()[0] or 0
            gen_row = conn.execute(
                "SELECT gen FROM derived_generations WHERE is_active=1"
            ).fetchone()
            pending_debt = conn.execute(
                "SELECT COUNT(*), MIN(created_at) FROM candidates"
                " WHERE status='pending'").fetchone()

        blocks, injected, omitted = [], [], {}
        abstains = {}
        for scope in scopes:
            sblocks, somitted = self._structural(scope)
            for c, v in somitted.items():
                omitted[c] = omitted.get(c, 0) + v
            for b in sblocks:
                blocks.append({"type": b["type"], "id": b["id"],
                               "state": b["state"], "scope": scope})
                injected.append({"type": b["type"], "id": b["id"]})
            rec = self._recall(scope, query or "", k,
                               visible_tiers=visible) if query else \
                {"items": [], "abstain": None, "omitted": {},
                 "stats": {}}
            for c, v in rec["omitted"].items():
                omitted[c] = omitted.get(c, 0) + v
            for item in rec["items"]:
                blk = {"type": "memory", "id": item["record_id"],
                       "state": item["state"], "scope": scope,
                       "score": item["score"]}
                if (item["state"].get("tier") or "hot") in \
                        ("cold", "archive"):
                    blk["blurred"] = True
                blocks.append(blk)
                injected.append({"type": "memory", "id": item["record_id"]})
            if rec["abstain"]:
                abstains[scope] = rec["abstain"]

        # inputs_hash: 判定入力（scopes/query/purpose/head/gen）の pin
        inputs_hash = canon.hash_obj(
            "pcore.supply_inputs.v1",
            {"scopes": scopes, "query": query, "purpose": purpose,
             "head_seq": head, "gen": gen_row["gen"] if gen_row else None})
        payload = sup.assembly_payload(
            injected=injected, omitted=omitted, purpose=purpose,
            consumer=consumer, basis_seq=head,
            canon_algo="canon-json-v1",
            derived_gen=gen_row["gen"] if gen_row else None,
            via=via, scopes=scopes, query=query,
            inputs_hash=inputs_hash,
            pending_debt={
                "count": pending_debt[0] or 0,
                "oldest": pending_debt[1]} if pending_debt else None)
        receipt = self.writer.append_event(
            "context_assembly", payload, actor="system",
            correlation=correlation)
        return {"blocks": blocks, "abstain": abstains or None,
                "omitted": omitted, "receipt": receipt,
                "inputs_hash": inputs_hash,
                "basis_seq": head}

    # ------------------------------------------------ 監査・管理読み（非供給）
    def inspect(self, scope: str, record_id: str):
        """ID 直参照（全 status 見える。供給経路ではないので receipt 無し）。"""
        row = self.writer._conn.execute(
            "SELECT state_json FROM records WHERE scope=? AND record_id=?",
            (scope, record_id)).fetchone()
        return canon.loads(row["state_json"]) if row else None

    def browse(self, scope: str, *, family=None, status=None,
               limit: int = 100):
        """一覧列挙（監査用・非供給経路）。"""
        sql = ("SELECT record_id, record_family, status, state_json"
               " FROM records WHERE scope=?")
        args = [scope]
        if family:
            sql += " AND record_family=?"
            args.append(family)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        return [dict(record_id=r["record_id"], family=r["record_family"],
                     status=r["status"], state=canon.loads(r["state_json"]))
                for r in self.writer._conn.execute(sql, args)]
