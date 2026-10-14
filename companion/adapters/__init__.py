"""companion.adapters —— Phase 8 の I/O・LLM・スケジューリング境界層。

kernel/（決定的純粋層）と外界の間の不純端。canonical への書込みは
`kernel.writer` の単一接続を経由する（第二書込み路を作らない）。
"""
