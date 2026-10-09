from datetime import datetime
from uuid import UUID

from pydantic import Field

from mining_contracts.domain.core import Contract, ErrorCode
from mining_contracts.domain.documents import DocumentResponse, ResourceExtractionResult
from mining_contracts.domain.market import MarketRefreshResult
from mining_contracts.domain.news import Article, ArticleAnalysis, CollectionResult
from mining_contracts.domain.tasks import StepKey, TaskKind, TaskState

type TaskResult = (
    DocumentResponse
    | ResourceExtractionResult
    | Article
    | ArticleAnalysis
    | CollectionResult
    | MarketRefreshResult
)


class StepView(Contract):
    key: StepKey
    completed_at: datetime


class TaskView(Contract):
    id: UUID
    kind: TaskKind
    state: TaskState
    generation: int
    workflow_version: str
    input_revision: str
    checkpoint_seq: int
    next_step: StepKey | None
    result: TaskResult | None
    error_code: ErrorCode | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    steps: list[StepView] = Field(default_factory=list)
