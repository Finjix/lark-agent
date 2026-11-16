"""The workbook reader, A1 arithmetic and sheet periods (gof-xlsx.test.ts)."""

from __future__ import annotations

import os
import struct
import unittest
import zlib

from tests.helpers import temp_dir

from gof.a1 import CellRange, column_index, column_letter, translate_formula
from gof.snapshot import Snapshot, sheet_period, validation_projection
from gof.util import Blocked
from gof.xlsx import read_workbook
from gof.xmlparse import parse_xml
from gof.zipread import ZipReader


def zip_files(files: dict[str, str], deflate: bool = True) -> bytes:
    """A minimal zip writer for building workbook files by hand: the part names and XML are exactly what the test wants to feed in."""
    locals_: list[bytes] = []
    central: list[bytes] = []
    offset = 0
    for name, text in files.items():
        raw = text.encode("utf-8")
        if deflate:
            compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
            body = compressor.compress(raw) + compressor.flush()
        else:
            body = raw
        name_bytes = name.encode("utf-8")
        crc = zlib.crc32(raw) & 0xFFFFFFFF
        header = struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 8 if deflate else 0, 0, 0, crc, len(body), len(raw), len(name_bytes), 0)
        entry = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 8 if deflate else 0, 0, 0, crc, len(body), len(raw), len(name_bytes), 0, 0, 0, 0, 0, offset)
        locals_.extend([header, name_bytes, body])
        central.extend([entry, name_bytes])
        offset += len(header) + len(name_bytes) + len(body)
    directory = b"".join(central)
    count = len(central) // 2
    end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, count, count, len(directory), offset, 0)
    return b"".join(locals_) + directory + end


HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
STYLES = (f"{HEAD}<styleSheet {NS}><fonts><font><sz val=\"11\"/></font><font><b/></font></fonts><fills><fill/><fill><patternFill patternType=\"solid\"/></fill></fills>"
          "<borders><border/></borders><cellXfs><xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\"/><xf numFmtId=\"0\" fontId=\"1\" fillId=\"0\" borderId=\"0\"/>"
          "<xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\"><alignment wrapText=\"1\"/></xf><xf numFmtId=\"164\" fontId=\"0\" fillId=\"0\" borderId=\"0\"/></cellXfs>"
          "<numFmts count=\"1\"><numFmt numFmtId=\"164\" formatCode=\"0.0\"/></numFmts></styleSheet>")


def workbook(sheet_xml: str, extra: dict[str, str] | None = None, styles: str = "") -> str:
    file = os.path.join(temp_dir("gof-xlsx-"), "book.xlsx")
    parts = {
        "xl/workbook.xml": f'{HEAD}<workbook {NS}><sheets><sheet name="表一" sheetId="1" r:id="rId1"/><sheet name="隐藏" sheetId="2" state="hidden" r:id="rId2"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'{HEAD}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                      '<Relationship Id="rId1" Type="x/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="x/worksheet" Target="/xl/worksheets/sheet2.xml"/></Relationships>',
        "xl/sharedStrings.xml": f'{HEAD}<sst {NS}><si><t>头</t></si><si><r><t>富</t></r><r><t xml:space="preserve"> 文本</t></r><rPh><t>ふ</t></rPh></si><si><t>a_x000D_b</t></si></sst>',
        "xl/styles.xml": styles or STYLES,
        "xl/worksheets/sheet1.xml": sheet_xml,
        "xl/worksheets/sheet2.xml": f"{HEAD}<worksheet {NS}><sheetData/></worksheet>",
        **(extra or {}),
    }
    with open(file, "wb") as handle:
        handle.write(zip_files(parts))
    return file


