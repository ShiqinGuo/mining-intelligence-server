from datetime import UTC, datetime
from uuid import uuid4

import pymupdf
import pytest

from mining_server.domain.core import DomainError, ErrorCode
from mining_server.domain.documents import ParsedDocument, ParsedPage
from mining_server.domain.news import DiscoveredArticle, Source
from mining_server.infrastructure.news.documents import (
    parse_document_article,
    read_document_article,
    resolve_document_link,
)
from mining_server.infrastructure.news.parsers import discover
from mining_server.infrastructure.news.seeds import defaults


def asx_source() -> Source:
    definition = next(
        source for source in defaults() if source.name == "PLS ASX announcements"
    )
    return Source(
        name=definition.name,
        publisher=definition.publisher,
        kind=definition.kind,
        url=definition.url,
        rules=definition.rules,
        interval_minutes=definition.interval_minutes,
        max_items=definition.max_items,
        backfill_days=definition.backfill_days,
        state=definition.state,
        id=uuid4(),
        revision=1,
        next_due_at=datetime.now(UTC),
    )


def test_asx_public_list_preserves_issuer_title_and_sydney_publication_time():
    raw = b'<table><tbody><tr><td>23/09/2026<br><span class="dates-time">11:24 am</span></td><td></td><td><a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&amp;idsId=03142755">September 2026 Quarterly Activities Report advisory<br><span class="page">2 pages</span><span class="filesize">133KB</span></a></td></tr></tbody></table>'
    result = discover(raw, asx_source())
    assert len(result) == 1
    assert result[0].title == "September 2026 Quarterly Activities Report advisory"
    assert result[0].published_at == datetime(2026, 9, 23, 1, 24, tzinfo=UTC)


def test_public_document_link_is_read_without_executing_or_submitting_form():
    raw = b'<p>Private personal investor use</p><form method="post"><input name="pdfURL" value="https://announcements.asx.com.au/asxpdf/20260923/pdf/074f015p42q95d.pdf"><input type="submit" value="Agree and proceed"></form>'
    resolved = resolve_document_link(raw, "input[name=pdfURL]", asx_source())
    assert (
        resolved
        == "https://announcements.asx.com.au/asxpdf/20260923/pdf/074f015p42q95d.pdf"
    )
    with pytest.raises(DomainError) as caught:
        resolve_document_link(
            b'<input name="pdfURL" value="https://attacker.test/report.pdf">',
            "input[name=pdfURL]",
            asx_source(),
        )
    assert caught.value.code == ErrorCode.FORBIDDEN


async def test_pdf_announcement_becomes_news_without_inventing_a_publication_date(
    tmp_path,
):
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text(
            (50, 50),
            "PLS Group Limited advises that the September quarterly report\nwill be released on 27 October 2026. Production guidance is unchanged.",
        )
        raw = pdf.tobytes()
    source = asx_source()
    item = DiscoveredArticle(
        url="https://announcements.asx.com.au/report.pdf",
        title="PLS quarter advisory",
        published_at=datetime(2026, 9, 23, 1, 24, tzinfo=UTC),
    )
    result = await read_document_article(
        raw, str(item.url), source, datetime.now(UTC), item, tmp_path
    )
    assert result.title == item.title
    assert result.published_at == item.published_at
    assert "27 October 2026" in result.content
    assert result.project is None
    assert (
        await read_document_article(
            raw, str(item.url), source, datetime.now(UTC), None, tmp_path
        )
    ).published_at is None


async def test_html_access_challenge_cannot_be_parsed_as_pdf_news(tmp_path):
    with pytest.raises(DomainError):
        await read_document_article(
            b"<html>Just a moment...</html>",
            "https://announcements.asx.com.au/report.pdf",
            asx_source(),
            datetime.now(UTC),
            None,
            tmp_path,
        )


def test_pdf_news_rejects_text_beyond_character_budget():
    parsed = ParsedDocument(
        pages=[ParsedPage(page_number=1, text="a" * 2_000_001, width=600, height=800)]
    )
    with pytest.raises(DomainError) as caught:
        parse_document_article(
            parsed,
            "https://announcements.asx.com.au/report.pdf",
            asx_source(),
            datetime.now(UTC),
            None,
        )
    assert caught.value.code == ErrorCode.INVALID_INPUT
