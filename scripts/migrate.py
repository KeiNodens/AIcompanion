#!/usr/bin/env python
"""scripts/migrate.py —— L-MIG 運用 CLI（mem0→canonical 切替工程）。

使い方（リポジトリ直下から）:

    python scripts/migrate.py inventory          # 移行元目録の件数を提示
    python scripts/migrate.py backup             # data/ 重要物のバックアップ
    python scripts/migrate.py authority <mode>   # state.json 権威旗の表示/設定
    python scripts/migrate.py stage              # staged transcription 実行
    python scripts/migrate.py verify             # 機械検証14項目の実行
    python scripts/migrate.py reconcile          # 3面突合レポート
    python scripts/migrate.py g1                 # G1 投影一致レポート
    python scripts/migrate.py cutover            # 権威移動＋旧系凍結（要確認）
    python scripts/migrate.py rollback           # 権威旗を mem0 に戻す
    python scripts/migrate.py status             # deliver_state 集計

cutover 前は「完全停止」（bot・scheduler を止めた状態）が前提。
stage/commit は canonical core.db への刻印で、再実行は import_key 冪等。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))

from companion import config, db                    # noqa: E402
from companion.kernel.writer import open_core       # noqa: E402


def _p(msg):
    print(msg, flush=True)


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _writer():
    path = config.DATA_DIR / "core.db"
    if not path.exists():
        _p(f"[!] {path} がまだありません。"
           " canonical lane（CANONICAL_RECORD=1）を一度動かして作成するか、"
           "先に初期化してください")
        sys.exit(2)
    return open_core(path)


def _mem0():
    """mem0 seam（本番接続。失敗したら None で戻す）。"""
    try:
        from companion import memory
        return memory.memory
    except Exception as e:
        _p(f"[!] mem0 接続なし: {e}")
        return None


def _inventory(mem0=None):
    from companion.adapters.srcinv import collect_inventory
    from companion.adapters.transcribe import Transcriber
    raw = collect_inventory(
        mem0=mem0, user_id=config.MEMORY_USER,
        agent_id=config.MEMORY_AGENT,
        data_dir=config.DATA_DIR, db_path=config.DB_PATH,
        persona_md=ROOT / "persona.md",
        persona_notes=config.DATA_DIR / "persona_notes.md",
        state_path=config.STATE_PATH)
    tr = Transcriber(_writer())
    return tr.build_inventory(**raw)


# ------------------------------------------------------------------ cmds
def cmd_inventory(_a):
    _p("[*] 移行元目録を集めます（mem0 接続を含む）…")
    inv = _inventory(mem0=_mem0())
    by = {}
    for r in inv:
        by[r["source_table"]] = by.get(r["source_table"], 0) + 1
    _p(json.dumps({"total": len(inv), "by_source": by},
                  ensure_ascii=False, indent=2))


def cmd_backup(_a):
    dst = config.DATA_DIR / "backups" / f"pre-lmig-{_now_tag()}"
    dst.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("companion.db", "history.db", "state.json",
                 "core.db", "persona_notes.md", "recent_context.md",
                 "user_profile.md", "tool_log.jsonl"):
        src = config.DATA_DIR / name
        if src.exists():
            shutil.copy2(src, dst / name)
            copied.append(name)
    qd = config.DATA_DIR / "qdrant"
    if qd.exists():
        shutil.copytree(qd, dst / "qdrant")
        copied.append("qdrant/")
    _p(f"[+] backup -> {dst}  ({len(copied)} items)")
    # rollback リハーサル用: バックアップからの復元手順を添える
    (dst / "RESTORE.md").write_text(
        "# rollback 手順\n\n"
        "1. bot を停止する\n"
        "2. このディレクトリの各ファイルを data/ へ上書きコピーする\n"
        "   （qdrant/ は data/qdrant を置き換え）\n"
        "3. state.json の memory_authority を \"mem0\" に戻す\n"
        "   （`python scripts/migrate.py authority mem0`）\n"
        "4. bot を再起動する\n",
        encoding="utf-8")
    _p("    RESTORE.md を同梱しました（rollback リハーサル資料）")


def cmd_authority(a):
    cur = db.load_state().get("memory_authority") or "mem0"
    if not a.mode:
        _p(f"memory_authority = {cur}")
        st = db.load_state()
        for k in ("cutover_at", "mem0_grace_until", "legacy_frozen_at",
                  "rolled_back_at"):
            if st.get(k):
                _p(f"  {k} = {st[k]}")
        return
    if a.mode not in ("mem0", "dual_run", "canonical"):
        _p(f"[!] mode は mem0|dual_run|canonical: {a.mode}")
        sys.exit(2)
    db.update_state(memory_authority=a.mode)
    _p(f"[+] memory_authority: {cur} -> {a.mode}")


def cmd_stage(a):
    from companion.adapters.transcribe import Transcriber
    run_id = a.run_id or f"lmig-{_now_tag()}"
    _p(f"[*] stage run: {run_id}")
    w = _writer()
    tr = Transcriber(w)
    inv = _inventory(mem0=_mem0())
    _p(f"    inventory: {len(inv)} rows")
    counts = tr.stage(inv, run_id)
    _p(json.dumps({"run_id": run_id, "staged": counts},
                  ensure_ascii=False, indent=2))
    if a.commit:
        res = tr.commit(run_id)
        _p(json.dumps({"committed": res}, ensure_ascii=False, indent=2))


def cmd_verify(a):
    from companion.adapters.migverify import MigVerifier
    w = _writer()
    # mem0 原本の内容が content_equiv / 重複 baseline の照合に必要
    inv = _inventory(mem0=_mem0())
    res = MigVerifier(w).machine_checks(inv)
    for c in res["checks"]:
        _p(f"  [{'ok' if c['ok'] else 'NG'}] {c['name']}: {c['detail']}")
    _p(json.dumps({"ok": res["ok"], "failed": res["failed"]},
                  ensure_ascii=False))


def cmd_reconcile(_a):
    from companion.adapters.migration import Reconciler
    inv = _inventory(mem0=None)
    res = Reconciler(_writer()).reconcile(inv)
    _p(json.dumps(res["counts"], ensure_ascii=False, indent=2))
    for key in ("missing", "failed", "no_target_row"):
        for e in res[key][:10]:
            _p(f"  {key}: {e['source_table']}#{e['source_id']}")


def cmd_g1(_a):
    from companion.adapters.dualrun import DualRun
    w = _writer()
    dr = DualRun(w, extraction=None, admission=None)
    mem0 = _mem0()
    if mem0 is None:
        _p("[!] mem0 が開けません")
        sys.exit(2)
    from companion.adapters.srcinv import collect_mem0_items
    for scope in ("user", "agent"):
        items = collect_mem0_items(
            mem0, user_id=config.MEMORY_USER,
            agent_id=config.MEMORY_AGENT)
        items = [it for it in items
                 if (it.get("metadata") or {}).get("agent_id")
                 is not None] if scope == "agent" else \
                [it for it in items
                 if (it.get("metadata") or {}).get("agent_id") is None]
        _p(json.dumps(dr.g1_report(items, scope),
                      ensure_ascii=False, indent=2))


def cmd_cutover(a):
    from companion.adapters.cutover import Cutover
    w = _writer()
    cur = db.load_state().get("memory_authority") or "mem0"
    if cur == "canonical" and not a.force:
        _p("[!] 既に canonical 権威です（--force で再刻印）")
        sys.exit(2)
    if not a.yes:
        _p("[!] cutover は権威移動です。--yes を付けて再実行してください"
           "（事前に verify が全緑であること）")
        sys.exit(2)
    co = Cutover(w)
    res = co.perform(from_mode=cur, run_id=a.run_id,
                     grace_days=a.grace_days)
    _p(json.dumps(res, ensure_ascii=False, indent=2))
    if a.freeze:
        _p(json.dumps(co.freeze_legacy(data_dir=config.DATA_DIR),
                      ensure_ascii=False, indent=2))


def cmd_gates(a):
    """G1〜G5 の総合評価。cutover 前の最終ゲート確認。"""
    import tempfile
    from companion.adapters.migration import Reconciler, EquivGate
    from companion.adapters.migverify import MigVerifier
    from companion.adapters.dualrun import DualRun
    from companion.adapters.derived import DerivedStore
    from companion.adapters.embedder import Fastembedder
    from companion.adapters.supply import Supply
    from companion.adapters import srcinv

    w = _writer()
    mem0 = _mem0()

    _p("[*] derived index を再構築（実モデル embedder）…")
    ds = DerivedStore(w)
    rb = ds.rebuild(embedder=Fastembedder())
    _p(f"    gen={rb['gen']} verify={rb['verify']} active={rb['active']}")
    if not rb["active"]:
        _p("[!] derived build 失敗 —— 以降のゲートを中止")
        return

    inv = _inventory(mem0=mem0)
    ver = MigVerifier(w)

    # G5 旧資産転写
    g5 = Reconciler(w).reconcile(inv)
    _p(f"[G5] reconcile: {g5['counts']}")

    # G2 provenance 被覆（機械検証の内）
    mc = ver.machine_checks(inv)
    g2 = {"ok": mc["ok"], "failed": mc["failed"]}
    _p(f"[G2] machine_checks: ok={g2['ok']} failed={g2['failed']}")

    # G1 投影一致（dual-run 比較）
    dr = DualRun(w, extraction=None, admission=None)
    g1s = []
    if mem0 is not None:
        items = srcinv.collect_mem0_items(
            mem0, user_id=config.MEMORY_USER, agent_id=config.MEMORY_AGENT)
        for scope in ("user", "agent"):
            sub = [it for it in items
                   if (it.get("metadata") or {}).get("agent_id")
                   is not None] if scope == "agent" else \
                  [it for it in items
                   if (it.get("metadata") or {}).get("agent_id") is None]
            g1s.append(dr.g1_report(sub, scope))
    g1 = {"ok": all(r["ok"] for r in g1s) if g1s else False,
          "scopes": g1s}
    for r in g1s:
        _p(f"[G1] {r['scope']}: coverage={r['coverage']}"
           f" ({r['covered']}/{r['mem0_total']})")

    # G3 意味的同等（代表クエリを新旧両系へ）
    queries = [
        {"id": "q-music", "text": "好きな音楽・アーティストは？",
         "scope": "user"},
        {"id": "q-weather", "text": "東京の天気を教えて", "scope": "user"},
        {"id": "q-pref", "text": "マスターの好みや要望は？", "scope": "user"},
        {"id": "q-reflect", "text": "振り返りで気づいたこと",
         "scope": "agent"},
        {"id": "q-self", "text": "自分はどんな存在か", "scope": "agent"},
        {"id": "q-hermes", "text": "Hermes や body との連携",
         "scope": "agent"},
    ]
    sp = Supply(w, ds, embedder=Fastembedder())

    def old_recall(q):
        flt = ({"agent_id": config.MEMORY_AGENT} if q["scope"] == "agent"
               else {"user_id": config.MEMORY_USER})
        res = mem0.search(q["text"], top_k=10, filters=flt)
        return res.get("results") if isinstance(res, dict) else res

    def new_recall(q):
        out = sp.supply_context(scopes=[q["scope"]], query=q["text"],
                                purpose="dual_run", consumer="mig-gates")
        return [b["state"] for b in out["blocks"] if b["type"] == "memory"]

    g3 = EquivGate(coverage_floor=0.6).evaluate(
        queries, old_recall, new_recall) if mem0 is not None else \
        {"ok": False, "detail": "mem0 接続なし"}
    _p(f"[G3] equiv gate: ok={g3['ok']} failed={g3.get('failed')}")
    for r in g3.get("results", []):
        _p(f"      {r['query_id']}: coverage={r['coverage']:.2f}"
           f" ok={r['ok']} missed={r['missed'][:3]}")

    # G4 export 往復
    with tempfile.TemporaryDirectory(prefix="lmig-g4-") as td:
        from companion.adapters.packio import PackIO
        io1 = PackIO(w)
        pack_dir = os.path.join(td, "pack")
        io1.export_pack(pack_dir)
        w2 = open_core(os.path.join(td, "dst-core.db"))
        try:
            io2 = PackIO(w2)
            run = io2.stage(pack_dir)
            ta = io2.test_apply(run["run_id"])
            if ta["ok"]:
                io2.commit(run["run_id"], pack_dir)
                g4 = io2.validate_pack(pack_dir)
            else:
                g4 = {"ok": False, "detail": "test_apply failed",
                      "report": ta}
        finally:
            w2.close()
    _p(f"[G4] round-trip: ok={g4.get('ok')}")

    rep = ver.gate_report(g1=g1, g2=g2, g3=g3, g4=g4, g5=g5)
    _p(json.dumps(rep, ensure_ascii=False, indent=2))


def cmd_rollback(a):
    from companion.adapters.cutover import Cutover
    res = Cutover(_writer()).rollback(reason=a.reason)
    _p(json.dumps(res, ensure_ascii=False, indent=2))
    _p("    ※ data/ の戻しは backups/<pre-lmig-*>/RESTORE.md の手順で")


def cmd_status(_a):
    from companion.adapters.migration import DeliverState
    w = _writer()
    with w.transaction() as conn:
        _p(json.dumps(DeliverState(w).counts(conn),
                      ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="L-MIG 運用 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory")
    sub.add_parser("backup")
    p = sub.add_parser("authority")
    p.add_argument("mode", nargs="?")
    p = sub.add_parser("stage")
    p.add_argument("--run-id")
    p.add_argument("--commit", action="store_true",
                   help="stage 直後に commit まで行う")
    sub.add_parser("verify")
    sub.add_parser("reconcile")
    sub.add_parser("g1")
    sub.add_parser("gates")
    p = sub.add_parser("cutover")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--freeze", action="store_true",
                   help="旧系を filesystem read-only に凍結")
    p.add_argument("--run-id")
    p.add_argument("--grace-days", type=int, default=14)
    p = sub.add_parser("rollback")
    p.add_argument("--reason", default="operator")
    sub.add_parser("status")
    a = ap.parse_args()
    globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    main()
