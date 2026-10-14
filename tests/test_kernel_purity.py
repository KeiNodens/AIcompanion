"""kernel 純度の AST lint（P8-1・C-KER 多層強制の lint 層）

companion/kernel/ 配下で、writer.py 以外のモジュールが
sqlite3・ファイル/ネットワーク IO・壁時計・乱数・LLM 系に直接到達することを
機械的に拒否する。writer.py は唯一の不純端。

限界（phase8-plan §10 記載どおり）: import 間接化・getattr 迂回・
文字列 eval 等、AST で拾えない迂回は残る。conformance suite（P8-5）と
review で補う前提の lint であり、これだけで純度を完全には証明しない。
"""
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

KERNEL_DIR = _support.PKG_DIR / "kernel"

# writer.py 以外の kernel モジュールで禁止する import
FORBIDDEN_MODULE_IMPORTS = {
    # 永続化・DB 直入
    "sqlite3", "dbm", "shelve", "pickle",
    # ファイル・OS・プロセス
    "os", "io", "pathlib", "shutil", "subprocess", "ctypes", "mmap",
    "tempfile", "glob", "fnmatch",
    # ネット・LLM・外部 SDK
    "socket", "ssl", "http", "urllib", "requests", "httpx",
    "aiohttp", "websocket", "websockets", "openai", "anthropic",
    "mem0", "qdrant_client",
    # 時計・乱数・並行
    "time", "random", "secrets", "uuid", "asyncio", "threading",
    "multiprocessing", "concurrent", "signal", "sched",
    # 環境・動的実行
    "sys", "importlib", "runpy", "code", "codeop", "pty",
    "logging", "traceback", "inspect", "builtins", "site",
}

# kernel 内のどのファイルでも禁止する import（writer の不純端権限でも不可）
FORBIDDEN_ALWAYS = {
    "socket", "ssl", "http", "urllib", "requests", "httpx",
    "aiohttp", "websocket", "websockets", "openai", "anthropic",
    "mem0", "qdrant_client", "subprocess", "ctypes", "pty",
    "mmap", "signal", "multiprocessing",
}

# 禁止する「裸の名前」呼び出し（builtin 動的実行・IO —— ast.Name のみ。
# re.compile 等の属性呼び出しは FORBIDDEN_ATTR_CALLS 側が担う）
FORBIDDEN_CALLS = {
    "open", "eval", "exec", "compile", "__import__", "input",
    "breakpoint", "exit", "quit", "print",
}
FORBIDDEN_ATTR_CALLS = {
    "now", "utcnow", "today", "fromtimestamp", "gettimeofday",
    "monotonic", "perf_counter", "process_time", "sleep",
    "urandom", "getrandbits", "randbits", "randrange", "randint", "random",
    "choice", "choices", "shuffle", "sample",
    "getenv", "environ", "system", "popen", "listdir", "getcwd",
    "connect", "attach", "detach", "set_authorizer",
    "read_text", "read_bytes", "write_text", "write_bytes",
    "mkdir", "unlink", "rmdir", "touch",
}

# kernel 内で許容する唯一の不純端モジュール
IMPURE_EDGE = {"writer.py"}


def _call_name(node):
    """Call ノードの呼出名（名前 or 属性名）を返す。"""
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def lint_file(path: Path) -> list:
    """1ファイルの違反リストを返す。空=合格。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    is_edge = path.name in IMPURE_EDGE
    violations = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            else:
                mods = [(node.module or "").split(".")[0]]
            for m in mods:
                if m in FORBIDDEN_ALWAYS:
                    violations.append(
                        f"{path.name}:{node.lineno} forbidden import {m}")
                elif not is_edge and m in FORBIDDEN_MODULE_IMPORTS:
                    violations.append(
                        f"{path.name}:{node.lineno} import {m} outside"
                        " writer.py")
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name is None:
                continue
            if not is_edge and isinstance(node.func, ast.Name) \
                    and name in FORBIDDEN_CALLS:
                violations.append(
                    f"{path.name}:{node.lineno} call {name}() outside"
                    " writer.py")
            elif not is_edge and isinstance(node.func, ast.Attribute) \
                    and name in FORBIDDEN_ATTR_CALLS:
                violations.append(
                    f"{path.name}:{node.lineno} attr call .{name}() outside"
                    " writer.py")
    return violations


class KernelPurityTest(unittest.TestCase):
    def test_kernel_files_pure(self):
        files = sorted(KERNEL_DIR.glob("*.py"))
        self.assertTrue(files, "kernel/*.py not found")
        all_v = []
        for f in files:
            all_v.extend(lint_file(f))
        self.assertEqual(all_v, [])

    def test_lint_detects_violation(self):
        src = KERNEL_DIR / "_lint_probe.py"
        src.write_text("import sqlite3\nopen('x')\n")
        try:
            v = lint_file(src)
            self.assertTrue(any("sqlite3" in x for x in v))
            self.assertTrue(any("open()" in x for x in v))
        finally:
            src.unlink()


if __name__ == "__main__":
    unittest.main()
