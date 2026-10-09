from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import ConfigDict, Field, HttpUrl

from mining_contracts.domain.core import Contract


class SourceState(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class EntityKind(StrEnum):
    ORGANIZATION = "organization"
    PROJECT = "project"
    LOCATION = "location"
    COMMODITY = "commodity"
    PERSON = "person"


class NewsEntity(Contract):
    name: str = Field(min_length=1, max_length=200)
    kind: EntityKind
    evidence: str = Field(min_length=1, max_length=1000)


class SummaryFact(Contract):
    statement: str = Field(min_length=1, max_length=1000)
    evidence: str = Field(min_length=1, max_length=2000)


class ArticleAnalysis(Contract):
    model_config = ConfigDict(extra="forbid")
    relevant: bool
    entities: list[NewsEntity] = Field(default_factory=list, max_length=30)
    summary: list[SummaryFact] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=10)
    article_id: UUID
    article_revision: int
    model: str
    prompt_version: str
    response_id: str
    completed_at: datetime


class FeedBody(Contract):
    content: str = Field(min_length=1)


class Article(Contract):
    url: HttpUrl
    title: str = Field(min_length=1)
    published_at: datetime | None = None
    summary: str | None = None
    feed_body: FeedBody | None = Field(default=None, exclude=True)
    content: str = Field(min_length=1)
    content_hash: str
    fetched_at: datetime
    publisher: str
    source_id: UUID | None = None
    project: str | None = None
    id: UUID
    revision: int = Field(ge=1)
    discovered_at: datetime
    analysis: ArticleAnalysis | None = None
    analysis_task_id: UUID | None = None


class ArticleSummary(Contract):
    id: UUID
    url: HttpUrl
    title: str
    publisher: str
    source_id: UUID | None = None
    project: str | None = None
    published_at: datetime | None = None
    fetched_at: datetime
    discovered_at: datetime
    revision: int = Field(ge=1)
    content_hash: str
    excerpt: str = Field(max_length=1200)
    truncated: bool
    analysis: ArticleAnalysis | None = None
    analysis_task_id: UUID | None = None


class SourceCoverage(Contract):
    source_id: UUID
    name: str
    state: SourceState
    last_success_at: datetime | None


class NewsSearchRequest(Contract):
    query: str = Field(min_length=1, max_length=500)
    days: int = Field(default=7, ge=1, le=3650)
    project: str | None = None
    limit: int = Field(default=30, ge=1, le=100)


class NewsSearchResponse(Contract):
    articles: list[ArticleSummary]
    sources: list[SourceCoverage]
    truncated: bool
    searched_at: datetime
    time_filter_notice: str = "Dated articles use publication time; undated articles use discovery time for filtering while publication time remains unknown"


class ArticleFetchRequest(Contract):
    url: HttpUrl


class CollectionResult(Contract):
    source_id: UUID
    revision: int
    discovered: int
    stored: int
    cursor: datetime | None
    document_task_ids: list[UUID] = Field(default_factory=list)
    article_task_ids: list[UUID] = Field(default_factory=list)
