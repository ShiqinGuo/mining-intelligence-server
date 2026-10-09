import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
import pymupdf
import pytest
from bs4 import BeautifulSoup
from mining_contracts.domain.core import ErrorCode
from mining_contracts.domain.market import PriceAdapter, PriceInstrument
from pydantic import TypeAdapter

from mining_server.domain.core import DomainError
from mining_server.domain.documents import PublicBytesResponse
from mining_server.domain.market import (
    MarketLimits,
    MysteelEnvelope,
    MysteelHistory,
    MysteelSeries,
    MysteelStatus,
    MysteelUnit,
)
from mining_server.domain.news import Source, SourceKind, SourceRules
from mining_server.infrastructure.documents.http import PublicHttpClient
from mining_server.infrastructure.documents.parser import PdfParser
from mining_server.infrastructure.documents.storage import DocumentStorage
from mining_server.infrastructure.market.adapters import (
    MarketHttpAdapter,
    discover_spodumene_articles,
    parse_mysteel,
    parse_pls_report,
    parse_sina,
    parse_spodumene_article,
)
from mining_server.infrastructure.market.seeds import defaults
from mining_server.infrastructure.news.documents import read_document_article

DAILY_ARTICLE_URL = "https://xny.mysteel.com/a/26100820/151833A2AC7B0828.html"
DAILY_ARTICLE_TITLE = "Mysteel\u65e5\u62a5\uff1a\u8282\u540e\u9996\u65e5\u78b3\u9178\u9502\u6210\u4ea4\u6e05\u6de1 \u5e02\u573a\u4ee5\u89c2\u671b\u4e3a\u4e3b"
DAILY_ARTICLE = (
    '<span class="publish-time">2026-10-08 20:50</span>'
    '<div id="article-content">'
    "10\u67088\u65e5\u6fb3\u5927\u5229\u4e9aCIF6%\u4e2d\u56fd\u9502\u8f89\u77f3\u7cbe\u77ff\u62a51670-1720\u7f8e\u5143/\u5428\u3002"
    "</div>"
)


@pytest.fixture
def quarter_pdf() -> bytes:
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((40, 45), "24 October 2025")
        for left, right in zip((40, 180, 260, 360), (180, 260, 360, 460), strict=True):
            for top, bottom in zip((60, 90, 120), (90, 120, 150), strict=True):
                page.draw_rect((left, top, right, bottom))
        page.insert_text((265, 80), "Sep Q FY26")
        page.insert_text((365, 80), "Jun Q FY25")
        page.insert_text((45, 110), "Realised price")
        page.insert_text((185, 110), "US$/t")
        page.insert_text((265, 110), "742")
        page.insert_text((283, 105), "2", fontsize=7)
        page.insert_text((365, 110), "599")
        page.insert_text((383, 105), "3", fontsize=7)
        page.insert_text((185, 140), "US$/t SC6")
        page.insert_text((265, 140), "841")
        page.insert_text((365, 140), "703")
        assert "7422" in page.get_text()
        return document.tobytes()


async def test_pdf_extraction_separates_numeric_superscripts_and_preserves_cells(
    tmp_path, quarter_pdf
):
    path = tmp_path / "quarter.pdf"
    path.write_bytes(quarter_pdf)
    parser = PdfParser()
    parsed = await parser.parse(path)
    table = await parser.table(path, 1)
    assert parsed.pages[0].tables == table.tables
    cell = BeautifulSoup(table.tables[0][1][2], "html.parser")
    marker = cell.find("sup")
    assert marker is not None
    assert marker.get_text() == "2"
    marker.extract()
    assert Decimal(cell.get_text().strip()) == Decimal(742)
    assert "7422" not in parsed.pages[0].text
    assert table.tables[0][2][2:] == ["841", "703"]
    rendered = await parser.render(path, 1)
    assert rendered.page_number == 1
    assert rendered.image_data_url.startswith("data:image/png;base64,")


