"""adapters.rerank —— cross-encoder 後段再採点（C-3、read-path §4 補強）

score-v1 で並んだ上位候補を、質問文と候補本文を一緒に読む
cross-encoder（fastembed TextCrossEncoder）で再採点する腕。
vec0/fts の網羅性（recall）で拾い、CE の精密判定（precision）で
仕上げる —— 単一ユーザー規模ではプール全件に掛けても十分軽い。

- モデルは遅延ロード（最初の rerank で DL＋常駐）
- reranker=None なら呼び出し側は score-v1 順のまま（決定的 fallback）
- 失敗は呼び出し側が catch して score-v1 順に倒す（供給を止めない）
"""

import logging

log = logging.getLogger("companion")

DEFAULT_MODEL = "jinaai/jina-reranker-v2-base-multilingual"


class FastembedReranker:
    """fastembed TextCrossEncoder の遅延ロード実装（本番面）。"""

    def __init__(self, model_name: str = None):
        if model_name is None:
            from .. import config
            model_name = getattr(config, "RERANK_MODEL", "") \
                or DEFAULT_MODEL
        self.model_name = model_name
        self._model = None

    def model_stamp(self) -> dict:
        return {"model": self.model_name, "type": "cross-encoder",
                "version": "fastembed"}

    def _ensure(self):
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            self._model = TextCrossEncoder(model_name=self.model_name)

    def rerank(self, query: str, texts) -> list:
        """(query, 本文列) -> 関連度スコア列（texts と同じ順・長さ）。"""
        self._ensure()
        docs = [str(t or "").replace("\n", " ") for t in texts]
        return [float(s) for s in self._model.rerank(query, docs)]


def reranker_kind() -> str:
    """設定値: 0/off（既定）or 1/on。"""
    import os
    return os.getenv("CANONICAL_RERANK", "0").strip().lower()


def default_reranker():
    """CANONICAL_RERANK=1 なら本番 reranker、それ以外は None（score-v1
    順の決定的 fallback）。"""
    if reranker_kind() in ("1", "on", "true", "yes"):
        return FastembedReranker()
    return None
