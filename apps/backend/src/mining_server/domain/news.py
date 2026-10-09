import re
from datetime import datetime
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mining_contracts.domain.core import Contract
from mining_contracts.domain.news import (
    CollectionResult,
    FeedBody,
    NewsEntity,
    SourceState,
    SummaryFact,
)
from pydantic import ConfigDict, Field, HttpUrl, field_validator, model_validator
from soupsieve import SelectorSyntaxError
from soupsieve import compile as compile_selector


class SourceKind(StrEnum):
    RSS = "rss"
    HTML_LIST = "html_list"
    DOCUMENT_URLS = "document_urls"


class FeedBodyPolicy(StrEnum):
    WEBPAGE = "webpage"
    FEED_FULL_TEXT = "feed_full_text"


class ArticleAnalysisDraft(Contract):
    model_config = ConfigDict(extra="forbid")
    relevant: bool
    entities: list[NewsEntity] = Field(default_factory=list, max_length=30)
    summary: list[SummaryFact] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=10)


class AnalysisPayload(Contract):
    article_id: UUID
    article_revision: int
    content_hash: str
    model: str
    prompt_version: str = "1"


class SourceRules(Contract):
    model_config = ConfigDict(extra="forbid")
    allowed_domains: list[str] = Field(min_length=1)
    feed_body_policy: FeedBodyPolicy = FeedBodyPolicy.WEBPAGE
    list_selector: str = "a[href]"
    article_selector: str = "article"
    title_selector: str = "h1"
    time_selector: str = "time"
    link_pattern: str | None = None
    document_url_selector: str | None = None
    timezone: str = "UTC"
    date_format: str | None = None
    project: str | None = None
    commodities: list[str] = Field(default_factory=list)

    @field_validator("allowed_domains")
    @classmethod
    def validate_domains(cls, value: list[str]) -> list[str]:
        if any(not domain or "/" in domain or ":" in domain for domain in value):
            raise ValueError("Allowed domains must be hostnames")
        return [domain.lower() for domain in value]

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Timezone must be a valid IANA identifier") from error
        return value

    @field_validator("link_pattern")
    @classmethod
    def validate_link_pattern(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError(
                    "Link pattern must be a valid regular expression"
                ) from error
        return value

    @field_validator(
        "list_selector", "article_selector", "title_selector", "time_selector"
    )
    @classmethod
    def validate_selector(cls, value: str) -> str:
        try:
            compile_selector(value)
        except SelectorSyntaxError as error:
            raise ValueError("Selector must be valid CSS") from error
        return value

    @field_validator("document_url_selector")
    @classmethod
    def validate_document_selector(cls, value: str | None) -> str | None:
        if value is not None:
            cls.validate_selector(value)
        return value


class SourceCreate(Contract):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    publisher: str = Field(min_length=1, max_length=200)
    kind: SourceKind
    url: HttpUrl
    rules: SourceRules
    interval_minutes: int = Field(default=60, ge=5, le=10080)
    max_items: int = Field(default=50, ge=1, le=200)
    backfill_days: int = Field(default=7, ge=1, le=3650)
    state: SourceState = SourceState.ENABLED

    @model_validator(mode="after")
    def validate_feed_policy(self) -> "SourceCreate":
        if (
            self.rules.feed_body_policy == FeedBodyPolicy.FEED_FULL_TEXT
            and self.kind != SourceKind.RSS
        ):
            raise ValueError("Feed full text requires an RSS source")
        return self


class SourceUpdate(SourceCreate):
    expected_revision: int = Field(ge=1)


class SourceStateRequest(Contract):
    state: SourceState
    expected_revision: int = Field(ge=1)


class Source(SourceCreate):
    id: UUID
    revision: int = Field(ge=1)
    last_success_at: datetime | None = None
    next_due_at: datetime
    cursor: datetime | None = None
    last_error: str | None = None


class DiscoveredArticle(Contract):
    url: HttpUrl
    title: str = Field(min_length=1)
    published_at: datetime | None = None
    summary: str | None = None
    feed_body: FeedBody | None = None


class ArticleContent(DiscoveredArticle):
    feed_body: FeedBody | None = Field(default=None, exclude=True)
    content: str = Field(min_length=1)
    content_hash: str
    fetched_at: datetime
    publisher: str
    source_id: UUID | None = None
    project: str | None = None


class SourcePreview(Contract):
    source_id: UUID
    revision: int
    items: list[DiscoveredArticle]
    fetched_at: datetime


class CollectionPayload(Contract):
    source: Source


class ArticleFetchPayload(Contract):
    url: HttpUrl
    source: Source | None = None
    discovered: DiscoveredArticle | None = None


class ArticleBatch(Contract):
    discovered: int
    articles: list[ArticleContent]
    fetched_at: datetime
    documents: list[DiscoveredArticle] = Field(default_factory=list)


class CollectionRun(Contract):
    id: UUID
    source_id: UUID
    task_id: UUID
    revision: int
    started_at: datetime
    completed_at: datetime | None = None
    result: CollectionResult | None = None


class SourceList(Contract):
    sources: list[Source]


class CollectionRunList(Contract):
    runs: list[CollectionRun]


class HtmlElementAttributes(Contract):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    href: str | None = None
    value: str | None = None
    title: str | None = None
    date_time: str | None = Field(default=None, alias="datetime")


class NewsLimits(Contract):
    response_bytes: int = Field(default=2_000_000, ge=1)
    document_response_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    announcement_characters: int = Field(default=2_000_000, ge=1)
    announcement_page_batch_size: int = Field(default=1, ge=1)
    analysis_characters: int = Field(default=50_000, ge=1)
    minimum_article_characters: int = Field(default=40, ge=1)
    excerpt_characters: int = Field(default=1200, ge=1)
    preview_items: int = Field(default=5, ge=1)
    collection_run_history: int = Field(default=100, ge=1)