async def test_news_and_issuer_prices_consume_the_same_pdf_extraction(
    tmp_path, quarter_pdf
):
    storage = DocumentStorage(tmp_path)

    async def chunks():
        yield quarter_pdf

    stored = await storage.store(chunks())
    parsed = await PdfParser().parse(storage.path(stored.sha256))
    source = Source(
        name="Quarterly report",
        publisher="ASX",
        kind=SourceKind.DOCUMENT_URLS,
        url="https://announcements.asx.com.au/report.pdf",
        rules=SourceRules(allowed_domains=["announcements.asx.com.au"]),
        id=uuid4(),
        revision=1,
        next_due_at=datetime.now(UTC),
    )
    news = await read_document_article(
        quarter_pdf, str(source.url), source, datetime.now(UTC), None, tmp_path
    )
    assert news.content == parsed.pages[0].text
    index = next(
        index
        for index, definition in enumerate(defaults())
        if definition.adapter == PriceAdapter.PLS_REPORT
    )
    prices = parse_pls_report(
        parsed.pages[0], instrument(index), datetime.now(UTC), str(source.url)
    )
    assert [(point.observed_date, point.value) for point in prices.points] == [
        (date(2025, 9, 30), Decimal(841)),
        (date(2025, 6, 30), Decimal(703)),
    ]


def instrument(index: int = 0) -> PriceInstrument:
    value = defaults()[index]
    return PriceInstrument(
        slug=value.slug,
        name=value.name,
        commodity=value.commodity,
        grade=value.grade,
        purity_basis=value.purity_basis,
        purity_min=value.purity_min,
        hydrate_form=value.hydrate_form,
        region=value.region,
        origin=value.origin,
        issuer=value.issuer,
        project=value.project,
        price_kind=value.price_kind,
        frequency=value.frequency,
        currency=value.currency,
        unit=value.unit,
        tax_basis=value.tax_basis,
        delivery_basis=value.delivery_basis,
        session=value.session,
        adapter=value.adapter,
        source_symbol=value.source_symbol,
        source_url=value.source_url,
        methodology_url=value.methodology_url,
        methodology_version=value.methodology_version,
        usage_notice=value.usage_notice,
        enabled=value.enabled,
        id=uuid4(),
        revision=1,
    )


def test_mysteel_pairs_dates_and_decimal_values_without_inventing_publication():
    history = MysteelHistory(
        xAxis=[date(2026, 10, 8), date(2026, 9, 30)],
        unitName=MysteelUnit.YUAN_PER_TONNE,
        datas=[
            MysteelSeries(
                yAxis=[Decimal(122000), Decimal(120000)], indexCode="ID01551919"
            )
        ],
        indexName="battery grade China",
        indexCode="ID01551919",
    )
    raw = MysteelEnvelope(
        status=MysteelStatus.SUCCESS,
        message="OK",
        isValid=True,
        response=TypeAdapter(list[MysteelHistory]).dump_json([history]).decode(),
    ).model_dump_json()
    batch = parse_mysteel(
        raw, instrument(), datetime.now(UTC), "https://www.mysteel.com/mmlc/"
    )
    assert batch.points[0].value == Decimal(122000)
    assert batch.points[0].observed_date == date(2026, 10, 8)
    assert batch.points[0].published_at is None
    assert batch.points[1].observed_date == date(2026, 9, 30)


@pytest.mark.parametrize("values", [["122000", None], ["NaN"], ["0"], ["-10"], []])
def test_mysteel_rejects_invalid_quotes_and_alignment(values):
    history = (
        '[{"xAxis":["2026-10-08"],"unitName":"元/吨","datas":[{"yAxis":'
        + json.dumps(values)
        + ',"indexCode":"ID01551919"}],"indexName":"battery grade China","indexCode":"ID01551919"}]'
    )
    raw = MysteelEnvelope(
        status=MysteelStatus.SUCCESS, message="OK", isValid=True, response=history
    ).model_dump_json()
    with pytest.raises(DomainError) as caught:
        parse_mysteel(
            raw, instrument(), datetime.now(UTC), "https://www.mysteel.com/mmlc/"
        )
    assert caught.value.code == ErrorCode.UPSTREAM_FAILURE


