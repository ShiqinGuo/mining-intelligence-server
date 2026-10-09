from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from mining_server.domain.core import Contract
from mining_server.domain.model import (
    ModelChannel,
    OpenAIProtocol,
    validate_openai_base_url,
)


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


class InvocationState(StrEnum):
    INTENT = "intent"
    COMPLETE = "complete"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


class WorkerTaskName(StrEnum):
    EXECUTE = "mining.execute"
    RECOVER = "mining.recover"
    SOURCES_DUE = "mining.sources_due"
    HEALTH = "mining.health"


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


class TaskExecutionDefaults(Contract):
    initial_generation: int = Field(default=0, ge=0)
    initial_checkpoint: int = Field(default=0, ge=0)
    initial_revision: int = Field(default=1, ge=1)
    revision_increment: int = Field(default=1, ge=1)
    runtime_config_slot: int = Field(default=1, ge=1)
    single_record_limit: int = Field(default=1, ge=1)
    recovery_batch_size: int = Field(default=100, ge=1)
    idempotency_key_length: int = Field(default=200, ge=1)
    heartbeat_divisor: int = Field(default=3, ge=2)
    busy_retry_seconds: int = Field(default=30, ge=1)
    quota_retry_seconds: int = Field(default=3600, ge=1)
    auth_retry_seconds: int = Field(default=300, ge=1)
    failure_retry_limit: int = Field(default=3, ge=0, le=10)
    failure_retry_seconds: int = Field(default=30, ge=1)
    failure_retry_multiplier: int = Field(default=2, ge=1, le=10)


class TaskEnvelope(Contract):
    task_id: UUID
    generation: int = Field(ge=0)
    contract_version: int = Field(default=1, ge=1, le=1)


class OutboxDelivery(Contract):
    message_id: UUID
    owner: UUID
    envelope: TaskEnvelope


class TaskOutcome[T: BaseModel](Contract):
    state: TaskState = TaskState.SUCCEEDED
    result: T


class TaskActionRequest(Contract):
    operation: TaskOperation
    acknowledge_unknown: bool = False


class RuntimeSettingsValues(Contract):
    model_name: str = Field(default="gpt-6.1-sol", min_length=1, max_length=100)
    model_channel: ModelChannel = ModelChannel.CHATGPT_SUBSCRIPTION
    openai_base_url: str = "https://api.openai.com/v1"
    openai_protocol: OpenAIProtocol = OpenAIProtocol.CHAT_COMPLETIONS
    source_interval_seconds: int = Field(default=3600, ge=60, le=86400)
    model_concurrency: int = Field(default=1, ge=1, le=4)
    document_model_rounds: int = Field(default=20, ge=1, le=100)
    document_tool_calls: int = Field(default=80, ge=1, le=400)
    document_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    document_visible_pages: int = Field(default=30, ge=1, le=200)

    @field_validator("model_name")
    @classmethod
    def valid_model_name(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("model_name must not contain whitespace")
        return value

    @field_validator("openai_base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        return validate_openai_base_url(value)


class RuntimeSettings(RuntimeSettingsValues):
    revision: int = 1


class RuntimeSettingsUpdate(Contract):
    expected_revision: int = Field(ge=1)
    settings: RuntimeSettingsValues
