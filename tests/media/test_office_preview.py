import io
from zipfile import ZipFile

import pytest

from tgdrive.media.books import parse_book

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def office_samples():
    def package(parts):
        stream = io.BytesIO()
        with ZipFile(stream, "w") as archive:
            for path, contents in parts.items():
                archive.writestr(path, contents)
        return stream.getvalue()

    return {
        "Meeting.docx": package(
            {
                "word/document.xml": f'<w:document xmlns:w="{WORD}"><w:body>'
                '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                "<w:r><w:t>Meeting notes</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>Review &lt;draft&gt;</w:t></w:r></w:p>"
                "</w:body></w:document>"
            }
        ),
        "Budget.xlsx": package(
            {
                "xl/workbook.xml": f'<workbook xmlns="{SHEET}" xmlns:r="{REL}">'
                '<sheets><sheet name="Summary" r:id="a"/>'
                '<sheet name="Forecast" r:id="b"/>'
                '<sheet name="Wide" r:id="c"/><sheet name="Empty" r:id="d"/>'
                "</sheets></workbook>",
                "xl/_rels/workbook.xml.rels": f'<Relationships><Relationship Id="a" '
                f'Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/>'
                f'<Relationship Id="b" Type="{REL}/worksheet" '
                'Target="/xl/worksheets/sheet2.xml"/>'
                f'<Relationship Id="c" Type="{REL}/worksheet" '
                'Target="worksheets/sheet3.xml"/>'
                f'<Relationship Id="d" Type="{REL}/worksheet" '
                'Target="worksheets/sheet4.xml"/>'
                "</Relationships>",
                "xl/sharedStrings.xml": f'<sst xmlns="{SHEET}">'
                "<si><r><t>Gross </t></r><r><t>income</t></r></si></sst>",
                "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{SHEET}">'
                '<sheetData><row><c r="B1" t="s"><v>0</v></c>'
                '<c r="AB1"><v>200</v></c></row></sheetData></worksheet>',
                "xl/worksheets/sheet2.xml": f'<worksheet xmlns="{SHEET}">'
                '<sheetData><row><c r="A1" t="inlineStr">'
                "<is><t>Next quarter</t></is></c>"
                '<c r="B1"><f>20+22</f><v>42</v></c></row>'
                "</sheetData></worksheet>",
                "xl/worksheets/sheet3.xml": f'<worksheet xmlns="{SHEET}">'
                '<sheetData><row><c r="A1" t="inlineStr"><is><t>'
                + "long_value_"
                * 35
                + '</t></is></c><c r="T1" t="inlineStr"><is><t>Last column</t></is></c>'
                "</row></sheetData></worksheet>",
                "xl/worksheets/sheet4.xml": f'<worksheet xmlns="{SHEET}">'
                "<sheetData/></worksheet>",
            }
        ),
        "Long notes.docx": package(
            {
                "word/document.xml": f'<w:document xmlns:w="{WORD}"><w:body>'
                '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>'
                + "LongHeading" * 20
                + "</w:t></w:r></w:p>"
                + (
                    "<w:p><w:r><w:t>Long document paragraph for reading."
                    "</w:t></w:r></w:p>"
                )
                * 60
                + "<w:p><w:r><w:t>End of notes</w:t></w:r></w:p>"
                + "</w:body></w:document>"
            }
        ),
        "Damaged.docx": b"not a zip",
    }


@pytest.mark.asyncio
async def test_worksheet_preserves_blank_cells_and_bounds_preview():
    content = office_samples()["Budget.xlsx"]

    async def fetch(start, size):
        return content[start : start + size]

    data = {"name": "Budget.xlsx", "size": len(content)}
    first = await parse_book(data, fetch)
    assert first["rows"] == [["", "Gross income"]]
    assert first["truncated"]
    second = await parse_book(data, fetch, "1")
    assert second["rows"] == [["Next quarter", "42"]]
    assert not second["truncated"]
