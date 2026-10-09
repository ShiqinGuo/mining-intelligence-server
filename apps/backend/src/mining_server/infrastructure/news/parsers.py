import hashlib
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import HTTP_PORT, HTTPS_PORT
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from mining_contracts.domain.core import ErrorCode
from mining_contracts.domain.http import HttpScheme
from mining_contracts.domain.news import FeedBody

from mining_server.domain.core import fail
from mining_server.domain.news import (
    ArticleContent,
    DiscoveredArticle,
    FeedBodyPolicy,
    HtmlElementAttributes,
    NewsLimits,
    Source,
    SourceKind,
)
from mining_server.infrastructure.news.html import element_attributes


def canonical_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in {HttpScheme.HTTP, HttpScheme.HTTPS}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise fail(ErrorCode.INVALID_INPUT, "Article URL must be public HTTP or HTTPS")
    parameters = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ]
    hostname = parts.hostname.lower()
    port = parts.port
    netloc = (
        hostname
        if port is None
        or (parts.scheme == HttpScheme.HTTPS and port == HTTPS_PORT)
        or (parts.scheme == HttpScheme.HTTP and port == HTTP_PORT)
        else f"{hostname}:{port}"
    )
    return urlunsplit(
        (parts.scheme.lower(), netloc, parts.path or "/", urlencode(parameters), "")
    )


def domain_allowed(url: str, source: Source) -> bool:
    hostname = urlsplit(url).hostname
    return hostname is not None and any(
        hostname == allowed or hostname.endswith("." + allowed)
        for allowed in source.rules.allowed_domains
    )


def parse_timestamp(raw: str | None, source: Source) -> datetime | None:
    if not raw or not raw.strip():
        return None
    value = raw.strip()
    try:
        if source.rules.date_format:
            result = (
                datetime.strptime(value, source.rules.date_format).astimezone()
                if "%z" in source.rules.date_format
                else datetime.strptime(value, source.rules.date_format).replace(
                    tzinfo=ZoneInfo(source.rules.timezone)
                )
            )
        else:
            try:
                result = datetime.fromisoformat(value)
            except ValueError:
                result = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise fail(
            ErrorCode.UPSTREAM_FAILURE, "Source publication timestamp is invalid"
        ) from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo(source.rules.timezone))
    return result.astimezone(UTC)


def parse_rss(raw: bytes, source: Source) -> list[DiscoveredArticle]:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise fail(ErrorCode.UPSTREAM_FAILURE, "Invalid RSS or Atom document") from exc
    atom = "{http://www.w3.org/2005/Atom}"
    items = root.findall(".//item")
    if not items:
        items = root.findall(f"{atom}entry")
    output = []
    seen = set()
    for item in items:
        title = item.findtext("title") or item.findtext(f"{atom}title")
        link = item.findtext("link")
        if not link:
            link_node = item.find(f"{atom}link[@rel='alternate']")
            if link_node is None:
                link_node = item.find(f"{atom}link")
            link = (
                HtmlElementAttributes.model_validate(link_node.attrib).href
                if link_node is not None
                else None
            )
        if not title or not link:
            continue
        url = canonical_url(urljoin(str(source.url), link))
        if not domain_allowed(url, source) or url in seen:
            continue
        seen.add(url)
        published = (
            item.findtext("pubDate")
            or item.findtext(f"{atom}published")
            or item.findtext(f"{atom}updated")
        )
        summary = item.findtext("description") or item.findtext(f"{atom}summary")
        feed_body = None
        if source.rules.feed_body_policy == FeedBodyPolicy.FEED_FULL_TEXT:
            body = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
            if body is None:
                node = item.find(f"{atom}content")
                if node is not None:
                    body = (
                        "".join(
                            ElementTree.tostring(child, encoding="unicode")
                            for child in node
                        )
                        if len(node)
                        else node.text
                    )
            if not body or not body.strip():
                raise fail(
                    ErrorCode.UPSTREAM_FAILURE,
                    "Configured feed full body was not found",
                )
            feed_body = parse_feed_body(body)
        output.append(
            DiscoveredArticle(
                feed_body=feed_body,
                url=url,
                title=BeautifulSoup(title, "html.parser").get_text(" ", strip=True),
                published_at=parse_timestamp(published, source),
                summary=BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)
                if summary
                else None,
            )
        )
    return output[: source.max_items]