def test_sina_real_quote_keeps_close_separate_from_settlement_and_holiday_gaps():
    raw = '/* daily */var _LC2701=([{"d":"2026-09-30","o":"119500","h":"120480","l":"117580","c":"118420","v":"160461","p":"412703","s":"118820"},{"d":"2026-10-08","o":"118280","h":"125900","l":"117200","c":"117300","v":"227730","p":"409678","s":"121540"}]);'
    batch = parse_sina(
        raw, instrument(3), datetime.now(UTC), "https://stock2.finance.sina.com.cn/"
    )
    assert len(batch.points) == 2
    assert batch.points[-1].value == Decimal(117300)
    assert batch.points[-1].settlement == Decimal(121540)
    assert batch.points[-1].volume == 227730
    assert batch.points[-1].published_at is None


def test_access_challenge_is_not_empty_market_history():
    with pytest.raises(DomainError):
        parse_sina(
            "<html>Checking your browser</html>",
            instrument(3),
            datetime.now(UTC),
            "https://stock2.finance.sina.com.cn/",
        )


async def test_market_http_rejects_oversized_payload(tmp_path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="123456789")
        )
    )
    async with client:
        with pytest.raises(DomainError):
            await MarketHttpAdapter(
                client, DocumentStorage(tmp_path), PdfParser(), max_bytes=3
            ).read("https://www.mysteel.com/mmlc/")


def test_spodumene_uses_quote_date_and_explicit_mid_not_article_date():
    raw = (
        '<article><h1>Update (3\u670813\u65e5)</h1><span class="publish-time">2026-03-13 07:30</span><div id="article-content"> \u3010'
        + "\u0033\u6708\u0031\u0032\u65e5\u6fb3\u5927\u5229\u4e9a\u9502\u8f89\u77f3\u8fdc\u671f\u73b0\u8d27\u4ef7\u683c\u4e3a2155\u7f8e\u91d1/\u5428\u3011\u6fb3\u5927\u5229\u4e9aCIF6\u4e2d\u56fd\u9502\u8f89\u77f3\u7cbe\u77ff\u62a52120-2190\u7f8e\u5143/\u5428\u3002"
        + "</div></article>"
    )
    batch = parse_spodumene_article(
        raw,
        instrument(4),
        datetime.now(UTC),
        "https://xny.mysteel.com/a/26031217/D51843650B60AF0D.html",
    )
    assert batch.points[0].observed_date == date(2026, 3, 12)
    assert batch.points[0].value == Decimal(2155)
    assert not batch.points[0].derived


def test_spodumene_discovery_finds_recent_daily_report_and_bounds_unique_articles():
    links = [
        '<a href="/a/26093007/EARLY.html">Mysteel\u9502\u7535\u65e9\u8bfb</a>',
        f'<a href="{DAILY_ARTICLE_URL}">{DAILY_ARTICLE_TITLE}</a>',
        f'<a href="{DAILY_ARTICLE_URL}?tracking=1#quote">{DAILY_ARTICLE_TITLE}</a>',
        *[
            f'<a href="/a/26092{index}07/OLD{index}.html">Mysteel\u9502\u7535\u65e9\u8bfb</a>'
            for index in range(5)
        ],
    ]
    urls = discover_spodumene_articles(
        "".join(links).encode(), "https://xny.mysteel.com/"
    )
    assert urls[0] == DAILY_ARTICLE_URL
    assert len(urls) == MarketLimits().discovered_article_limit
    assert len(set(urls)) == len(urls)
    assert "https://xny.mysteel.com/a/26093007/EARLY.html" in urls


@pytest.mark.parametrize(
    "title,href",
    [
        (
            "Mysteel\u65e5\u62a5\uff1a\u954d\u5e02\u573a\u4ea4\u6613\u6e05\u6de1",
            "/a/26100820/NICKEL.html",
        ),
        (
            "Mysteel\u65e5\u62a5\uff1a\u94b4\u4ef7\u4fdd\u6301\u7a33\u5b9a",
            "/a/26100820/COBALT.html",
        ),
        (
            "Mysteel\u65e5\u62a5\uff1a\u5149\u4f0f\u7845\u6599\u6210\u4ea4\u6e05\u6de1",
            "/a/26100820/SOLAR.html",
        ),
        (DAILY_ARTICLE_TITLE, "https://evil.example/a/26100820/FAKE.html"),
        (
            DAILY_ARTICLE_TITLE,
            "https://xny.mysteel.com.evil.example/a/26100820/FAKE.html",
        ),
        (DAILY_ARTICLE_TITLE, "https://user@xny.mysteel.com/a/26100820/FAKE.html"),
        (DAILY_ARTICLE_TITLE, "/market/price.html"),
    ],
)
def test_spodumene_discovery_rejects_unrelated_reports_and_invalid_urls(title, href):
    with pytest.raises(DomainError) as caught:
        discover_spodumene_articles(
            f'<a href="{href}">{title}</a>'.encode(), "https://xny.mysteel.com/"
        )
    assert caught.value.code == ErrorCode.UPSTREAM_FAILURE