class WorkbookTests(unittest.TestCase):
    def test_cell_kinds(self) -> None:
        """Shared and rich strings, inline strings, numbers, booleans, errors, escaped control characters, empty cells."""
        file = workbook(f"{HEAD}<worksheet {NS}><sheetData><row r=\"1\">"
                        '<c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c><c r="D1" t="inlineStr"><is><t>行内</t></is></c>'
                        '<c r="E1"><v>12.50</v></c><c r="F1" t="b"><v>1</v></c><c r="G1" t="e"><v>#N/A</v></c><c r="H1" t="str"><v>结果</v></c><c r="I1"/><c r="J1" s="1"/>'
                        "</row></sheetData></worksheet>")
        sheet = read_workbook(file).sheets[0]
        at = sheet.cells.get
        self.assertEqual([at("A1").value, at("B1").value, at("C1").value, at("D1").value], ["头", "富 文本", "a\rb", "行内"])
        self.assertEqual([at("E1").kind, at("E1").value, at("F1").kind, at("G1").kind, at("H1").kind], ["n", "12.50", "b", "e", "s"])
        self.assertIsNone(at("I1").value)
        self.assertIsNotNone(at("J1").style, "a bold empty cell still has a style of its own")
        self.assertEqual(read_workbook(file).sheets[1].state, "hidden")

    def test_formulas(self) -> None:
        """Plain, shared (filled down with moved references) and array formulas."""
        file = workbook(f"{HEAD}<worksheet {NS}><sheetData>"
                        '<row r="1"><c r="A1"><v>1</v></c><c r="B1"><f>A1*2</f><v>2</v></c></row>'
                        '<row r="2"><c r="A2"><v>2</v></c><c r="B2"><f t="shared" ref="B2:B4" si="0">$A2+A2</f><v>4</v></c></row>'
                        '<row r="3"><c r="A3"><v>3</v></c><c r="B3"><f t="shared" si="0"/><v>6</v></c></row>'
                        '<row r="4"><c r="B4"><f t="array" ref="B4">SUM(A1:A3)</f></c></row></sheetData></worksheet>')
        sheet = read_workbook(file).sheets[0]
        self.assertEqual([sheet.cells[c].value for c in ("B1", "B2", "B3", "B4")], ["=A1*2", "=$A2+A2", "=$A3+A3", "=SUM(A1:A3)"])
        self.assertTrue(all(sheet.cells[c].kind == "f" for c in ("B1", "B2", "B3", "B4")))

    def test_styles_by_content(self) -> None:
        """Styles are compared by content, so renumbering the style table does not look like a change."""
        a = workbook(f'{HEAD}<worksheet {NS}><sheetData><row r="1"><c r="A1" s="1"><v>1</v></c></row></sheetData></worksheet>')
        reordered = (f'{HEAD}<styleSheet {NS}><fonts><font><sz val="11"/></font><font><b/></font></fonts><fills><fill/><fill><patternFill patternType="solid"/></fill></fills>'
                     '<borders><border/></borders><cellXfs><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0"><alignment wrapText="1"/></xf>'
                     '<xf numFmtId="0" fontId="1" fillId="0" borderId="0"/></cellXfs></styleSheet>')
        b = workbook(f'{HEAD}<worksheet {NS}><sheetData><row r="1"><c r="A1" s="2"><v>1</v></c></row></sheetData></worksheet>', {}, reordered)
        self.assertEqual(read_workbook(a).sheets[0].cells["A1"].style, read_workbook(b).sheets[0].cells["A1"].style)
        other = workbook(f'{HEAD}<worksheet {NS}><sheetData><row r="1"><c r="A1" s="3"><v>1</v></c></row></sheetData></worksheet>')
        self.assertNotEqual(read_workbook(a).sheets[0].cells["A1"].style, read_workbook(other).sheets[0].cells["A1"].style)

    def test_layout_parts(self) -> None:
        """Merges, rows, columns, filter, protection, comments, hyperlinks."""
        file = workbook(f'{HEAD}<worksheet {NS}><cols><col min="2" max="3" width="9" hidden="1"/></cols><sheetData>'
                        '<row r="1" ht="30" customHeight="1" hidden="1"><c r="A1"><v>x</v></c></row><row r="2" spans="1:2"><c r="A2"><v>y</v></c></row></sheetData>'
                        '<sheetProtection sheet="1" objects="1"/><autoFilter ref="A1:C9"><filterColumn colId="0"/></autoFilter><mergeCells count="1"><mergeCell ref="B3:A2"/></mergeCells>'
                        '<hyperlinks><hyperlink ref="A2" r:id="rId1"/><hyperlink ref="B2:B3" location="Sheet2!A1"/></hyperlinks></worksheet>',
                        {"xl/worksheets/_rels/sheet1.xml.rels": f'{HEAD}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                                                '<Relationship Id="rId1" Type="x/hyperlink" Target="https://example.com/a" TargetMode="External"/><Relationship Id="rId2" Type="http://x/comments" Target="../comments1.xml"/></Relationships>',
                         "xl/comments1.xml": f'{HEAD}<comments {NS}><authors><author>某人</author></authors><commentList><comment ref="C5" authorId="0"><text><r><t>备</t></r><r><t>注</t></r></text></comment></commentList></comments>'})
        sheet = read_workbook(file).sheets[0]
        self.assertEqual(sheet.merges, ["A2:B3"])
        self.assertEqual(sheet.rows, {"1": ["30", True, 0, False, None]}, "a row with only spans has nothing to compare")
        self.assertEqual(sheet.columns["B"], [2, 3, "9", True, 0, False, None])
        self.assertEqual(sheet.filter["ref"], "A1:C9")
        self.assertTrue(sheet.protected_sheet)
        self.assertEqual(sheet.cells["A2"].link, ["https://example.com/a", None])
        self.assertEqual(sheet.cells["B3"].link, [None, "Sheet2!A1"])
        self.assertEqual(sheet.cells["C5"].comment, ["某人", "备注"])
        self.assertIsNone(sheet.cells["C5"].value, "a comment alone makes the cell occupied but valueless")

    def test_dropdown_rules(self) -> None:
        """Dropdown rules, including the ones in the x14 extension, enter the comparison but not the options."""
        file = workbook(f'{HEAD}<worksheet {NS} xmlns:x14="u" xmlns:xm="v"><sheetData><row r="1"><c r="A1"><v>1</v></c></row></sheetData>'
                        '<dataValidations count="1"><dataValidation type="list" allowBlank="1" sqref="A2:A9 C2:C3"><formula1>"x,y"</formula1></dataValidation></dataValidations>'
                        '<extLst><ext uri="u"><x14:dataValidations count="1"><x14:dataValidation type="list"><x14:formula1><xm:f>Sheet2!$A$1:$A$3</xm:f></x14:formula1><xm:sqref>B2:B9</xm:sqref></x14:dataValidation></x14:dataValidations></ext></extLst></worksheet>')
        sheet = read_workbook(file).sheets[0]
        self.assertEqual(len(sheet.rules), 2)
        self.assertEqual([[r.ext, r.formula1, [str(x) for x in r.ranges]] for r in sheet.rules], [[False, '"x,y"', ["A2:A9", "C2:C3"]], [True, "Sheet2!$A$1:$A$3", ["B2:B9"]]])
        snap = Snapshot(file)
        self.assertEqual(len(validation_projection(snap)), 3, "one interval group per column and rule")
        self.assertEqual(len(validation_projection(snap, ("表一", CellRange("A2:C4")))), 2, "rows inside the append area drop out")

    def test_refusals(self) -> None:
        """Files that are not workbooks, or that could hurt, are refused."""
        directory = temp_dir("gof-xlsx-")

        def write(name: str, data: bytes) -> str:
            f = os.path.join(directory, name)
            with open(f, "wb") as handle:
                handle.write(data)
            return f

        self.assertRaises(Blocked, read_workbook, write("a.xlsx", b"not a zip"))
        self.assertRaisesRegex(Blocked, r"workbook\.xml", read_workbook, write("b.xlsx", zip_files({"other.txt": "x"})))
        self.assertRaisesRegex(Blocked, "XLSX", Snapshot, write("c.csv", b"a,b"))
        data = bytearray(zip_files({"xl/workbook.xml": "x" * 10}))
        at = data.index(bytes([0x50, 0x4B, 0x01, 0x02])) + 24
        data[at:at + 4] = struct.pack("<I", 0x7FFFFFFF)
        self.assertRaisesRegex(Blocked, "过大", ZipReader(bytes(data)).read, "xl/workbook.xml")
        self.assertEqual(ZipReader(zip_files({"a.txt": "hello"}, False)).text("a.txt"), "hello")
        self.assertRaisesRegex(Blocked, "标签不匹配", parse_xml, "<a><b></a>")
        self.assertRaisesRegex(Blocked, "未闭合", parse_xml, "<a>")
        self.assertEqual(parse_xml('<a x="&lt;1&#65;&#x42;">&amp;<![CDATA[<z>]]></a>').attrs["x"], "<1AB")
        self.assertEqual(parse_xml('<!DOCTYPE a [<!ENTITY e "boom">]><a>&e;</a>').text, "&e;", "custom entities are not expanded")

    def test_a1(self) -> None:
        """A1 arithmetic and formula filling."""
        self.assertEqual([column_letter(n) for n in (1, 26, 27, 702, 703)], ["A", "Z", "AA", "ZZ", "AAA"])
        self.assertEqual(column_index("AAA"), 703)
        self.assertEqual(str(CellRange("B2:A1")), "A1:B2")
        self.assertEqual(str(CellRange("C3:C3")), "C3")
        self.assertTrue(CellRange("A1:C3").contains("B2"))
        self.assertFalse(CellRange("A1:C3").contains("D2"))
        formula = '=CONCATENATE("GOFCN",A5,"_A1_",$D5,"_",E$5,"_",$F$5,LOG10(B5),"x""A9",\'Sheet 1\'!C5)'
        self.assertEqual(translate_formula(formula, "C5", "C8"),
                         '=CONCATENATE("GOFCN",A8,"_A1_",$D8,"_",E$5,"_",$F$5,LOG10(B8),"x""A9",\'Sheet 1\'!C8)')
        self.assertEqual(translate_formula("=A1+B2", "C3", "D5"), "=B3+C4")
        self.assertRaisesRegex(Blocked, "越界", translate_formula, "=A1", "B2", "A1")

    def test_sheet_period(self) -> None:
        """A sheet name gives the month it ends in, the year rolling over when the span crosses January."""
        self.assertEqual(sheet_period("26年1月-GOFCN"), (2026, 1))
        self.assertEqual(sheet_period("GOFCN 2025年11月"), (2025, 11))
        self.assertEqual(sheet_period("GOFCN 2025年10月-11月"), (2025, 11))
        self.assertEqual(sheet_period("GOFCN 2025年12月-1月"), (2026, 1))
        self.assertEqual(sheet_period("GOFCN 25年12月至2月"), (2026, 2))
        self.assertIsNone(sheet_period("GOFCN 2025年13月"))


if __name__ == "__main__":
    unittest.main()
