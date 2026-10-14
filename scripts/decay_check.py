"""既存の自己記憶に対する減衰ポリシーの影響を試算する。

既定は読み取り専用。qdrant を直接開いて payload を読み、
_memory._decay_targets と同じ判定で「いつ・どれだけ減衰するか」を表示する。

  python scripts/decay_check.py          — 試算のみ（本番に書き込まない）
  python scripts/decay_check.py --apply  — policy_started_at を既存記憶に刻印する
                                   （bot停止中に実行。要バックアップ）
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion.config import DATA_DIR, MEMORY_AGENT  # noqa: E402
from companion.memory import _decay_targets  # noqa: E402  （純粋関数）

_BUILTIN_KEYS = {
    "data", "hash", "created_at", "updated_at",
    "user_id", "agent_id", "actor_id", "role", "run_id", "name",
}


def fetch_self_memories() -> list[dict]:
    """agent scope の記憶を qdrant payload から直接読む（埋め込みモデル不要）"""
    from qdrant_client import QdrantClient

    client = QdrantClient(path=str(DATA_DIR / "qdrant"))
    items = []
    offset = None
    while True:
        points, offset = client.scroll(
            "companion", limit=1000, offset=offset, with_payload=True
        )
        for p in points:
            pl = p.payload or {}
            if pl.get("agent_id") != MEMORY_AGENT:
                continue
            meta = {k: v for k, v in pl.items() if k not in _BUILTIN_KEYS}
            items.append({
                "id": str(p.id),
                "memory": pl.get("data"),
                "metadata": meta,
                "created_at": pl.get("created_at"),
            })
        if offset is None:
            break
    return items


def _parse_dt(v):
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _conf_at(conf, imp, idle_days):
    """idle_days 経過後の confidence 予測値"""
    grace = max(7.0, imp * 7.0)
    if idle_days <= grace:
        return conf
    over = idle_days - grace
    return max(0.05, round(conf * (0.9 ** (over / 7.0)), 3))


def main() -> None:
    now = datetime.now(timezone.utc)
    items = fetch_self_memories()
    print(f"自己記憶（agent={MEMORY_AGENT}）: {len(items)} 件")
    if not items:
        return

    # --- 現在の記憶の内訳 ---
    has_recall = has_stamp = fresh = 0
    for it in items:
        m = it.get("metadata") or {}
        if m.get("last_recalled_at"):
            has_recall += 1
        elif m.get("policy_started_at"):
            has_stamp += 1
        else:
            fresh += 1
    print(f"  last_recalled_at あり: {has_recall} / policy_started_at のみ: {has_stamp}"
          f" / どちらも無し(移行対象): {fresh}")

    # --- シナリオA: policy_started_at=今日 で刻印した場合 ---
    migrated = []
    for it in items:
        m = dict(it.get("metadata") or {})
        if not m.get("last_recalled_at") and not m.get("policy_started_at"):
            m["policy_started_at"] = now.isoformat()
        migrated.append({**it, "metadata": m})
    now_targets = _decay_targets(migrated, now)
    print(f"\n[A] 移行(起点=今日)を想定: 今日すぐ減衰する記憶 … {len(now_targets)} 件")

    # 各記憶の減衰開始日と +30/90日時点の予測confidence
    rows = []
    for it in migrated:
        m = it["metadata"]
        try:
            imp = float(m.get("importance") or 3)
            conf = float(m.get("confidence") or 0.5)
        except (TypeError, ValueError):
            continue
        anchor = _parse_dt(m.get("last_recalled_at")) or _parse_dt(m.get("policy_started_at"))
        grace = max(7.0, imp * 7.0)
        first_decay = (anchor + timedelta(days=grace)).date() if anchor else "対象外"
        if anchor:
            elapsed = max(0.0, (now - anchor).total_seconds() / 86400.0)
            c30 = _conf_at(conf, imp, elapsed + 30)
            c90 = _conf_at(conf, imp, elapsed + 90)
        else:
            c30 = c90 = conf
        rows.append((str(it["memory"])[:36], imp, conf, first_decay, c30, c90))
    rows.sort(key=lambda r: (str(r[3]), -r[1]))
    print(f"\n{'記憶':<38} {'imp':>3} {'conf':>5} {'減衰開始':>10} {'30d後':>6} {'90d後':>6}")
    for r in rows[:40]:
        print(f"{r[0]:<38} {r[1]:>3.0f} {r[2]:>5.2f} {str(r[3]):>10} {r[4]:>6.2f} {r[5]:>6.2f}")
    if len(rows) > 40:
        print(f"  …他 {len(rows) - 40} 件")

    # --- シナリオB: 誤って created_at を起点にした場合（対比） ---
    naive = []
    for it in items:
        m = dict(it.get("metadata") or {})
        if not m.get("last_recalled_at") and not m.get("policy_started_at"):
            m["last_recalled_at"] = it.get("created_at")
        naive.append({**it, "metadata": m})
    naive_targets = _decay_targets(naive, now)
    print(f"\n[B] 参考: 作成日を起点にした場合、今日減衰する記憶 … {len(naive_targets)} 件")
    for t in naive_targets[:10]:
        print(f"  {str(t['memory'])[:40]} conf {t['confidence']} → {t['new_confidence']}"
              f" (idle {t['idle_days']}日)")
    if len(naive_targets) > 10:
        print(f"  …他 {len(naive_targets) - 10} 件")

    if "--apply" in sys.argv:
        from companion.memory import migrate_decay_policy
        print("\n--apply: policy_started_at を刻印します")
        res = migrate_decay_policy()
        print(f"  刻印: {res['stamped']} / 全体: {res['total']}")
    else:
        print("\n（適用は未実行。刻印するには bot 停止中に --apply を付けて実行）")


if __name__ == "__main__":
    main()