@pytest.mark.parametrize(
    "start,expected_count", [(date(2026, 10, 8), 1), (date(2026, 10, 9), 0)]
)
async def test_spodumene_collects_discovered_daily_report_without_historical_fallback(
    monkeypatch, start, expected_count, tmp_path
):
    requested: list[str] = []

    async def fetch(self, url: str, max_bytes: int) -> PublicBytesResponse:
        requested.append(url)
        if url == "https://xny.mysteel.com/":
            raw = f'<a href="{DAILY_ARTICLE_URL}">{DAILY_ARTICLE_TITLE}</a>'
        else:
            assert url == DAILY_ARTICLE_URL
            raw = DAILY_ARTICLE
        return PublicBytesResponse(
            data=raw.encode(), final_url=url, content_type="text/html"
        )

    monkeypatch.setattr(PublicHttpClient, "get", fetch)
    async with httpx.AsyncClient() as client:
        batch = await MarketHttpAdapter(
            client, DocumentStorage(tmp_path), PdfParser()
        ).collect(instrument(4), start, date(2026, 10, 9))
    assert requested == ["https://xny.mysteel.com/", DAILY_ARTICLE_URL]
    assert len(batch.points) == expected_count
    if batch.points:
        point = batch.points[0]
        assert point.observed_date == date(2026, 10, 8)
        assert point.low == Decimal(1670)
        assert point.high == Decimal(1720)
        assert point.value == Decimal(1695)
        assert point.derived
        assert point.published_at is not None
        assert point.published_at.isoformat() == "2026-10-08T20:50:00+08:00"
        assert str(point.source_url) == DAILY_ARTICLE_URL


@pytest.mark.parametrize(
    "raw",
    ["<html>Checking your browser</html>", '<div id="article-content">No quote</div>'],
)
async def test_spodumene_collect_rejects_discovery_or_quote_failure(
    monkeypatch, raw, tmp_path
):
    async def fetch(self, url: str, max_bytes: int) -> PublicBytesResponse:
        return PublicBytesResponse(
            data=raw.encode(), final_url=DAILY_ARTICLE_URL, content_type="text/html"
        )

    monkeypatch.setattr(PublicHttpClient, "get", fetch)
    async with httpx.AsyncClient() as client:
        with pytest.raises(DomainError) as caught:
            await MarketHttpAdapter(
                client, DocumentStorage(tmp_path), PdfParser()
            ).collect(instrument(4), date(2026, 10, 8), date(2026, 10, 9))
    assert caught.value.code == ErrorCode.UPSTREAM_FAILURE


async def test_market_adapter_serializes_typed_query_and_validates_response_boundary(
    tmp_path,
):
    from mining_server.infrastructure.market.adapters import MarketQuery

    def handle(request):
        assert request.url.params["indexCodes"] == "ID01551919"
        assert request.url.params["startTime"] == "2026-09-30"
        assert request.url.params["endTime"] == "2026-10-08"
        return httpx.Response(
            200, text='{"status":"200","message":"OK","isValid":true,"response":"{}"}'
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        fetched = await MarketHttpAdapter(
            client, DocumentStorage(tmp_path), PdfParser()
        ).read(
            "https://openapi.mysteel.com/publishd/index/chart/dateData",
            MarketQuery(
                index_code="ID01551919", start=date(2026, 9, 30), end=date(2026, 10, 8)
            ),
        )
        with pytest.raises(DomainError) as caught:
            parse_mysteel(
                fetched.text, instrument(), datetime.now(UTC), fetched.source_url
            )
    assert caught.value.code == ErrorCode.UPSTREAM_FAILURE
