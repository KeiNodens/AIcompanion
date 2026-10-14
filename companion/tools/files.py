"""file_read — ローカルテキストファイルの読み取り。

橋を通さない Core 側の読取口で、小窓・API直叩きを含む全入口で効く。
出力は Hermes の read_file と同じ「行番号|本文」形式 + offset 継続に
揃えてある（モデルが両者を同じ手順で扱えるように）。

秘密系ファイルは名前・置き場所で拒否する。読んだ内容はそのまま
会話コンテキスト = ChatMock 経由で外部LLMへ送られるので、.env 等を
素通しさせない（Hermes file_safety の read 拒否と同じポリシー。
構造確認なら .env.example を読む運用）。
"""
import contextlib
import functools
import itertools
import json
import logging
import posixpath
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

log = logging.getLogger("companion")

# 秘密系 basename（どこにあっても拒否）
_SECRET_NAMES = {
    ".envrc",
    "id_rsa", "id_ed25519", "authorized_keys", ".netrc", ".pgpass",
    ".npmrc", ".pypirc", ".git-credentials", "auth.json", "auth.lock",
    ".anthropic_oauth.json", "bws_cache.json", "webhook_subscriptions.json",
}
# .env 系は名前に ".env" を含めば拒否（.env.bak/foo.env 等も拾う）。
# ただし見本系 suffix のものだけ許可（構造確認の用途を残す）
_ENV_ALLOW_SUFFIXES = (".example", ".sample", ".template", ".dist")
# 秘密鍵・証拠書系の拡張子（中身が鍵本体なので場所を問わず拒否）
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".ppk", ".keystore", ".jks"}
# 資格情報ディレクトリ。パスのパーツに含まれれば拒否
_SECRET_DIRS = {
    ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure",
}
_READ_LIMIT = 2000          # 1回の最大行数（Hermes read_file と同じ）
_READ_MAX_CHARS = 100_000   # 1回の最大文字数（同。超えたら次offsetを返す）

# テキスト化してから行番号読みに回せるドキュメント形式。
# Hermes側のread_fileがstdlib抽出する拡張子 + PDF（pypdfium2でテキスト層だけ）
_DOC_EXTENSIONS = {
    ".docx", ".xlsx", ".ipynb", ".pdf", ".db", ".sqlite", ".sqlite3",
}


class _DocError(Exception):
    """ドキュメントのテキスト化に失敗した（壊れ・空テキスト・非対応）"""


def _denied(p: Path) -> bool:
    name = p.name.lower()
    if name in _SECRET_NAMES or p.suffix.lower() in _SECRET_SUFFIXES:
        return True
    if ".env" in name and not name.endswith(_ENV_ALLOW_SUFFIXES):
        return True
    parts = {part.lower() for part in p.parts}
    return bool(parts & _SECRET_DIRS)


