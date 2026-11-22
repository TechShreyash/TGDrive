import io
from base64 import b64decode
from zipfile import ZipFile

import pytest

from tgdrive.media.books import parse_book

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

PRESENTATION = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
ODF = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
}


def office_samples():
    image_png = b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklE"
        "QVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg=="
    )

    def package(parts):
        stream = io.BytesIO()
        with ZipFile(stream, "w") as archive:
            for path, contents in parts.items():
                archive.writestr(path, contents)
        return stream.getvalue()

    def odf(kind, content, **resources):
        namespaces = " ".join(f'xmlns:{key}="{value}"' for key, value in ODF.items())
        return package(
            {
                **resources,
                "content.xml": f"<office:document-content {namespaces}>"
                f"<office:body><office:{kind}>{content}</office:{kind}></office:body>"
                "</office:document-content>",
            }
        )

    chart_ns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    chart = (
        f'<p:graphicFrame><a:graphic><a:graphicData uri="{chart_ns}">'
        '<c:chart r:id="chart"/></a:graphicData></a:graphic></p:graphicFrame>'
    )
    chart_deck = {
        "ppt/presentation.xml": (
            f'<p:presentation xmlns:p="{PRESENTATION}" xmlns:r="{REL}">'
            "<p:sldIdLst>"
            + "".join(f'<p:sldId r:id="slide{index}"/>' for index in range(3))
            + "</p:sldIdLst></p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": (
            "<Relationships>"
            + "".join(
                f'<Relationship Id="slide{index}" Type="{REL}/slide" '
                f'Target="slides/slide{index}.xml"/>'
                for index in range(3)
            )
            + "</Relationships>"
        ),
        "ppt/charts/chart1.xml": f'<c:chartSpace xmlns:c="{chart_ns}"/>',
    }
    for index, content in enumerate(
        (chart, "<a:p><a:r><a:t>Chart title</a:t></a:r></a:p>" + chart, "")
    ):
        chart_deck[f"ppt/slides/slide{index}.xml"] = (
            f'<p:sld xmlns:p="{PRESENTATION}" xmlns:a="{DRAWING}" '
            f'xmlns:c="{chart_ns}" xmlns:r="{REL}">'
            f"<p:cSld><p:spTree>{content}</p:spTree></p:cSld></p:sld>"
        )
        if index < 2:
            chart_deck[f"ppt/slides/_rels/slide{index}.xml.rels"] = (
                f'<Relationships><Relationship Id="chart" Type="{REL}/chart" '
                'Target="../charts/chart1.xml"/></Relationships>'
            )

    return {
        "Charts.pptx": package(chart_deck),
        "Charts.odp": odf(
            "presentation",
            '<draw:page><draw:object xlink:href="./Object 1"/></draw:page>'
            "<draw:page><text:p>Chart title</text:p>"
            '<draw:object-ole xlink:href="./Object 2"/></draw:page>'
            "<draw:page><presentation:notes><text:p>Presenter cue</text:p>"
            '<draw:object xlink:href="./Object 1"/></presentation:notes></draw:page>',
        ),
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
        "Briefing.pptx": package(
            {
                "ppt/presentation.xml": f'<p:presentation xmlns:p="{PRESENTATION}" '
                f'xmlns:r="{REL}">'
                '<p:sldIdLst><p:sldId r:id="b"/><p:sldId r:id="a"/></p:sldIdLst>'
                "</p:presentation>",
                "ppt/_rels/presentation.xml.rels": "<Relationships>"
                f'<Relationship Id="a" Type="{REL}/slide" Target="slides/slide1.xml"/>'
                f'<Relationship Id="b" Type="{REL}/slide" '
                'Target="/ppt/slides/slide2.xml"/>'
                "</Relationships>",
                "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{PRESENTATION}" '
                f'xmlns:a="{DRAWING}">'
                "<a:p><a:r><a:t>Rollout checklist</a:t></a:r></a:p></p:sld>",
                "ppt/slides/slide2.xml": f'<p:sld xmlns:p="{PRESENTATION}" '
                f'xmlns:a="{DRAWING}" xmlns:r="{REL}">'
                "<a:p><a:r><a:t>Team briefing</a:t></a:r></a:p>"
                "<a:p><a:r><a:t>Review the local deployment before opening access."
                "</a:t></a:r></a:p>"
                '<a:blip r:embed="image"/><a:blip r:embed="external"/></p:sld>',
                "ppt/slides/_rels/slide2.xml.rels": "<Relationships>"
                f'<Relationship Id="image" Type="{REL}/image" '
                'Target="../media/cover.png"/>'
                f'<Relationship Id="external" Type="{REL}/image" '
                'TargetMode="External" Target="https://example.invalid/image.png"/>'
                "</Relationships>",
                "ppt/media/cover.png": image_png,
            }
        ),
        "Open slides.odp": odf(
            "presentation",
            "<draw:page><text:p>Open presentation</text:p>"
            "<presentation:notes><text:p>Presenter cue</text:p>"
            '<draw:image xlink:href="Pictures/notes.png"/>'
            "</presentation:notes></draw:page>"
            "<draw:page><text:p>Second slide</text:p></draw:page>"
            '<draw:page><draw:image xlink:href="Pictures/cover.png"/></draw:page>'
            "<draw:page/>",
            **{"Pictures/cover.png": image_png, "Pictures/notes.png": image_png},
        ),
        "Open notes.odt": odf(
            "text",
            "<text:h>Open document</text:h>"
            '<text:p>Alpha<text:s text:c="2"/>Beta<text:tab/>'
            "Gamma<text:line-break/>Delta</text:p>",
        ),
        "Open budget.ods": odf(
            "spreadsheet",
            '<table:table table:name="Stock"><table:table-row>'
            '<table:table-cell table:number-columns-repeated="2" />'
            '<table:table-cell office:value="12"/>'
            "</table:table-row></table:table>"
            '<table:table table:name="Prices">'
            '<table:table-row table:number-rows-repeated="100000">'
            '<table:table-cell office:value="4" '
            'table:number-columns-repeated="100000"/>'
            "</table:table-row></table:table>",
        ),
        "Read me.md": chr(10)
        .join(
            (
                "# Local setup",
                "",
                "Use **Docker** to start TGDrive.",
                "",
                "- Start the server",
                "- Open My Files",
                "",
                "![External image](https://example.invalid/image.png)",
                "",
                "<script>externalScript()</script>",
            )
        )
        .encode(),
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


@pytest.mark.asyncio
async def test_common_office_previews_preserve_slide_order_and_document_content():
    samples = office_samples()

    async def preview(name, entry=None):
        payload = samples[name]

        async def fetch(start, size):
            return payload[start : start + size]

        return await parse_book({"name": name, "size": len(payload)}, fetch, entry)

    slides = await preview("Briefing.pptx")
    assert slides["kind"] == "presentation" and slides["total"] == 2
    assert slides["paragraphs"][0]["text"] == "Team briefing"
    assert [image["mime"] for image in slides["images"]] == ["image/png"]
    assert slides["truncated"]
    odp = await preview("Open slides.odp")
    assert odp["paragraphs"] == [{"text": "Open presentation", "heading": False}]
    assert odp["images"] == [] and not odp["truncated"]
    assert (await preview("Briefing.pptx", "1"))["paragraphs"][0][
        "text"
    ] == "Rollout checklist"
    assert (await preview("Open slides.odp", "1"))["paragraphs"][0][
        "text"
    ] == "Second slide"
    document = await preview("Open notes.odt")
    assert document["paragraphs"][0] == {"text": "Open document", "heading": True}
    assert (
        document["paragraphs"][1]["text"]
        == "Alpha  Beta" + chr(9) + "Gamma" + chr(10) + "Delta"
    )
    assert (await preview("Open budget.ods"))["rows"] == [["", "", "12"]]
    sheet = await preview("Open budget.ods", "1")
    assert sheet["truncated"] and sheet["rows"] == [["4"] * 20] * 100


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Charts.pptx", "Charts.odp"])
async def test_omitted_chart_is_partial_but_blank_slide_is_not(name):
    content = office_samples()[name]

    async def fetch(start, size):
        return content[start : start + size]

    rows = [
        await parse_book({"name": name, "size": len(content)}, fetch, str(index))
        for index in range(3)
    ]
    assert all(row["kind"] == "presentation" and row["total"] == 3 for row in rows)
    assert [row["paragraphs"] for row in rows] == [
        [],
        [{"text": "Chart title", "heading": False}],
        [],
    ]
    assert [row["images"] for row in rows] == [[], [], []]
    assert [row["truncated"] for row in rows] == [True, True, False]
