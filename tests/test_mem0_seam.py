"""test_mem0_seam —— L-MIG Phase 1 の CI grep lint。

`memory.py` が mem0 呼出しの唯一の通過点であることを静的に担保する:

- `mem0` パッケージの import / `Memory(` 構築は memory.py 限定
- `memory` シングルトンへの素の API 呼出し
  （.add/.search/.get_all/.update/.delete/.get）は memory.py 限定
  —— 他モジュールは公開 wrapper（recall/store_turn/all_user/
  add_user_fact/…）を通す。wrapper が authority を見て canonical に
  切替えるので、素呼び出しがあると canonical 権威を素通りする

除外: companion/memory.py 本体・tests/・__pycache__。
dualrun.store_verbatim は self.mem0（注入 seam）経由なので
`memory` 名への属性呼出しではなく対象外 —— seam 経路は変わらない。
"""

import ast
import io
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "companion")
SEAM_FILE = os.path.join(PKG, "memory.py")

_FORBIDDEN_ATTRS = {"add", "search", "get_all", "update", "delete", "get"}
_FORBIDDEN_CTOR = {"Memory", "Mem0", "AsyncMemory"}
_ALLOW = {SEAM_FILE}


def _iter_py(base):
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _violations(path):
    with io.open(path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=path)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "mem0" or a.name.startswith("mem0."):
                    out.append((node.lineno, f"import {a.name}"))
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "") == "mem0" or \
                    (node.module or "").startswith("mem0."):
                out.append((node.lineno, f"from {node.module} import ..."))
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in _FORBIDDEN_CTOR:
                out.append((node.lineno, f"{f.id}(...) construction"))
            if isinstance(f, ast.Attribute) and f.attr in _FORBIDDEN_ATTRS:
                v = f.value
                if isinstance(v, ast.Name) and v.id == "memory":
                    out.append((node.lineno, f"memory.{f.attr}(...)"))
    return out


class TestMem0Seam(unittest.TestCase):
    def test_no_direct_mem0_callsites_outside_seam(self):
        bad = []
        for path in _iter_py(PKG):
            if path in _ALLOW:
                continue
            for ln, what in _violations(path):
                bad.append(f"{os.path.relpath(path, ROOT)}:{ln}: {what}")
        self.assertEqual(bad, [],
                         "mem0 呼出しが seam 外にあります:\n" +
                         "\n".join(bad))

    def test_seam_wrapper_is_installed(self):
        """_create_memory の戻り値が Mem0Seam で包まれていること。"""
        with io.open(SEAM_FILE, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("class Mem0Seam", src)
        self.assertIn("return Mem0Seam(m)", src)

    def test_authority_entrypoints_exist(self):
        """authority 分岐を持つ公開面がそろっていること。"""
        import ast as _ast
        with io.open(SEAM_FILE, encoding="utf-8") as f:
            tree = _ast.parse(f.read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for want in ("authority", "recall", "store_turn", "add_self_note",
                     "recall_self", "search_user", "all_user", "all_agent",
                     "add_user_fact", "delete_item", "search_agent"):
            self.assertIn(want, names, f"{want} が memory.py にない")


if __name__ == "__main__":
    unittest.main()
