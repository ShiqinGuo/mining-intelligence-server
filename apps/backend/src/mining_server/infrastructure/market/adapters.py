import re
from calendar import Month, monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TypedDict
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from mining_contracts.domain.core import ErrorCode, ErrorDetails
from mining_contracts.domain.http import HttpScheme
from mining_contracts.domain.market import PriceAdapter, PriceInstrument, PricePoint
from pydantic import TypeAdapter, ValidationError

from mining_server.domain.core import fail
from mining_server.domain.documents import ParsedPage
from mining_server.domain.market import (
    MarketBatch,
    MarketLimits,
    MysteelEnvelope,
    MysteelHistory,
    MysteelStatus,
    MysteelUnit,
    ReportPublicationMonth,
    ReportQuarter,
    SinaDay,
)
from mining_server.infrastructure.documents.http import PublicHttpClient
from mining_server.infrastructure.documents.parser import PdfParser
from mining_server.infrastructure.documents.storage import DocumentStorage
from mining_server.infrastructure.news.html import element_attributes


@dataclass(frozen=True)
class MarketQuery:
    index_code: str | None = None
    start: date | None = None
    end: date | None = None
    symbol: str | None = None


@dataclass(frozen=True)
class MarketFetch:
    text: str
    source_url: str


@dataclass(frozen=True)
class ReportDateRules:
    year_century: int = 2000
    quarter_count: int = 2


def parse_pls_report(
    page: ParsedPage, instrument: PriceInstrument, fetched_at: datetime, source_url: str
) -> MarketBatch:
    text = page.text
    published_match = re.search(r"(\d{1,2})\s+(July|October)\s+(20\d{2})", text)
    columns = re.findall(r"(Jun|Mar|Sep)\s+Q\s+FY(\d{2})", text)
    if page.tables is None:
        raise fail(ErrorCode.UPSTREAM_FAILURE, "PLS report has no table extraction")
    price_rows = [
        row
        for table in page.tables
        for row in table
        if any(
            cell is not None and re.fullmatch(r"US\$/t\s+SC6", cell.strip())
            for cell in row
        )
    ]
    if (
        published_match is None
        or len(columns) < ReportDateRules().quarter_count
        or len(price_rows) != 1
    ):
        raise fail(
            ErrorCode.UPSTREAM_FAILURE, "PLS report lacks a verified SC6 quarter table"
        )
    row = price_rows[0]
    unit_column = next(
        index
        for index, cell in enumerate(row)
        if cell is not None and re.fullmatch(r"US\$/t\s+SC6", cell.strip())
    )
    values = []
    for cell in row[
        unit_column + 1 : unit_column + 1 + ReportDateRules().quarter_count
    ]:
        match = (
            re.fullmatch(r"(\d+)(?:<sup>[^<]+</sup>)?", cell.strip())
            if cell is not None
            else None
        )
        if match is None:
            raise fail(ErrorCode.UPSTREAM_FAILURE, "PLS SC6 table has an invalid price")
        values.append(match.group(1))
    if len(values) != ReportDateRules().quarter_count:
        raise fail(ErrorCode.UPSTREAM_FAILURE, "PLS SC6 table has missing quarters")
    day, month, year = published_match.groups()
    match ReportPublicationMonth(month):
        case ReportPublicationMonth.JULY:
            publication_month = Month.JULY
        case ReportPublicationMonth.OCTOBER:
            publication_month = Month.OCTOBER
    published = datetime(
        int(year),
        publication_month,
        int(day),
        tzinfo=ZoneInfo("Australia/Perth"),
    )
    points = []
    for (quarter, fiscal_year), value in zip(
        columns[: ReportDateRules().quarter_count], values, strict=True
    ):
        year = (
            ReportDateRules().year_century
            + int(fiscal_year)
            - (1 if ReportQuarter(quarter) == ReportQuarter.SEPTEMBER else 0)
        )
        match ReportQuarter(quarter):
            case ReportQuarter.MARCH:
                start, end = (
                    date(year, Month.JANUARY, 1),
                    date(year, Month.MARCH, monthrange(year, Month.MARCH)[1]),
                )
            case ReportQuarter.JUNE:
                start, end = (
                    date(year, Month.APRIL, 1),
                    date(year, Month.JUNE, monthrange(year, Month.JUNE)[1]),
                )
            case ReportQuarter.SEPTEMBER:
                start, end = (
                    date(year, Month.JULY, 1),
                    date(year, Month.SEPTEMBER, monthrange(year, Month.SEPTEMBER)[1]),
                )
        points.append(
            PricePoint(
                instrument_id=instrument.id,
                observed_date=end,
                period_start=start,
                period_end=end,
                value=Decimal(value),
                estimated=True,
                publication_date=published.date(),
                asof_at=fetched_at,
                fetched_at=fetched_at,
                source_url=source_url,
                evidence=f"PDF page 1; realised price US$/t SC6; {quarter} Q FY{fiscal_year}; {value}; issuer SC6 equivalent, estimated CIF China; original shipped grade differs; publication date precision is one day",
            )
        )
    return MarketBatch(points=points)


