""":8788 が配信するローカル小窓ページ（旧 desktop.py の軽量置換）。

server.py の GET /ui が companion/web/ui.html をそのまま返す。
ページ自体に秘密は無く、データ取得はすべて /v1 系の鍵付きAPIに
ブラウザから直接投げる（入口鍵はページ上で1回入力→localStorage）。
ファイルはリクエストごとに読む（ローカル面なので量は知れている。
編集が即反映されてデバッグが楽）。
"""
from pathlib import Path

PAGE_PATH = Path(__file__).parent / "web" / "ui.html"


def page() -> str:
    return PAGE_PATH.read_text(encoding="utf-8")
