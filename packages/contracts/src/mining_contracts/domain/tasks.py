from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from mining_contracts.domain.core import Contract


class TaskKind(StrEnum):
    COLLECT_SOURCE = "collect_source"
    FETCH_ARTICLE = "fetch_article"
    DOCUMENT_INGEST = "document_ingest"
    RESOURCE_EXTRACTION = "resource_extraction"
    COLLECT_PRICES = "collect_prices"
    NEWS_ANALYSIS = "news_analysis"


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_AUTH = "waiting_auth"
    WAITING_QUOTA = "waiting_quota"
    RETRY_WAIT = "retry_wait"
    PARTIAL = "partial"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class StepKind(StrEnum):
    DOWNLOAD_DOCUMENT = "download_document"
    PARSE_DOCUMENT = "parse_document"
    STORE_DOCUMENT = "store_document"
    SAVE_DOCUMENT_PAGES = "save_document_pages"
    EXTRACT_RESOURCES = "extract_resources"
    MODEL_DECISION = "model_decision"
    DOCUMENT_TOOL = "document_tool"
    ARTICLE_FETCH = "article_fetch"
    SOURCE_COLLECT = "source_collect"
    PRICE_COLLECT = "price_collect"
    ARTICLE_STORE = "article_store"
    SOURCE_COMMIT = "source_commit"
    PRICE_STORE = "price_store"
    NEWS_ANALYSIS = "news_analysis"
    NEWS_ANALYSIS_STORE = "news_analysis_store"

    def storage_key(self) -> str:
        return StepKey(kind=self).storage_key()


class StepKey(Contract):
    kind: StepKind
    round: int = Field(default=0, ge=0)

    def storage_key(self) -> str:
        return f"{self.kind}:{self.round}"

    @classmethod
    def from_storage_key(cls, value: str) -> StepKey:
        kind, round_value = value.rsplit(":", 1)
        return cls(kind=StepKind(kind), round=int(round_value))


class TaskOperation(StrEnum):
    RESUME = "resume"
    RETRY_FAILED_STEP = "retry_failed_step"
    REPROCESS = "reprocess"
    CANCEL = "cancel"


class TaskActionRequest(Contract):
    operation: TaskOperation
    acknowledge_unknown: bool = False
