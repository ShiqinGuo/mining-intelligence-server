from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from html import escape
from itertools import groupby
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, FiniteFloat, TypeAdapter

from mining_server.domain.pdf import PdfPageExtraction

if TYPE_CHECKING:
    from pymupdf import Page


type PdfBox = tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]


class PdfBlockKind(IntEnum):
    TEXT = 0


class PdfTextFormat(StrEnum):
    RAW_DICTIONARY = "rawdict"


class TextCharacter(BaseModel):
    c: str
    bbox: PdfBox


class TextSpan(BaseModel):
    chars: list[TextCharacter]
    flags: int = Field(ge=0)


class TextLine(BaseModel):
    bbox: PdfBox
    spans: list[TextSpan]


class TextBlock(BaseModel):
    type: Literal[PdfBlockKind.TEXT]
    lines: list[TextLine]


class TextLayout(BaseModel):
    blocks: list[TextBlock]


@dataclass(frozen=True)
class TextElement:
    bbox: PdfBox
    text: str


@dataclass(frozen=True)
class TableElement:
    bbox: PdfBox
    rows: list[list[str | None]]


@dataclass(frozen=True)
class TextGlyph:
    order: tuple[int, int, int, int]
    x: float
    y: float
    text: str
    superscript: bool


def read_layout(page: Page) -> TextLayout:
    import pymupdf

    return TextLayout.model_validate(
        page.get_text(
            PdfTextFormat.RAW_DICTIONARY,
            flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES,
            sort=True,
        )
    )


def line_text(line: TextLine) -> str:
    import pymupdf

    parts = []
    for span in line.spans:
        text = escape("".join(character.c for character in span.chars), quote=False)
        if span.flags & pymupdf.TEXT_FONT_SUPERSCRIPT:
            text = f"<sup>{text}</sup>"
        parts.append(text)
    return "".join(parts).strip()


def contains(outer: PdfBox, inner: PdfBox) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def glyph_index(layout: TextLayout) -> tuple[list[TextGlyph], list[float]]:
    import pymupdf

    glyphs = [
        TextGlyph(
            order=(block_index, line_index, span_index, character_index),
            x=(character.bbox[0] + character.bbox[2]) / 2,
            y=(character.bbox[1] + character.bbox[3]) / 2,
            text=character.c,
            superscript=bool(span.flags & pymupdf.TEXT_FONT_SUPERSCRIPT),
        )
        for block_index, block in enumerate(layout.blocks)
        for line_index, line in enumerate(block.lines)
        for span_index, span in enumerate(line.spans)
        for character_index, character in enumerate(span.chars)
    ]
    glyphs.sort(key=lambda glyph: glyph.y)
    return glyphs, [glyph.y for glyph in glyphs]


def cell_text(cell: PdfBox, glyphs: list[TextGlyph], heights: list[float]) -> str:
    candidates = glyphs[bisect_left(heights, cell[1]) : bisect_left(heights, cell[3])]
    selected = sorted(
        (glyph for glyph in candidates if cell[0] <= glyph.x < cell[2]),
        key=lambda glyph: glyph.order,
    )
    lines = []
    for _, line in groupby(selected, key=lambda glyph: glyph.order[:2]):
        parts = []
        for _, span in groupby(line, key=lambda glyph: glyph.order[:3]):
            group = list(span)
            text = escape("".join(glyph.text for glyph in group), quote=False)
            if group[0].superscript:
                text = f"<sup>{text}</sup>"
            parts.append(text)
        lines.append("".join(parts).strip())
    return "\n".join(lines).strip()


def read_tables(page: Page, layout: TextLayout) -> list[TableElement]:
    located = page.find_tables().tables
    if not located:
        return []
    box_adapter = TypeAdapter(PdfBox)
    cells_adapter = TypeAdapter(list[PdfBox | None])
    glyphs, heights = glyph_index(layout)
    tables = []
    for table in located:
        rows = []
        for row in table.rows:
            cells = cells_adapter.validate_python(row.cells)
            values = []
            for cell in cells:
                if cell is None:
                    values.append(None)
                    continue
                values.append(cell_text(cell, glyphs, heights))
            rows.append(values)
        tables.append(TableElement(box_adapter.validate_python(table.bbox), rows))
    return tables


def table_text(table: TableElement) -> str:
    rows = [
        "<tr>"
        + "".join(f"<td>{value if value is not None else ''}</td>" for value in row)
        + "</tr>"
        for row in table.rows
    ]
    return "<table>\n" + "\n".join(rows) + "\n</table>"


def extract_pdf_page(page: Page) -> PdfPageExtraction:
    layout = read_layout(page)
    tables = read_tables(page, layout)
    elements = [
        TextElement(line.bbox, line_text(line))
        for block in layout.blocks
        for line in block.lines
        if not any(contains(table.bbox, line.bbox) for table in tables)
    ]
    elements.extend(TextElement(table.bbox, table_text(table)) for table in tables)
    elements.sort(key=lambda element: (element.bbox[1], element.bbox[0]))
    return PdfPageExtraction(
        text="\n".join(element.text for element in elements if element.text),
        tables=[table.rows for table in tables],
    )