def parse_mysteel(
    raw: str, instrument: PriceInstrument, fetched_at: datetime, source_url: str
) -> MarketBatch:
    try:
        envelope = MysteelEnvelope.model_validate_json(raw)
        if envelope.status != MysteelStatus.SUCCESS or not envelope.isValid:
            raise fail(
                ErrorCode.UPSTREAM_FAILURE, "Mysteel rejected the history request"
            )
        histories = TypeAdapter(list[MysteelHistory]).validate_json(envelope.response)
    except ValidationError as exc:
        raise fail(
            ErrorCode.UPSTREAM_FAILURE, "Invalid Mysteel history response"
        ) from exc
    matches = [
        history
        for history in histories
        if history.indexCode == instrument.source_symbol
    ]
    if len(matches) != 1:
        raise fail(
            ErrorCode.UPSTREAM_FAILURE,
            "Requested Mysteel instrument is missing or duplicated",
        )
    history = matches[0]
    if history.unitName != MysteelUnit.YUAN_PER_TONNE:
        raise fail(ErrorCode.UPSTREAM_FAILURE, "Unexpected Mysteel price unit")
    series = [
        item for item in history.datas if item.indexCode == instrument.source_symbol
    ]
    if len(series) != 1 or len(series[0].yAxis) != len(history.xAxis):
        raise fail(
            ErrorCode.UPSTREAM_FAILURE, "Mysteel dates and prices are not aligned"
        )
    if len(set(history.xAxis)) != len(history.xAxis):
        raise fail(
            ErrorCode.UPSTREAM_FAILURE, "Mysteel contains duplicate observation dates"
        )
    points = []
    for observed, value in zip(history.xAxis, series[0].yAxis, strict=True):
        if not value.is_finite() or value <= 0:
            raise fail(
                ErrorCode.UPSTREAM_FAILURE, "Mysteel quote must be positive and finite"
            )
        points.append(
            PricePoint(
                instrument_id=instrument.id,
                observed_date=observed,
                value=value,
                asof_at=fetched_at,
                fetched_at=fetched_at,
                source_url=source_url,
                evidence=f"{history.indexName}; {observed.isoformat()}; {value} {history.unitName}; actual publication timestamp not supplied",
            )
        )
    return MarketBatch(points=points)


def parse_sina(
    raw: str, instrument: PriceInstrument, fetched_at: datetime, source_url: str
) -> MarketBatch:
    match = re.fullmatch(
        r"\s*(?:/\*.*?\*/\s*)?var\s+_[A-Z0-9]+\s*=\s*\((\[.*\])\)\s*;?\s*",
        raw,
        re.DOTALL,
    )
    if match is None:
        raise fail(ErrorCode.UPSTREAM_FAILURE, "Invalid Sina JSONP wrapper")
    try:
        rows = TypeAdapter(list[SinaDay]).validate_json(match.group(1))
    except ValidationError as exc:
        raise fail(ErrorCode.UPSTREAM_FAILURE, "Invalid Sina daily quote") from exc
    if len({row.d for row in rows}) != len(rows):
        raise fail(ErrorCode.UPSTREAM_FAILURE, "Sina contains duplicate trading dates")
    points = []
    for row in rows:
        if row.l > row.h or not row.l <= row.c <= row.h or not row.l <= row.o <= row.h:
            raise fail(ErrorCode.UPSTREAM_FAILURE, "Sina OHLC range is inconsistent")
        points.append(
            PricePoint(
                instrument_id=instrument.id,
                observed_date=row.d,
                value=row.c,
                low=row.l,
                high=row.h,
                open=row.o,
                settlement=row.s,
                volume=row.v,
                open_interest=row.p,
                asof_at=fetched_at,
                fetched_at=fetched_at,
                source_url=source_url,
                evidence=f"Sina {instrument.source_symbol}; trading date {row.d}; close={row.c}; settlement={row.s}; publication time not provided",
            )
        )
    return MarketBatch(points=points)