def _decode_line(raw: bytes) -> str:
    """1行のデコード。utf-8 → Shift_JIS（cp932）→ 置換の順で倒す"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp932")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


# ── ドキュメント抽出（Hermes tools/read_extract.py のstdlib抽出器を移植）──

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_MAX_XLSX_ROWS_PER_SHEET = 5000
_MAX_XLSX_COLS = 256


def _joined(lines: list, empty_error: str) -> str:
    if not any(line.strip() for line in lines):
        raise _DocError(empty_error)
    return "\n".join(lines).rstrip("\n") + "\n"


def _docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            root = ET.fromstring(zf.read("word/document.xml"))
    except zipfile.BadZipFile as e:
        raise _DocError(f"DOCXではありません: {e}")
    except KeyError:
        raise _DocError("word/document.xml がないDOCXです")
    except (ET.ParseError, OSError) as e:
        raise _DocError(f"DOCXを開けません: {e}")
    w = f"{{{_NS_W}}}"
    breaks = {f"{w}tab": "\t", f"{w}br": "\n", f"{w}cr": "\n"}
    lines = []
    for para in root.iter(f"{w}p"):
        # w:rt はルビ（振り仮名）ガイド。本文ではないので除外する
        guide = {n for rt in para.iter(f"{w}rt") for n in rt.iter()}
        text = "".join(
            (n.text or "") if n.tag == f"{w}t" else breaks.get(n.tag, "")
            for n in para.iter() if n not in guide)
        lines.extend(text.split("\n"))
    return _joined(lines, "DOCXからテキストを取り出せませんでした")


def _xlsx_string_text(item: "ET.Element", s: str) -> str:
    """si/is のベーステキスト（rPh 振り仮名注釈は除く）"""
    return "".join(
        (child.text or "") if child.tag == f"{s}t" else child.findtext(f"{s}t") or ""
        for child in item if child.tag in {f"{s}t", f"{s}r"})


def _col_index(ref: str) -> int:
    """セル参照(A1, AB7)の0始まり列番号"""
    idx = functools.reduce(
        lambda acc, ch: acc * 26 + ord(ch.upper()) - ord("A") + 1,
        itertools.takewhile(str.isalpha, ref), 0)
    return max(idx - 1, 0)


def _cell_value(cell: "ET.Element", shared: list, s: str) -> str:
    value = cell.findtext(f"{s}v") or ""
    typ = cell.get("t", "")
    if typ == "s":
        with contextlib.suppress(ValueError, IndexError):
            return shared[int(value)]
        return ""
    if typ == "inlineStr":
        inline = cell.find(f"{s}is")
        return "" if inline is None else _xlsx_string_text(inline, s)
    if typ == "b":
        return "TRUE" if value.strip() in {"1", "true", "TRUE"} else "FALSE"
    return (value or "#ERROR") if typ == "e" else value


def _sheet_rows(xml_bytes: bytes, shared: list, s: str) -> list:
    root = ET.fromstring(xml_bytes)
    rows = []
    for row in itertools.islice(root.iter(f"{s}row"), _MAX_XLSX_ROWS_PER_SHEET):
        cells, max_col = {}, -1
        for cell in row.iter(f"{s}c"):
            col = _col_index(cell.get("r", "")) if cell.get("r") else max_col + 1
            if col < _MAX_XLSX_COLS:
                cells[col] = _cell_value(cell, shared, s)
                max_col = max(max_col, col)
        rows.append([cells.get(i, "") for i in range(max_col + 1)])
    while rows and not any(v.strip() for v in rows[-1]):
        rows.pop()
    return rows


def _xlsx(path: Path) -> str:
    s, r, pr = f"{{{_NS_S}}}", f"{{{_NS_REL}}}", f"{{{_NS_PKG_REL}}}"
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise _DocError(f"XLSXを開けません: {e}")
    with zf:
        names = set(zf.namelist())
        try:
            sst = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        except (KeyError, ET.ParseError):
            sst = ET.Element("missing")
        shared = [_xlsx_string_text(item, s) for item in sst.iter(f"{s}si")]
        try:
            rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            rels = {rel.get("Id", ""): rel.get("Target", "")
                    for rel in rels_root.iter(f"{pr}Relationship") if rel.get("Id")}
        except (KeyError, ET.ParseError):
            rels = {}
        try:
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
        except (KeyError, ET.ParseError) as e:
            raise _DocError(f"XLSXを開けません: {e}")
        out = []
        for sheet in wb.iter(f"{s}sheet"):
            target = rels.get(sheet.get(f"{r}id", ""), "").lstrip("/")
            part = posixpath.normpath(
                target if target.startswith("xl/") else f"xl/{target}")
            if sheet.get("state", "visible") in {"hidden", "veryHidden"} \
                    or part not in names:
                continue
            with contextlib.suppress(ET.ParseError):
                rows = _sheet_rows(zf.read(part), shared, s)
                out += [f"# ── Sheet: {sheet.get('name', 'Sheet')} ──",
                        *(["\t".join(row) for row in rows] or ["(empty)"]), ""]
    return _joined(out, "XLSXに読めるシートがありません")


def _source_text(source) -> str:
    """notebookのsource/textフィールド（strまたはstr断片のlist）"""
    if isinstance(source, list):
        source = "".join(item for item in source if isinstance(item, str))
    return source if isinstance(source, str) else ""


_NB_OUTPUT_CHARS = 2000  # セル出力1件あたりの上限（実行ログが会話を埋めないよう）


def _nb_outputs(cell: dict) -> str:
    texts = []
    for o in cell.get("outputs") or []:
        if not isinstance(o, dict):
            continue
        t = o.get("output_type")
        if t == "stream":
            texts.append(_source_text(o.get("text", "")))
        elif t in ("error", "pyerr"):
            tb = o.get("traceback") or []
            texts.append("\n".join(x for x in tb if isinstance(x, str)))
        elif t in ("execute_result", "display_data", "pyout"):
            data = o.get("data") or {}
            if isinstance(data, dict):
                texts.append(_source_text(data.get("text/plain", "")))
    joined = "\n".join(x for x in texts if x.strip())
    if len(joined) > _NB_OUTPUT_CHARS:
        joined = joined[:_NB_OUTPUT_CHARS] + "…"
    return joined


_CELL_LABELS = {"markdown": "Markdown", "code": "Code", "raw": "Raw"}


def _ipynb(path: Path) -> str:
    try:
        nb = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError) as e:
        raise _DocError(f"ノートブックを開けません: {e}")
    if not isinstance(nb, dict):
        raise _DocError("ノートブックの形式ではありません")
    raw = nb.get("cells")
    if isinstance(raw, list):
        cells = raw
    else:  # nbformat v3 は worksheets 配下
        cells = [c for ws in nb.get("worksheets", []) if isinstance(ws, dict)
                 for c in ws.get("cells", [])]
    counts, out = {}, []
    for cell in cells:
        typ = cell.get("cell_type") if isinstance(cell, dict) else None
        if typ not in _CELL_LABELS:
            continue
        counts[typ] = counts.get(typ, 0) + 1
        src = _source_text(cell.get("source", "")).rstrip("\n")
        out += [f"# ── {_CELL_LABELS[typ]} cell {counts[typ]} ──", src, ""]
        if typ == "code":
            otxt = _nb_outputs(cell).rstrip("\n")
            if otxt:
                out += [f"# ── Output (cell {counts[typ]}) ──", otxt, ""]
    return _joined(out, "ノートブックに読めるセルがありません")


_SQLITE_MAGIC = b"SQLite format 3\x00"
_SQLITE_PREVIEW_ROWS = 5
_SQLITE_MAX_TABLES = 200
_SQLITE_CELL_CHARS = 80


def _sqlite_cell(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray)):
        return f"<blob {len(value)} bytes>"
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= _SQLITE_CELL_CHARS else text[:_SQLITE_CELL_CHARS - 1] + "…"


def _sqlite(path: Path) -> str:
    import sqlite3
    with open(path, "rb") as fh:
        if fh.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
            raise _DocError("SQLiteデータベースではありません")
    # 読み取り専用URI（immutable=1 で WAL/journal を触らず生きたDBも読める）
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        raise _DocError(f"SQLiteを開けません: {e}")
    try:
        objs = con.execute(
            "SELECT type, name, sql FROM sqlite_master"
            " WHERE sql IS NOT NULL ORDER BY type DESC, name").fetchall()
        out = ["# SQLite database", ""]
        tables = [o for o in objs if o[0] == "table"]
        others = [o for o in objs if o[0] != "table"]
        out.append(f"{len(tables)} table(s), {len(others)} index/view/trigger(s)")
        for _, name, sql in tables[:_SQLITE_MAX_TABLES]:
            quoted = '"' + name.replace('"', '""') + '"'
            count = con.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            out += ["", f"## {name}  ({count:,} rows)", (sql or "").strip()]
            cur = con.execute(f"SELECT * FROM {quoted} LIMIT {_SQLITE_PREVIEW_ROWS}")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            if rows:
                out.append("| " + " | ".join(cols) + " |")
                out.append("|" + "---|" * len(cols))
                for row in rows:
                    out.append("| " + " | ".join(_sqlite_cell(v) for v in row) + " |")
        if len(tables) > _SQLITE_MAX_TABLES:
            out.append(f"\n... {len(tables) - _SQLITE_MAX_TABLES} more tables omitted")
        if others:
            out += ["", "## Indexes / views / triggers"]
            out += [f"- {t} {n}" for t, n, _ in others]
        return "\n".join(out)
    except sqlite3.DatabaseError as e:
        raise _DocError(f"SQLiteの読み取りに失敗しました: {e}")
    finally:
        con.close()


def _pdf(path: Path) -> str:
    """テキスト層の抽出。スキャンPDFは文字が取れないので警告を先頭に付ける"""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise _DocError(
            "PDFのテキスト抽出にはpypdfium2が要ります（doc_ingest経由が確実）")
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as e:
        raise _DocError(f"PDFを開けません: {e}")
    out, empty = [], 0
    try:
        total = len(doc)
        for i in range(total):
            page = doc[i]
            try:
                tp = page.get_textpage()
                try:
                    txt = tp.get_text_range().strip()
                finally:
                    tp.close()
            finally:
                page.close()
            if not txt:
                empty += 1
            out += [f"# ── Page {i + 1} ──", txt, ""]
    finally:
        doc.close()
    body = _joined(out, "PDFにテキスト層がありません"
                     "（スキャンPDFです。doc_ingestでの索引化が向いています）")
    if empty:
        body = (f"（{total}ページ中{empty}ページにテキストがありません。"
                "スキャン画像の可能性があります）\n") + body
    return body


_DOC_EXTRACTORS = {
    ".docx": _docx,
    ".xlsx": _xlsx,
    ".ipynb": _ipynb,
    ".pdf": _pdf,
    ".db": _sqlite,
    ".sqlite": _sqlite,
    ".sqlite3": _sqlite,
}

_DOC_LABEL = {
    ".docx": "Word文書", ".xlsx": "Excel文書", ".ipynb": "ノートブック",
    ".pdf": "PDF", ".db": "SQLite", ".sqlite": "SQLite", ".sqlite3": "SQLite",
}


def _paged(lines, offset: int, limit: int):
    """行イテレータから offset/limit と文字上限を適用して行を集める。
    (out, more, skipped) を返す。out が None のとき offset 超過（skipped=実在行数）"""
    skipped = 0
    for _ in range(offset - 1):
        if next(lines, None) is None:
            return None, False, skipped
        skipped += 1
    out, used, more = [], 0, False
    while len(out) < limit:
        line = next(lines, None)
        if line is None:
            break
        entry = f"{offset + len(out)}|{line}"
        if used + len(entry) > _READ_MAX_CHARS and out:
            more = True  # この行を未消費のまま終わる
            break
        if used + len(entry) > _READ_MAX_CHARS:
            # 先頭行だけで上限超過（minified等）。切り詰めて必ず進める
            entry = entry[:_READ_MAX_CHARS] + "…（行内省略）"
        out.append(entry)
        used += len(entry) + 1
    if not more:
        more = next(lines, None) is not None
    return out, more, skipped


def _render_paged(lines, offset: int, limit: int, note="") -> str:
    """note は走査後に評価される（str or 0引数callable）。Shift_JIS判定の
    ような走査中に分かる注記を付けられるようにするため"""
    out, more, skipped = _paged(lines, offset, limit)
    if out is None:
        return f"{offset}行目以降はありません（{skipped}行まで）"
    if not out and not more:
        return "（空ファイルです）"
    body = (note() if callable(note) else note) + "\n".join(out)
    if more:
        body += f"\n（続きあり: offset={offset + len(out)} で続きを読めます）"
    return body


def file_read(path: str, offset: int = 1, limit: int | None = None) -> str:
    raw_path = (path or "").strip()
    if not raw_path:
        return "path を指定してください"
    # \\.\PhysicalDrive 等のデバイスパスは解決するだけで危険（NTLM流出）。
    # Path 正規化の前に生文字列で弾く
    if raw_path.startswith(("\\\\.", "\\\\?")):
        return "そのパス形式は読めません"
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    # リンク名・辿った先の両方で拒否判定（.env への symlink 等を抜けない）
    if _denied(p) or _denied(p.resolve()):
        return ("そのファイルは秘密情報を含む可能性があるため読めません"
                "（構造を確認したいだけなら .env.example などの見本ファイルを使って）")
    if not p.exists():
        return f"ファイルが見つかりません: {path}"
    if not p.is_file():
        return f"ファイルではありません: {path}"
    try:
        offset = max(1, int(offset or 1))
        limit = min(_READ_LIMIT, max(1, int(limit or _READ_LIMIT)))
    except (TypeError, ValueError):
        return "offset/limit は数値で指定してください"
    ext = p.suffix.lower()
    if ext in _DOC_EXTENSIONS:
        try:
            doc_text = _DOC_EXTRACTORS[ext](p)
        except _DocError as e:
            return str(e)
        note = f"（{_DOC_LABEL[ext]}をテキスト化したものです）\n"
        return _render_paged(iter(doc_text.splitlines()), offset, limit, note)
    try:
        f = p.open("rb")
    except OSError as e:
        return f"読み込みに失敗しました: {e}"
    used_sjis = [False]
    with f:
        if b"\0" in f.read(8192):
            return ("バイナリファイルは読めません"
                    "（画像は vision_analyze が向いています）")
        f.seek(0)
        return _render_paged(
            _text_lines(f, used_sjis), offset, limit,
            lambda: "（Shift_JISとして読みました）\n" if used_sjis[0] else "")


def _text_lines(f, used_sjis: list):
    """バイナリストリームを行イテレータに。Shift_JIS使用があれば旗を立てる"""
    while True:
        raw = f.readline()
        if raw == b"":
            return
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            used_sjis[0] = True
        yield _decode_line(raw.rstrip(b"\r\n"))
