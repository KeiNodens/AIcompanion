"""body 側との輸送層エラーの共有定義（codex-body.md）。

backend（hermes/codex）が共通で投げる例外型。呼び出し側
（runtime/body.py・tools/body.py・bodyruns.py）は backend を意識せず
この2種だけを見ればよい。

status_code の慣習は Hermes の HTTP 由来:
- 404 … run が身体側に見つからない（lost 判定に使う）
- 409 … 承認待ちでない/期限切れ・その状態では受け付けない操作
app-server (JSON-RPC) では error.code をそのまま写す
"""


class BodyUnreachableError(Exception):
    """body 側に到達できない（未起動・接続不能・起動失敗）"""


class BodyAPIError(Exception):
    """body 側がエラーや想定外の応答を返した。

    status_code に HTTP ステータス or JSON-RPC error.code が入る。
    404=run が見つからない、409=承認待ちでない/操作を受け付けない状態。
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
