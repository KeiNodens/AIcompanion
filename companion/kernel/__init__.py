"""companion.kernel —— canonical core の純粋関数層（Phase 8）

このパッケージは LLM・ネットワーク・sqlite3（writer 除く）・壁時計・乱数を
持たない決定的コードのみで構成する。唯一の不純端は writer.py（sqlite3 rw
接続の唯一所有者・時計/乱数の注入点）。規約違反は
tests/test_kernel_purity.py の AST lint と manifest.json の pin で機械検出する。
"""