def discover_spodumene_articles(raw: bytes, source_url: str) -> list[str]:
    soup = BeautifulSoup(raw, "html.parser")
    urls: set[str] = set()
    for link in soup.select("a[href]"):
        title = link.get_text(" ", strip=True)
        if not (
            "Mysteel\u9502\u7535\u65e9\u8bfb" in title
            or re.search(
                r"Mysteel\u65e5\u62a5\s*[:\uff1a].*(?:\u78b3\u9178\u9502|\u9502\u8f89\u77f3|\u9502\u77ff)",
                title,
            )
        ):
            continue
        parsed = urlsplit(urljoin(source_url, element_attributes(link).href))
        if (
            parsed.scheme not in {HttpScheme.HTTPS, HttpScheme.HTTP}
            or parsed.netloc not in {"xny.mysteel.com"}
            or re.fullmatch(r"/a/\d{8}/[A-Za-z0-9]+\.html", parsed.path) is None
        ):
            continue
        urls.add(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))
    if not urls:
        raise fail(
            ErrorCode.UPSTREAM_FAILURE,
            "Mysteel homepage has no discoverable lithium market articles",
        )
    return sorted(urls, key=lambda url: urlsplit(url).path, reverse=True)[
        : MarketLimits().discovered_article_limit
    ]


def parse_spodumene_article(
    raw: str, instrument: PriceInstrument, fetched_at: datetime, source_url: str
) -> MarketBatch:
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.select("script,style,nav,footer"):
        tag.decompose()
    body = (
        soup.select_one("#article-content")
        or soup.select_one("article")
        or soup.select_one(".article-content")
    )
    if body is None:
        raise fail(ErrorCode.UPSTREAM_FAILURE, "Public market article body is missing")
    text = (body or soup).get_text(" ", strip=True)
    year_match = re.search(r"/(\d{2})\d{6}/", source_url)
    if year_match is None:
        raise fail(ErrorCode.UPSTREAM_FAILURE, "Article year is missing")
    year = ReportDateRules().year_century + int(year_match.group(1))
    pattern = r"(?P<month>\d{1,2})\u6708(?P<day>\d{1,2})\u65e5[^\u3002]{0,500}?(?:\u6fb3\u5927\u5229\u4e9a|\u6fb3\u6d32)[^\u3002]{0,80}?(?:CIF[^\u3002]{0,10}6|6%?[^\u3002]{0,10}CIF)[^\u3002]{0,40}?(?P<low>\d{3,5})\s*[-–—~]\s*(?P<high>\d{3,5})[^\u3002]{0,30}?(?:\u7f8e\u5143/\u5428|\u7f8e\u5143\/\u5428|USD/t)"
    match = re.search(pattern, text, re.IGNORECASE)
    if match is None:
        return MarketBatch(points=[])
    observed = date(year, int(match.group("month")), int(match.group("day")))
    low, high = Decimal(match.group("low")), Decimal(match.group("high"))
    nearby = text[
        max(0, match.start() - MarketLimits().evidence_prefix_characters) : match.end()
        + MarketLimits().evidence_suffix_characters
    ]
    mid_match = re.search(
        r"(?:\u4e2d\u95f4\u4ef7|\u4e2d\u4ef7|\u5747\u4ef7|\u8fdc\u671f\u73b0\u8d27\u4ef7\u683c\s*\u4e3a)[^\d]{0,10}(\d{3,5})",
        nearby,
    )
    value = Decimal(mid_match.group(1)) if mid_match else (low + high) / 2
    if low > high or not low <= value <= high:
        raise fail(ErrorCode.UPSTREAM_FAILURE, "Article price range is inconsistent")
    publication_node = soup.select_one(".publish-time, time[datetime]")
    publication_text = (
        str(
            element_attributes(publication_node).date_time
            or publication_node.get_text(" ", strip=True)
        )
        if publication_node
        else text
    )
    published_match = re.search(
        r"(20\d{2}-\d{2}-\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)", publication_text
    )
    published = (
        datetime.fromisoformat(" ".join(published_match.groups())).replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
        if published_match
        else None
    )
    return MarketBatch(
        points=[
            PricePoint(
                instrument_id=instrument.id,
                observed_date=observed,
                value=value,
                low=low,
                high=high,
                derived=mid_match is None,
                published_at=published,
                asof_at=fetched_at,
                fetched_at=fetched_at,
                source_url=source_url,
                evidence=nearby,
            )
        ]
    )


