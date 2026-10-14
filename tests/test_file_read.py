"""tools/files.py の file_read — 行番号形式・分割継続・秘密系拒否・ドキュメント抽出"""
import sqlite3
import unittest
import zipfile
from pathlib import Path

import _support


_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _make_docx(path, paras):
    body = "".join(
        f'<w:p><w:r><w:t>{t}</w:t></w:r></w:p>' for t in paras)
    doc = (f'<?xml version="1.0"?><w:document xmlns:w="{_NS_W}">'
           f'<w:body>{body}</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)


def _make_xlsx(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml",
            f'<workbook xmlns="{_NS_S}" xmlns:r="{_NS_REL}"><sheets>'
            f'<sheet name="S1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{_NS_PKG_REL}">'
            f'<Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/>'
            f'</Relationships>')
        z.writestr("xl/sharedStrings.xml",
            f'<sst xmlns="{_NS_S}"><si><t>名前</t></si><si><t>値</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{_NS_S}"><sheetData>'
            f'<row r="1"><c r="A1" t="s"><v>0</v></c>'
            f'<c r="B1" t="s"><v>1</v></c></row>'
            f'<row r="2"><c r="A2"><v>42</v></c><c r="B2"><v>3.5</v></c></row>'
            f'</sheetData></worksheet>')


def _make_pdf(path, text):
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    path.write_bytes(out)


class FileReadTest(unittest.TestCase):
    def setUp(self):
        self._iso = _support.IsolatedModules()
        self._iso.__enter__()
        self.addCleanup(self._iso.close)
        self.files = self._iso.import_module("companion.tools.files")
        self.dir = self._iso.tmp / "reads"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _write(self, name, text):
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_reads_with_line_numbers(self):
        p = self._write("memo.txt", "一\n二\n三\n")
        out = self.files.file_read(str(p))
        self.assertEqual(out, "1|一\n2|二\n3|三")

    def test_offset_and_limit_with_continuation_hint(self):
        p = self._write("long.txt", "".join(f"L{i}\n" for i in range(1, 11)))
        out = self.files.file_read(str(p), offset=4, limit=3)
        self.assertIn("4|L4\n5|L5\n6|L6", out)
        self.assertIn("offset=7", out)
        out2 = self.files.file_read(str(p), offset=7, limit=3)
        self.assertIn("7|L7", out2)

    def test_offset_past_end(self):
        p = self._write("a.txt", "only\n")
        self.assertIn("ありません", self.files.file_read(str(p), offset=99))

    def test_missing_file(self):
        self.assertIn("見つかりません",
                      self.files.file_read(str(self.dir / "none.txt")))

    def test_env_denied_anywhere(self):
        p = self._write(".env", "TOKEN=secret\n")
        out = self.files.file_read(str(p))
        self.assertIn("読めません", out)
        self.assertNotIn("secret", out)

    def test_env_example_is_allowed(self):
        p = self._write(".env.example", "TOKEN=\n")
        self.assertIn("1|TOKEN=", self.files.file_read(str(p)))

    def test_env_suffixed_and_backup_denied(self):
        for name in ("foo.env", ".env.bak", "settings.env"):
            p = self._write(name, "TOKEN=secret\n")
            out = self.files.file_read(str(p))
            self.assertIn("読めません", out, name)
            self.assertNotIn("secret", out, name)

    def test_private_key_suffix_denied(self):
        p = self._write("server.pem", "-----BEGIN-----\n")
        self.assertIn("読めません", self.files.file_read(str(p)))

    def test_empty_file(self):
        p = self._write("empty.txt", "")
        self.assertIn("空ファイル", self.files.file_read(str(p)))

    def test_ssh_dir_denied(self):
        d = self.dir / ".ssh"
        d.mkdir()
        p = d / "config"
        p.write_text("Host x\n", encoding="utf-8")
        self.assertIn("読めません", self.files.file_read(str(p)))

    def test_binary_refused(self):
        p = self.dir / "bin.dat"
        p.write_bytes(b"\x00\x01\x02binary")
        self.assertIn("バイナリ", self.files.file_read(str(p)))

    def test_nt_namespace_refused(self):
        self.assertIn("読めません",
                      self.files.file_read("\\\\.\\PhysicalDrive0"))

    def test_shiftjis_fallback(self):
        p = self.dir / "sjis.txt"
        p.write_bytes("日本語\n".encode("cp932"))
        out = self.files.file_read(str(p))
        self.assertIn("1|日本語", out)
        self.assertIn("Shift_JIS", out)

    def test_docx_extracted(self):
        p = self.dir / "memo.docx"
        _make_docx(p, ["こんにちは", "二行目"])
        out = self.files.file_read(str(p))
        self.assertIn("1|こんにちは", out)
        self.assertIn("2|二行目", out)

    def test_xlsx_extracted(self):
        p = self.dir / "table.xlsx"
        _make_xlsx(p)
        out = self.files.file_read(str(p))
        self.assertIn("Sheet: S1", out)
        self.assertIn("名前\t値", out)
        self.assertIn("42\t3.5", out)

    def test_ipynb_extracted(self):
        import json
        p = self.dir / "nb.ipynb"
        p.write_text(json.dumps({"cells": [
            {"cell_type": "markdown", "source": ["# タイトル\n"]},
            {"cell_type": "code", "source": "x = 1", "outputs": [
                {"output_type": "stream", "text": "done\n"}]},
        ]}), encoding="utf-8")
        out = self.files.file_read(str(p))
        self.assertIn("Markdown cell", out)
        self.assertIn("x = 1", out)
        self.assertIn("done", out)

    def test_sqlite_extracted(self):
        p = self.dir / "test.db"
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        con.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
        con.commit(); con.close()
        out = self.files.file_read(str(p))
        self.assertIn("## t  (2 rows)", out)
        self.assertIn("| 1 | a |", out)

    def test_pdf_text_layer_extracted(self):
        import importlib.util
        if not importlib.util.find_spec("pypdfium2"):
            self.skipTest("pypdfium2 not installed")
        p = self.dir / "doc.pdf"
        _make_pdf(p, "Hello PDF")
        out = self.files.file_read(str(p))
        self.assertIn("Hello PDF", out)
        self.assertIn("PDFをテキスト化", out)

    def test_single_oversized_line_still_progresses(self):
        """1行が文字上限超過でも切り詰めて返す（offsetが進まない無限化を防ぐ）"""
        p = self.dir / "min.js"
        p.write_text("x" * (self.files._READ_MAX_CHARS + 500), encoding="utf-8")
        out = self.files.file_read(str(p))
        self.assertIn("行内省略", out)
        self.assertTrue(out.startswith("1|"))


if __name__ == "__main__":
    unittest.main()
