from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from mining_server.domain.core import ErrorCode, fail
from mining_server.domain.documents import DocumentResponse, ResourceExtractionResult
from mining_server.domain.market import MarketRefreshResult
from mining_server.domain.news import Article, ArticleAnalysis, CollectionResult
from mining_server.domain.task_views import StepView, TaskResult, TaskView
from mining_server.domain.tasks import InvocationState, StepKey, TaskKind
from mining_server.infrastructure.task_models import ModelInvocation, StepRun, TaskRun


class TaskRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, task_id: UUID, lock: bool = False) -> TaskRun:
        statement = select(TaskRun).where(TaskRun.id == task_id)
        if lock:
            statement = statement.with_for_update()
        task = await self.session.scalar(statement)
        if task is None:
            raise fail(ErrorCode.NOT_FOUND, "Task not found")
        return task

    async def uncertain_invocations(self, task_id: UUID) -> list[ModelInvocation]:
        newer = aliased(ModelInvocation)
        return list(
            (
                await self.session.scalars(
                    select(ModelInvocation).where(
                        ModelInvocation.task_id == task_id,
                        ModelInvocation.state.in_(
                            [InvocationState.INTENT, InvocationState.UNKNOWN]
                        ),
                        ~select(newer.id)
                        .where(
                            newer.task_id == ModelInvocation.task_id,
                            newer.name == ModelInvocation.name,
                            newer.attempt > ModelInvocation.attempt,
                        )
                        .exists(),
                    )
                )
            ).all()
        )

    async def view(self, task: TaskRun) -> TaskView:
        steps = (
            await self.session.scalars(
                select(StepRun)
                .where(StepRun.task_id == task.id)
                .order_by(StepRun.completed_at)
            )
        ).all()
        return TaskView(
            id=task.id,
            kind=task.kind,
            state=task.state,
            generation=task.generation,
            workflow_version=task.workflow_version,
            input_revision=task.input_revision,
            checkpoint_seq=task.checkpoint_seq,
            next_step=StepKey.from_storage_key(task.next_step)
            if task.next_step
            else None,
            result=self.result(task),
            error_code=task.error_code,
            error_message=task.error_message,
            created_at=task.created_at,
            updated_at=task.updated_at,
            steps=[
                StepView(
                    key=StepKey.from_storage_key(item.name),
                    completed_at=item.completed_at,
                )
                for item in steps
            ],
        )

    @staticmethod
    def result(task: TaskRun) -> TaskResult | None:
        if task.result_json is None:
            return None
        match task.kind:
            case TaskKind.DOCUMENT_INGEST:
                return DocumentResponse.model_validate_json(task.result_json)
            case TaskKind.RESOURCE_EXTRACTION:
                return ResourceExtractionResult.model_validate_json(task.result_json)
            case TaskKind.FETCH_ARTICLE:
                return Article.model_validate_json(task.result_json)
            case TaskKind.NEWS_ANALYSIS:
                return ArticleAnalysis.model_validate_json(task.result_json)
            case TaskKind.COLLECT_SOURCE:
                return CollectionResult.model_validate_json(task.result_json)
            case TaskKind.COLLECT_PRICES:
                return MarketRefreshResult.model_validate_json(task.result_json)
