"""会話履歴検索の CLI 口。

    python -m companion.history_search "検索語" [--profile X] [--limit N]

turns 表の発言原文を部分一致で引く（新しい順）。--profile で特定 bot の
会話に絞れる（省略時は全件）。
"""
import argparse

from . import db


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="python -m companion.history_search",
        description="turns 表の会話履歴を部分一致で検索（新しい順）",
    )
    ap.add_argument("query", help="検索語（発言原文の一部）")
    ap.add_argument("--profile", help="Hermes profile 名で絞る（省略時は全件）")
    ap.add_argument("--limit", type=int, default=20, help="最大件数（既定20）")
    args = ap.parse_args()

    rows = db.search_turns(args.query, profile=args.profile, limit=args.limit)
    if not rows:
        print("見つかりませんでした")
        return
    for tid, skey, utext, atext, ts, source, prof in rows:
        prof_s = f" @{prof}" if prof else ""
        print(f"[#{tid} {ts or ''}{prof_s} {source or ''}]")
        print(f"  相手> {(utext or '')[:200]}")
        print(f"  私  > {(atext or '')[:200]}")


if __name__ == "__main__":
    main()