def parse_html_list(raw: bytes, source: Source) -> list[DiscoveredArticle]:
    soup = BeautifulSoup(raw, "html.parser")
    output = []
    seen = set()
    for selected in soup.select(source.rules.list_selector):
        link = selected if selected.name == "a" else selected.select_one("a[href]")
        if link is None or not element_attributes(link).href:
            continue
        candidate = urljoin(str(source.url), element_attributes(link).href)
        if urlsplit(candidate).scheme not in {HttpScheme.HTTP, HttpScheme.HTTPS}:
            continue
        url = canonical_url(candidate)
        if not domain_allowed(url, source) or url in seen:
            continue
        if source.rules.link_pattern and not re.search(source.rules.link_pattern, url):
            continue
        direct_title = " ".join(
            text.strip()
            for text in link.find_all(string=True, recursive=False)
            if text.strip()
        )
        title = (
            direct_title
            or link.get_text(" ", strip=True)
            or (element_attributes(link).title or "").strip()
        )
        if not title:
            continue
        node = selected.select_one(source.rules.time_selector)
        time_value = (
            (element_attributes(node).date_time or node.get_text(" ", strip=True))
            if node
            else None
        )
        seen.add(url)
        output.append(
            DiscoveredArticle(
                url=url,
                title=title,
                published_at=parse_timestamp(str(time_value), source)
                if time_value
                else None,
            )
        )
        if len(output) == source.max_items:
            break
    return output


def discover(raw: bytes, source: Source) -> list[DiscoveredArticle]:
    match source.kind:
        case SourceKind.RSS:
            return parse_rss(raw, source)
        case SourceKind.HTML_LIST | SourceKind.DOCUMENT_URLS:
            return parse_html_list(raw, source)


def parse_article(
    raw: bytes,
    url: str,
    source: Source,
    fetched_at: datetime,
    discovered: DiscoveredArticle | None = None,
) -> ArticleContent:
    soup = sanitize_article_html(raw)
    title_node = soup.select_one(source.rules.title_selector)
    content_node = soup.select_one(source.rules.article_selector)
    if content_node is None:
        raise fail(
            ErrorCode.UPSTREAM_FAILURE, "Configured article content was not found"
        )
    title = (
        title_node.get_text(" ", strip=True)
        if title_node
        else discovered.title
        if discovered
        else ""
    )
    content = content_node.get_text("\n", strip=True)
    if not title:
        raise fail(ErrorCode.UPSTREAM_FAILURE, "Public article title is missing")
    validate_article_body(content)
    time_node = soup.select_one(source.rules.time_selector)
    time_value = (
        str(
            element_attributes(time_node).date_time
            or time_node.get_text(" ", strip=True)
        )
        if time_node
        else None
    )
    published = (
        parse_timestamp(time_value, source)
        if time_value
        else discovered.published_at
        if discovered
        else None
    )
    return ArticleContent(
        url=canonical_url(url),
        title=title,
        published_at=published,
        summary=discovered.summary if discovered else None,
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        fetched_at=fetched_at,
        publisher=source.publisher,
        source_id=source.id,
        project=source.rules.project,
    )


def validate_article_body(content: str) -> None:
    if len(content) < NewsLimits().minimum_article_characters:
        raise fail(
            ErrorCode.UPSTREAM_FAILURE, "Public article body is missing or incomplete"
        )
    if re.search(
        r"enable javascript|checking your browser|\u6b63\u5728\u8fdb\u884c\u5b89\u5168\u68c0\u67e5|Access Denied",
        content,
        re.IGNORECASE,
    ):
        raise fail(ErrorCode.UPSTREAM_FAILURE, "Source returned an access challenge")


def sanitize_article_html(raw: bytes | str) -> BeautifulSoup:
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup.select("script,style,nav,footer,aside,form"):
        node.decompose()
    return soup


def parse_feed_body(raw: str) -> FeedBody:
    soup = sanitize_article_html(raw)
    for node in soup.select(".sharedaddy,.share-buttons,.related-posts"):
        node.decompose()
    for node in soup.select("p"):
        if re.fullmatch(
            r"The post .+ appeared first on .+\.?", node.get_text(" ", strip=True)
        ):
            node.decompose()
    content = soup.get_text("\n", strip=True)
    validate_article_body(content)
    return FeedBody(content=content)


def parse_feed_article(
    item: DiscoveredArticle, source: Source, fetched_at: datetime
) -> ArticleContent:
    if item.feed_body is None:
        raise fail(
            ErrorCode.UPSTREAM_FAILURE, "Configured feed full body was not found"
        )
    content = item.feed_body.content
    return ArticleContent(
        url=item.url,
        title=item.title,
        published_at=item.published_at,
        summary=item.summary,
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        fetched_at=fetched_at,
        publisher=source.publisher,
        source_id=source.id,
        project=source.rules.project,
    )
