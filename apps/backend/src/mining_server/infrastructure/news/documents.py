import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from mining_contracts.domain.core import ErrorCode

from mining_server.domain.core import fail
from mining_server.domain.documents import ParsedDocument
from mining_server.domain.news import (
    ArticleContent,
    DiscoveredArticle,
    NewsLimits,
    Source,
)
from mining_server.infrastructure.documents.parser import PdfParser
from mining_server.infrastructure.documents.storage import DocumentStorage
from mining_server.infrastructure.news.html import element_attributes
from mining_server.infrastructure.news.parsers import canonical_url, domain_allowed


def resolve_document_link(
    raw: bytes, selector: str, source: Source, base_url: str | None = None
) -> str:
    node = BeautifulSoup(raw, "html.parser").select_one(selector)
    if node is None:
        raise fail(
            ErrorCode.UPSTREAM_FAILURE, "Configured public document URL was not found"
        )
    attributes = element_attributes(node)
    target = attributes.href or attributes.value
    if target is None:
        raise fail(ErrorCode.UPSTREAM_FAILURE, "Configured document link has no URL")
    normalized = canonical_url(urljoin(base_url or str(source.url), target))
    if not domain_allowed(normalized, source):
        raise fail(ErrorCode.FORBIDDEN, "Document link is outside source domains")
    return normalized


def parse_document_article(
    document: ParsedDocument,
    url: str,
    source: Source,
    fetched_at: datetime,
    discovered: DiscoveredArticle | None,
) -> ArticleContent:
    characters = sum(len(page.text) + len("\n\n") for page in document.pages)
    if characters > NewsLimits().announcement_characters:
        raise fail(ErrorCode.INVALID_INPUT, "Announcement text exceeds the limit")
    return article_from_text(
        "\n\n".join(page.text.strip() for page in document.pages),
        url,
        source,
        fetched_at,
        discovered,
    )


def article_from_text(
    content: str,
    url: str,
    source: Source,
    fetched_at: datetime,
    discovered: DiscoveredArticle | None,
) -> ArticleContent:
    title = (
        discovered.title
        if discovered
        else next((line for line in content.splitlines() if line.strip()), "")
    )
    if len(content) < NewsLimits().minimum_article_characters or not title:
        raise fail(
            ErrorCode.COVERAGE_INSUFFICIENT,
            "Announcement PDF has insufficient extractable text",
        )
    return ArticleContent(
        url=canonical_url(url),
        title=title,
        published_at=discovered.published_at if discovered else None,
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        fetched_at=fetched_at,
        publisher=source.publisher,
        source_id=source.id,
        project=source.rules.project,
    )


async def read_document_article(
    raw: bytes,
    url: str,
    source: Source,
    fetched_at: datetime,
    discovered: DiscoveredArticle | None,
    data_dir: Path,
) -> ArticleContent:
    storage = DocumentStorage(data_dir)

    async def chunks():
        yield raw

    stored = await storage.store(chunks())
    parser = PdfParser()
    manifest = await parser.parse_to_file(storage.path(stored.sha256))
    text = []
    characters = 0
    async for batch in parser.iter_batches(
        manifest, batch_size=NewsLimits().announcement_page_batch_size
    ):
        for page in batch.pages:
            characters += len(page.text) + len("\n\n")
            if characters > NewsLimits().announcement_characters:
                raise fail(
                    ErrorCode.INVALID_INPUT, "Announcement text exceeds the limit"
                )
            text.append(page.text.strip())
    return article_from_text("\n\n".join(text), url, source, fetched_at, discovered)