class MarketHttpAdapter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        storage: DocumentStorage,
        parser: PdfParser,
        max_bytes: int = MarketLimits().response_bytes,
    ):
        self.client = client
        self.storage = storage
        self.parser = parser
        self.max_bytes = max_bytes

    async def read(self, url: str, params: MarketQuery | None = None) -> MarketFetch:
        class QueryParameters(TypedDict, total=False):
            indexCodes: str
            startTime: str
            endTime: str
            symbol: str

        query: QueryParameters = {}
        if params is not None:
            if params.index_code is not None:
                query["indexCodes"] = params.index_code
            if params.start is not None:
                query["startTime"] = params.start.isoformat()
            if params.end is not None:
                query["endTime"] = params.end.isoformat()
            if params.symbol is not None:
                query["symbol"] = params.symbol
        try:
            async with self.client.stream(
                "GET", url, params=query, follow_redirects=False
            ) as response:
                if response.is_redirect:
                    raise fail(
                        ErrorCode.UPSTREAM_FAILURE,
                        "Market source redirected unexpectedly",
                    )
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in response.aiter_bytes(
                    chunk_size=MarketLimits().stream_chunk_bytes
                ):
                    chunks.extend(chunk)
                    if len(chunks) > self.max_bytes:
                        raise fail(
                            ErrorCode.UPSTREAM_FAILURE,
                            "Market response exceeds the size limit",
                        )
                return MarketFetch(
                    text=chunks.decode(response.encoding or "utf-8"),
                    source_url=str(response.request.url),
                )
        except (httpx.HTTPError, UnicodeDecodeError) as exc:
            raise fail(
                ErrorCode.UPSTREAM_FAILURE,
                "Market source request failed",
                ErrorDetails(retryable=True, reason=type(exc).__name__),
            ) from exc

    async def collect(
        self, instrument: PriceInstrument, start: date, end: date
    ) -> MarketBatch:
        fetched_at = datetime.now(UTC)
        match instrument.adapter:
            case PriceAdapter.MYSTEEL_JSON:
                url = "https://openapi.mysteel.com/publishd/index/chart/dateData"
                params = MarketQuery(
                    index_code=instrument.source_symbol, start=start, end=end
                )
                raw = await self.read(url, params)
                return parse_mysteel(raw.text, instrument, fetched_at, raw.source_url)
            case PriceAdapter.SINA_DAILY:
                url = f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{instrument.source_symbol}=/InnerFuturesNewService.getDailyKLine"
                params = MarketQuery(symbol=instrument.source_symbol)
                raw = await self.read(url, params)
                batch = parse_sina(raw.text, instrument, fetched_at, raw.source_url)
                return MarketBatch(
                    points=[
                        point
                        for point in batch.points
                        if start <= point.observed_date <= end
                    ]
                )
            case PriceAdapter.MYSTEEL_ARTICLE:
                url = str(instrument.source_url)
                public = PublicHttpClient(MarketLimits().public_timeout_seconds)
                response = await public.get(url, MarketLimits().response_bytes)
                soup = BeautifulSoup(response.data, "html.parser")
                if soup.select_one("#article-content"):
                    urls = [response.final_url]
                else:
                    urls = discover_spodumene_articles(
                        response.data, response.final_url
                    )
                points = []
                parsed_points = False
                for article_url in urls:
                    article = (
                        response
                        if article_url == response.final_url
                        else await public.get(
                            article_url, MarketLimits().response_bytes
                        )
                    )
                    batch = parse_spodumene_article(
                        article.data.decode("utf-8"),
                        instrument,
                        fetched_at,
                        article.final_url,
                    )
                    parsed_points = parsed_points or bool(batch.points)
                    points.extend(
                        point
                        for point in batch.points
                        if start <= point.observed_date <= end
                    )
                if not parsed_points:
                    raise fail(
                        ErrorCode.UPSTREAM_FAILURE,
                        "Mysteel articles contain no verified spodumene quote",
                    )
                return MarketBatch(points=points)
            case PriceAdapter.PLS_REPORT:
                urls = [
                    "https://announcements.asx.com.au/asxpdf/20250730/pdf/06m8x0wx5j7r91.pdf",
                    "https://announcements.asx.com.au/asxpdf/20251024/pdf/06qz382w4nt85r.pdf",
                ]
                points = []
                for url in urls:
                    response = await PublicHttpClient(
                        MarketLimits().public_timeout_seconds
                    ).get(url, MarketLimits().report_bytes)

                    async def chunks(data: bytes):
                        yield data

                    stored = await self.storage.store(chunks(response.data))
                    parsed = await self.parser.parse(self.storage.path(stored.sha256))
                    batch = parse_pls_report(
                        parsed.pages[0],
                        instrument,
                        fetched_at,
                        response.final_url,
                    )
                    points.extend(
                        point
                        for point in batch.points
                        if start <= point.observed_date <= end
                    )
                return MarketBatch(points=points)
