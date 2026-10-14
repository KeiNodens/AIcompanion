"""Codex body（codex app-server）の設定 lint＋疎通点検
（research/design/codex-body.md Phase 3c）。

`python -m companion.codex_check` で一覧表示する。app-server の実 spawn
（initialize 往復まで）を試すので、これが通れば body_task backend=codex
の輸送面は生きている。

秘密値は finding の文言に絶対に含めない。
"""
import dataclasses
import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from .runtime.errors import BodyAPIError, BodyUnreachableError


@dataclasses.dataclass
class Finding:
    level: str   # "error" | "warn" | "info"
    key: str     # 対象の config キー（ドット区切り）
    message: str


def check(probe: bool = True) -> list[Finding]:
    out: list[Finding] = []

    def add(level: str, key: str, message: str) -> None:
        out.append(Finding(level, key, message))

    # 1. 有効化フラグ
    if not config.CODEX_BODY_ENABLED:
        add("info", "CODEX_BODY_ENABLED",
            "codex body は無効（backend=codex の依頼は到達不可になる）")
        return out

    # 2. codex 実行ファイル
    cmd = config.CODEX_APP_SERVER_CMD or "codex"
    exe = shutil.which(cmd) or (cmd if Path(cmd).exists() else None)
    if not exe:
        add("error", "CODEX_APP_SERVER_CMD",
            f"codex 実行ファイルが見つからない（'{cmd}'。PATH か絶対パスを"
            "CODEX_APP_SERVER_CMD に設定）")
        return out

    # 3. バージョン（protocol pin の確認材料）
    try:
        ver = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=15,
        )
        v = (ver.stdout or ver.stderr or "").strip().splitlines()[0:1]
        add("info", "codex --version", v[0] if v else "取得できず")
    except Exception as e:
        add("warn", "codex --version", f"バージョン取得に失敗: {e}")

    # 4. 作業ディレクトリ
    if not Path(config.CODEX_BODY_CWD).is_dir():
        add("error", "CODEX_BODY_CWD",
            f"作業ディレクトリが存在しない: {config.CODEX_BODY_CWD}")

    # 5. 承認ポリシー・サンドボックスの値
    pol = (config.CODEX_BODY_APPROVAL_POLICY or "").strip()
    if pol not in ("untrusted", "on-request", "never"):
        add("error", "CODEX_BODY_APPROVAL_POLICY",
            f"値 '{pol}' は無効（untrusted/on-request/never のどれか）")
    elif pol == "never":
        add("warn", "CODEX_BODY_APPROVAL_POLICY",
            "'never' は身体側の承認要求を全て塞ぐ（run が止まりやすい。"
            "Core 4桁決裁に乗せるなら 'on-request' 推奨）")
    sb = (config.CODEX_BODY_SANDBOX or "").strip()
    if sb not in ("read-only", "workspace-write", "danger-full-access"):
        add("error", "CODEX_BODY_SANDBOX",
            f"値 '{sb}' は無効（read-only/workspace-write/"
            "danger-full-access のどれか）")
    elif sb == "danger-full-access":
        add("warn", "CODEX_BODY_SANDBOX",
            "'danger-full-access' は書込み・ネットワークを全面解放する"
            "（承認ゲートを通るとはいえ危険度が高い）")

    # 6. Codex→Core 方向（/mcp 面）の鍵。無いと core_* ツールが繋がらない
    if not config.COMPANION_MCP_KEY:
        add("info", "COMPANION_MCP_KEY",
            "未設定（/mcp 面が 404 になる。codex から core_context/"
            "core_report/core_skill_read を使うには要設定）")

    # 7. app-server の実疎通（spawn → initialize → 終了）
    if probe:
        try:
            from .runtime import codex
            res = codex.health()
            ua = res.get("userAgent") or res.get("serverInfo") or {}
            add("info", "app-server",
                f"initialize 疎通 OK（userAgent: {ua or '不明'}）")
        except BodyUnreachableError as e:
            add("error", "app-server", f"起動に失敗: {e}")
        except BodyAPIError as e:
            add("error", "app-server", f"initialize 応答が異常: {e}")
        except Exception as e:
            add("error", "app-server", f"疎通で予期しない失敗: {e}")
        finally:
            try:
                from .runtime import codex
                codex.shutdown()
            except Exception:
                pass

    return out


def main() -> None:
    """`python -m companion.codex_check [--no-probe]`"""
    probe = "--no-probe" not in sys.argv
    findings = check(probe=probe)
    counts = {"error": 0, "warn": 0, "info": 0}
    for f in findings:
        counts[f.level] = counts.get(f.level, 0) + 1
        print(f"[{f.level}] {f.key}: {f.message}")
    if not findings:
        print("findings なし")
    print(
        f"--- error {counts['error']} / warn {counts['warn']} "
        f"/ info {counts['info']}"
    )
    sys.exit(1 if counts["error"] else 0)


if __name__ == "__main__":
    main()
