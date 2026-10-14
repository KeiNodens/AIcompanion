"""adapters.embedder —— canonical 側の embedding 供給口（P8-3）

embedding は「どのモデルで・どう作ったか」で意味が変わるため、
`model_stamp` と `embedding_space`（語彙名）を必ず刻印する
（derived-rebuild §3）。canonical 側は mem0 の LazyEmbedder に依存しない
専用口を持つ（canonical 権威経路に mem0 を混ぜない）。

実装:
  - TrigramHashEmbedder: 決定的スタブ。norm_text の文字 trigram を
    bucket hash で写像した疎ベクトル。モデル DL 不要・同一入力→同一
    ベクトルで、trigram 共有が cosine 類似度に効くためテスト/オフライン
    運用で実際に想起が機能する。
  - Fastembedder: fastembed TextEmbedding の遅延ロード実装（本番面。
    model/dims は注入か config 既定）。
"""

from ..kernel import canon


class TrigramHashEmbedder:
    """決定的埋め込み（テスト・オフライン用）。

    norm_text → 文字 trigram → 各 trigram を sha256 の上位ビットで bucket に
    写像しカウント、L2 正規化。意味的には「文字列の trigram 共有度」を測る
    ベクトルなので、言い換えではなく語彙一致系の想起に近い挙動を示す。
    """

    def __init__(self, dims: int = 384):
        self.dims = dims
        self.space = f"pcore.trigram-hash-v1/d{dims}"

    def model_stamp(self) -> dict:
        return {"model": "trigram-hash", "version": "v1",
                "params": {"dims": self.dims}}

    def _vec(self, text: str) -> list:
        t = canon.norm_text(text or "")
        grams = {t[i:i + 3] for i in range(max(0, len(t) - 2))} or ({t} if t
                                                                  else set())
        v = [0.0] * self.dims
        for g in grams:
            h = int(canon.hash_bytes("pcore.emb_bucket.v1",
                                     g.encode("utf-8"))[:8], 16)
            v[h % self.dims] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    def embed(self, texts) -> list:
        return [self._vec(t) for t in texts]


class Fastembedder:
    """fastembed TextEmbedding の遅延ロード実装（本番面）。"""

    def __init__(self, model_name: str = None, dims: int = None):
        if model_name is None or dims is None:
            from .. import config
            model_name = model_name or config.EMBED_MODEL
            dims = dims or config.EMBED_DIMS
        self.model_name = model_name
        self.dims = dims
        self.space = f"fastembed/{model_name}/d{dims}"
        self._model = None

    def model_stamp(self) -> dict:
        return {"model": self.model_name, "version": "fastembed",
                "params": {"dims": self.dims}}

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=self.model_name)

    def embed(self, texts) -> list:
        self._ensure()
        from .. import config
        out = self._model.embed(
            [t.replace("\n", " ") for t in texts],
            batch_size=config.EMBED_BATCH)
        return [v.tolist() for v in out]
