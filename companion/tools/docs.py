import logging
from pathlib import Path

from ..config import (
    DOCS_DIR,
    PAGEINDEX_API_KEY,
    PAGEINDEX_BASE_URL,
    PAGEINDEX_CHAT_MODEL,
    PAGEINDEX_DIR,
    PAGEINDEX_INDEX_MODEL,
)

log = logging.getLogger("companion")

_client = None


def _client_or_err():
    """PageIndexクライアントを遅延生成。未インストール時はエラー文字列"""
    global _client
    if _client is not None:
        return _client, None
    try:
        from pageindex import PageIndexClient
    except ImportError:
        return None, "pageindex が未インストールです（pip install pageindex）"
    backend = {"api_key": PAGEINDEX_API_KEY, "base_url": PAGEINDEX_BASE_URL}
    _client = PageIndexClient(
        index={
            "model": PAGEINDEX_INDEX_MODEL,
            "backend": backend,
            "storage_path": str(PAGEINDEX_DIR),
        },
        chat={"model": PAGEINDEX_CHAT_MODEL, "backend": backend},
        instructions="回答は日本語で。根拠となるページ番号が分かる場合は添えて。",
    )
    return _client, None


def _resolve_doc(client, query: str):
    """文書名の部分一致から (doc_id, name, None) を返す。失敗時は (None, None, エラー文)"""
    docs = client.list_documents(limit=200).get("documents", [])
    if not docs:
        return None, None, "取り込まれた文書はまだありません"
    q = (query or "").strip().lower()
    if not q:
        if len(docs) == 1:
            return docs[0]["id"], docs[0]["name"], None
        names = "、".join(f"「{d['name']}」" for d in docs[:20])
        return None, None, f"文書を指定してください（{names}）"
    exact = [d for d in docs if (d.get("name") or "").lower() == q]
    if not exact:
        stem = q.removesuffix(".pdf")
        exact = [
            d for d in docs if (d.get("name") or "").lower().removesuffix(".pdf") == stem
        ]
    hits = exact or [
        d for d in docs if q in (d.get("name") or "").lower() or
        q.removesuffix(".pdf") in (d.get("name") or "").lower().removesuffix(".pdf")
    ]
    if not hits:
        return None, None, f"文書「{query}」が見つかりません（doc_listで一覧を確認）"
    if len(hits) > 1:
        names = "、".join(f"「{d['name']}」" for d in hits[:20])
        return None, None, f"複数の文書が該当します: {names}"
    return hits[0]["id"], hits[0]["name"], None


def doc_ingest(path: str, source: str = "") -> str:
    """ローカルのPDFを索引化してライブラリに登録する"""
    client, err = _client_or_err()
    if err:
        return err
    p = Path(path)
    if not p.is_absolute():
        p = DOCS_DIR / p.name
    p = p.resolve()
    if not p.exists():
        return f"ファイルが見つかりません: {path}"
    if p.suffix.lower() != ".pdf":
        return "ローカルモードで索引化できるのはPDFのみです（テキストファイルは file_read で開けます）"
    try:
        res = client.submit_document(str(p), metadata={"source": source} if source else None)
        info = client.get_document(res["doc_id"])
    except Exception as e:
        log.exception("doc_ingest failed: %s", p)
        return f"文書の索引化に失敗しました: {e}"
    name = info.get("name") or p.name
    pages = info.get("pageNum") or "?"
    desc = (info.get("description") or "").strip()
    try:
        from ..memory import add_self_note
        note = f"文書「{name}」（{pages}ページ）を読めるように索引を作った。"
        if desc:
            note += f" 内容: {desc[:200]}"
        add_self_note(note, importance=3)
    except Exception:
        log.exception("doc_ingest self-note failed")
    return f"文書「{name}」（{pages}ページ）の索引を作りました"


def doc_ask(query: str, doc: str | None = None) -> str:
    """取り込み済み文書に質問して、木構造索引を辿った回答を得る"""
    client, err = _client_or_err()
    if err:
        return err
    doc_id, name, err = _resolve_doc(client, doc or "")
    if err:
        return err
    try:
        answer = client.chat(query, doc_id=doc_id)
    except Exception as e:
        log.exception("doc_ask failed: %s", doc_id)
        return f"文書への質問に失敗しました: {e}"
    return f"【{name} より】\n{answer}"


def doc_list() -> str:
    """取り込み済み文書の一覧"""
    client, err = _client_or_err()
    if err:
        return err
    try:
        docs = client.list_documents(limit=200).get("documents", [])
    except Exception as e:
        log.exception("doc_list failed")
        return f"文書一覧の取得に失敗しました: {e}"
    if not docs:
        return "取り込まれた文書はまだありません"
    lines = []
    for d in docs[:50]:
        name = d.get("name") or "?"
        pages = d.get("pageNum")
        lines.append(f"- {name}" + (f"（{pages}ページ）" if pages else ""))
    return f"## 文書 ({len(docs)})\n" + "\n".join(lines)


def doc_toc(doc: str) -> str:
    """文書の目次ツリー（索引の構造）を返す"""
    client, err = _client_or_err()
    if err:
        return err
    doc_id, name, err = _resolve_doc(client, doc)
    if err:
        return err
    try:
        nodes = client.get_document_structure(doc_id)
    except Exception as e:
        log.exception("doc_toc failed: %s", doc_id)
        return f"目次の取得に失敗しました: {e}"
    if not nodes:
        return "目次情報がありません"
    lines = [f"## {name} の構成"]
    for n in nodes[:60]:
        title = n.get("title") or "(無題)"
        page = n.get("page_index") or n.get("start_index")
        summary = (n.get("summary") or "").strip()
        line = f"- {title}" + (f"（p{page}）" if page else "")
        if summary:
            line += f" — {summary[:100]}"
        lines.append(line)
    return "\n".join(lines)


def doc_forget(doc: str) -> str:
    """文書の索引をライブラリから削除する（元のPDFファイルは残る）"""
    client, err = _client_or_err()
    if err:
        return err
    doc_id, name, err = _resolve_doc(client, doc)
    if err:
        return err
    try:
        client.delete_document(doc_id)
    except Exception as e:
        log.exception("doc_forget failed: %s", doc_id)
        return f"文書の削除に失敗しました: {e}"
    return f"文書「{name}」の索引を削除しました（data/docsのPDF本体は残っています）"
