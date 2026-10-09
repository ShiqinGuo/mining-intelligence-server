from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from mining_contracts.domain.core import ErrorCode, ErrorDetails
from mining_contracts.domain.task_views import TaskView
from mining_contracts.domain.tasks import (
    StepKey,
    StepKind,
    TaskActionRequest,
    TaskKind,
    TaskOperation,
    TaskState,
)
from pydantic import BaseModel
from sqlalchemy import JSON, cast, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mining_server.domain.core import DomainError, fail
from mining_server.domain.model import (
    ModelChannel,
    ModelChannelCheck,
    ModelChannelState,
)
from mining_server.domain.task_lifecycle import TaskLifecycle
from mining_server.domain.tasks import (
    InvocationState,
    RuntimeSettings,
    RuntimeSettingsUpdate,
    TaskEnvelope,
    TaskOutcome,
)
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.settings import Settings
from mining_server.infrastructure.task_models import (
    Checkpoint,
    ModelInvocation,
    OutboxMessage,
    RuntimeConfig,
    StepRun,
    TaskRetryBudget,
    TaskRun,
)
from mining_server.infrastructure.task_repository import TaskRepository


def now() -> datetime:
    return datetime.now(UTC)


def payload_json(model: BaseModel) -> str:
    return model.model_dump_json()


@dataclass(frozen=True)
class AuthRecoveryCandidate:
    task_id: UUID
    generation: int
    channel: ModelChannel
    model_name: str


class TaskService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        repository_factory: type[TaskRepository] = TaskRepository,
    ):
        self.database = database
        self.settings = settings
        self.repositories = repository_factory
        self.defaults = settings.task_defaults

    async def submit(
        self,
        kind: TaskKind,
        payload: BaseModel,
        idempotency_key: str,
        workflow_version: str = "1",
        input_revision: str = "1",
    ) -> TaskView:
        async with self.database.sessions() as session, session.begin():
            return await self.submit_in_session(
                session,
                kind,
                payload,
                idempotency_key,
                workflow_version,
                input_revision,
            )

    async def submit_in_session(
        self,
        session: AsyncSession,
        kind: TaskKind,
        payload: BaseModel,
        idempotency_key: str,
        workflow_version: str = "1",
        input_revision: str = "1",
    ) -> TaskView:
        if not session.in_transaction():
            raise ValueError("Task submission requires an active transaction")
        if (
            not idempotency_key
            or len(idempotency_key) > self.defaults.idempotency_key_length
        ):
            raise fail(
                ErrorCode.INVALID_INPUT,
                "Idempotency key must contain 1 to 200 characters",
            )
        encoded = payload_json(payload)
        digest = hashlib.sha256(
            f"{kind}:{workflow_version}:{input_revision}:{encoded}".encode()
        ).hexdigest()
        task_id = uuid4()
        configuration = await self.runtime_settings_in_session(session)
        inserted = await session.scalar(
            insert(TaskRun)
            .values(
                id=task_id,
                kind=kind,
                state=TaskState.QUEUED,
                payload_json=encoded,
                payload_hash=digest,
                idempotency_key=idempotency_key,
                workflow_version=workflow_version,
                input_revision=input_revision,
                generation=self.defaults.initial_generation,
                checkpoint_seq=self.defaults.initial_checkpoint,
                settings_json=configuration.model_dump_json(),
            )
            .on_conflict_do_nothing(index_elements=[TaskRun.idempotency_key])
            .returning(TaskRun.id)
        )
        task = await session.scalar(
            select(TaskRun).where(TaskRun.idempotency_key == idempotency_key)
        )
        if task.payload_hash != digest:
            raise fail(
                ErrorCode.CONFLICT,
                "Idempotency key was already used with different input",
            )
        if inserted:
            session.add(
                OutboxMessage(
                    task_id=task_id, generation=self.defaults.initial_generation
                )
            )
        await session.flush()
        return await self.repositories(session).view(task)

    async def get(self, task_id: UUID) -> TaskView:
        async with self.database.sessions() as session:
            repository = self.repositories(session)
            return await repository.view(await repository.get(task_id))

    async def claim(self, envelope: TaskEnvelope, owner: UUID) -> TaskContext | None:
        async with self.database.sessions() as session, session.begin():
            task = await self.repositories(session).get(envelope.task_id, lock=True)
            if not TaskLifecycle.can_claim(
                task.state, task.generation, envelope.generation
            ):
                return None
            task.generation += self.defaults.revision_increment
            task.owner = owner
            task.lease_until = now() + timedelta(seconds=self.settings.lease_seconds)

            task.state = TaskState.RUNNING
            task.error_code = None
            task.error_message = None
            task.updated_at = now()
            return TaskContext(
                self,
                task.id,
                task.generation,
                owner,
                task.kind,
                task.payload_json,
                RuntimeSettings.model_validate_json(task.settings_json),
            )

    async def renew(self, context: TaskContext) -> None:
        async with self.database.sessions() as session, session.begin():
            task = await self.repositories(session).get(context.task_id, lock=True)
            context.check_fence(task)
            task.lease_until = now() + timedelta(seconds=self.settings.lease_seconds)
            await session.flush()
            context.check_fence(task)

    async def finish(self, context: TaskContext, outcome: TaskOutcome) -> None:
        if not TaskLifecycle.can_complete(outcome.state):
            raise fail(
                ErrorCode.INVALID_INPUT, "Handler outcome must be succeeded or partial"
            )
        async with self.database.sessions() as session, session.begin():
            task = await self.repositories(session).get(context.task_id, lock=True)
            context.check_fence(task)
            task.state = outcome.state
            lease_until = task.lease_until
            task.result_json = outcome.result.model_dump_json()
            task.owner = None
            task.lease_until = None
            task.updated_at = now()

            await session.flush()
            if lease_until <= now():
                raise fail(
                    ErrorCode.LEASE_LOST, "Task lease expired during result commit"
                )

    async def fail_task(self, context: TaskContext, error: DomainError) -> None:
        if error.code == ErrorCode.LEASE_LOST:
            return
        async with self.database.sessions() as session, session.begin():
            task = await self.repositories(session).get(context.task_id, lock=True)
            if (
                task.generation != context.generation
                or task.owner != context.owner
                or task.lease_until is None
                or task.lease_until <= now()
                or task.state not in (TaskState.RUNNING, TaskState.CANCEL_REQUESTED)
            ):
                return
            uncertain = await self.repositories(session).uncertain_invocations(task.id)
            if uncertain and error.code not in (
                ErrorCode.CANCELLED,
                ErrorCode.UNKNOWN_RESULT,
            ):
                error = fail(
                    ErrorCode.UNKNOWN_RESULT,
                    "Model invocation outcome is uncertain",
                    ErrorDetails(task_id=context.task_id),
                )
            budget = await session.get(TaskRetryBudget, task.id)
            retry_count = budget.failure_retry_count if budget is not None else 0
            decision = TaskLifecycle.failure(
                error, retry_count, self.defaults, task.state
            )
            task.state = decision.state
            task.retry_at = (
                now() + timedelta(seconds=decision.retry_seconds)
                if decision.retry_seconds is not None
                else None
            )
            if decision.consumes_retry:
                if budget is None:
                    budget = TaskRetryBudget(
                        task_id=task.id, failure_retry_count=retry_count
                    )
                    session.add(budget)
                budget.failure_retry_count += self.defaults.revision_increment
            task.error_code = error.code
            task.error_message = error.message
            if uncertain and task.state == TaskState.CANCELLED:
                task.error_code = ErrorCode.UNKNOWN_RESULT
                task.error_message = "Task cancelled with an uncertain model invocation"
            task.owner = None
            task.lease_until = None
            task.updated_at = now()

    async def action(self, task_id: UUID, request: TaskActionRequest) -> TaskView:
        if request.operation == TaskOperation.REPROCESS:
            async with self.database.sessions() as session, session.begin():
                task = await self.repositories(session).get(task_id)
                configuration = await self.runtime_settings_in_session(session)
                digest = hashlib.sha256(
                    f"{task.kind}:{task.workflow_version}:{task.input_revision}:{task.payload_json}".encode()
                ).hexdigest()
                revised = TaskRun(
                    id=uuid4(),
                    kind=task.kind,
                    state=TaskState.QUEUED,
                    payload_json=task.payload_json,
                    payload_hash=digest,
                    settings_json=configuration.model_dump_json(),
                    idempotency_key=f"reprocess:{uuid4()}",
                    workflow_version=task.workflow_version,
                    input_revision=task.input_revision,
                    generation=self.defaults.initial_generation,
                    checkpoint_seq=self.defaults.initial_checkpoint,
                )
                session.add(revised)
                await session.flush()
                session.add(
                    OutboxMessage(
                        task_id=revised.id, generation=self.defaults.initial_generation
                    )
                )
                return await self.repositories(session).view(revised)
        async with self.database.sessions() as session, session.begin():
            task = await self.repositories(session).get(task_id, lock=True)
            match request.operation:
                case TaskOperation.CANCEL:
                    target = TaskLifecycle.cancel(task.state)
                    if (
                        target == TaskState.CANCELLED
                        and task.state != TaskState.CANCELLED
                    ):
                        task.generation += self.defaults.revision_increment
                        task.owner = None
                        task.lease_until = None
                    task.state = target
                case TaskOperation.RESUME | TaskOperation.RETRY_FAILED_STEP:
                    if not TaskLifecycle.can_resume(task.state):
                        raise fail(
                            ErrorCode.CONFLICT, "Task cannot be resumed from this state"
                        )
                    unknown = await self.repositories(session).uncertain_invocations(
                        task.id
                    )
                    if unknown and not request.acknowledge_unknown:
                        raise fail(
                            ErrorCode.UNKNOWN_RESULT,
                            "Explicit acknowledgement is required to repeat uncertain model calls",
                        )
                    for invocation in unknown:
                        invocation.state = InvocationState.UNKNOWN
                    task.generation += self.defaults.revision_increment
                    task.state = TaskState.QUEUED
                    task.owner = None
                    task.lease_until = None
                    task.error_code = None
                    task.error_message = None
                    task.retry_at = None
                    budget = await session.get(TaskRetryBudget, task.id)
                    if budget is not None:
                        budget.failure_retry_count = 0
                    session.add(
                        OutboxMessage(task_id=task.id, generation=task.generation)
                    )
                case _:
                    raise fail(ErrorCode.INVALID_INPUT, "Unsupported task operation")
            task.updated_at = now()
            await session.flush()
            return await self.repositories(session).view(task)

    async def recover_waiting_auth(
        self,
        check: Callable[[ModelChannel], Awaitable[ModelChannelCheck]],
        channel: ModelChannel | None = None,
        immediate: bool = False,
    ) -> int:
        candidates: list[AuthRecoveryCandidate] = []
        async with self.database.sessions() as session, session.begin():
            rows: list[TaskRun] = []
            for selected in ModelChannel:
                if channel is not None and selected != channel:
                    continue
                stored_channel = cast(TaskRun.settings_json, JSON)[
                    "model_channel"
                ].as_string()
                channel_matches = stored_channel == selected
                if selected == ModelChannel.CHATGPT_SUBSCRIPTION:
                    channel_matches = or_(channel_matches, stored_channel.is_(None))
                query = select(TaskRun).where(
                    TaskRun.state == TaskState.WAITING_AUTH, channel_matches
                )
                if not immediate:
                    query = query.where(
                        or_(TaskRun.retry_at.is_(None), TaskRun.retry_at <= now())
                    )
                rows.extend(
                    (
                        await session.scalars(
                            query.order_by(TaskRun.updated_at)
                            .with_for_update(skip_locked=True)
                            .limit(self.defaults.recovery_batch_size)
                        )
                    ).all()
                )
            for task in rows:
                runtime = RuntimeSettings.model_validate_json(task.settings_json)
                if channel is not None and runtime.model_channel != channel:
                    continue
                task.retry_at = now() + timedelta(
                    seconds=self.defaults.auth_retry_seconds
                )
                if runtime.model_channel == ModelChannel.OPENAI_COMPATIBLE and (
                    runtime.openai_base_url != self.settings.openai_base_url
                    or runtime.openai_protocol != self.settings.openai_protocol
                ):
                    continue
                candidates.append(
                    AuthRecoveryCandidate(
                        task.id,
                        task.generation,
                        runtime.model_channel,
                        runtime.model_name,
                    )
                )
        recovered = 0
        for selected in ModelChannel:
            matching = [item for item in candidates if item.channel == selected]
            if not matching:
                continue
            result = await check(selected)
            if result.channel != selected or result.state != ModelChannelState.READY:
                continue
            async with self.database.sessions() as session, session.begin():
                for candidate in matching:
                    if not any(
                        model.slug == candidate.model_name for model in result.models
                    ):
                        continue
                    task = await self.repositories(session).get(
                        candidate.task_id, lock=True
                    )
                    if (
                        task.state != TaskState.WAITING_AUTH
                        or task.generation != candidate.generation
                    ):
                        continue
                    uncertain = await self.repositories(session).uncertain_invocations(
                        task.id
                    )
                    if uncertain:
                        continue
                    task.generation += self.defaults.revision_increment
                    task.state = TaskState.QUEUED
                    task.owner = None
                    task.lease_until = None
                    task.retry_at = None
                    task.error_code = None
                    task.error_message = None
                    task.updated_at = now()
                    session.add(
                        OutboxMessage(task_id=task.id, generation=task.generation)
                    )
                    recovered += 1
        return recovered

    async def recover_due(self) -> int:
        recovered: list[UUID] = []
        async with self.database.sessions() as session, session.begin():
            tasks = (
                await session.scalars(
                    select(TaskRun)
                    .where(
                        or_(
                            (
                                TaskRun.state.in_(
                                    [TaskState.RUNNING, TaskState.CANCEL_REQUESTED]
                                )
                            )
                            & (TaskRun.lease_until < now()),
                            (
                                TaskRun.state.in_(
                                    [TaskState.RETRY_WAIT, TaskState.WAITING_QUOTA]
                                )
                            )
                            & (TaskRun.retry_at <= now()),
                        )
                    )
                    .with_for_update(skip_locked=True)
                    .limit(self.defaults.recovery_batch_size)
                )
            ).all()
            for task in tasks:
                uncertain = await self.repositories(session).uncertain_invocations(
                    task.id
                )
                target = TaskLifecycle.recover(task.state, bool(uncertain))
                match target:
                    case TaskState.CANCELLED:
                        task.state = TaskState.CANCELLED
                        if uncertain:
                            task.error_code = ErrorCode.UNKNOWN_RESULT
                            task.error_message = (
                                "Task cancelled with an uncertain model invocation"
                            )
                    case TaskState.UNKNOWN:
                        task.state = TaskState.UNKNOWN
                        task.error_code = ErrorCode.UNKNOWN_RESULT
                        task.error_message = (
                            "Worker lease expired with an uncertain model invocation"
                        )
                    case _:
                        task.state = TaskState.QUEUED
                        task.generation += self.defaults.revision_increment
                        session.add(
                            OutboxMessage(task_id=task.id, generation=task.generation)
                        )
                        recovered.append(task.id)
                task.owner = None
                task.lease_until = None
                task.updated_at = now()
            cutoff = now() - timedelta(seconds=self.settings.lease_seconds)
            old_delivery = (
                select(OutboxMessage.id)
                .where(
                    OutboxMessage.task_id == TaskRun.id,
                    OutboxMessage.generation == TaskRun.generation,
                    OutboxMessage.published_at < cutoff,
                )
                .exists()
            )
            pending_or_recent = (
                select(OutboxMessage.id)
                .where(
                    OutboxMessage.task_id == TaskRun.id,
                    OutboxMessage.generation == TaskRun.generation,
                    or_(
                        OutboxMessage.published_at.is_(None),
                        OutboxMessage.published_at >= cutoff,
                    ),
                )
                .exists()
            )
            stranded = (
                await session.scalars(
                    select(TaskRun)
                    .where(
                        TaskRun.state == TaskState.QUEUED,
                        old_delivery,
                        ~pending_or_recent,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(self.defaults.recovery_batch_size)
                )
            ).all()
            for task in stranded:
                session.add(OutboxMessage(task_id=task.id, generation=task.generation))
                task.updated_at = now()
                recovered.append(task.id)
        return len(recovered)

    async def runtime_settings(self) -> RuntimeSettings:
        async with self.database.sessions() as session, session.begin():
            return await self.runtime_settings_in_session(session)

    async def runtime_settings_in_session(
        self, session: AsyncSession
    ) -> RuntimeSettings:
        if not session.in_transaction():
            raise ValueError("Runtime settings require an active transaction")
        await session.execute(
            insert(RuntimeConfig)
            .values(
                id=self.defaults.runtime_config_slot,
                revision=self.defaults.initial_revision,
                config_json=RuntimeSettings(
                    model_name=self.settings.model_name,
                    model_channel=self.settings.model_channel,
                    openai_base_url=self.settings.openai_base_url,
                    openai_protocol=self.settings.openai_protocol,
                ).model_dump_json(),
            )
            .on_conflict_do_nothing()
        )
        row = await session.scalar(
            select(RuntimeConfig)
            .where(RuntimeConfig.id == self.defaults.runtime_config_slot)
            .with_for_update()
        )
        return RuntimeSettings.model_validate_json(row.config_json)

    async def update_runtime_settings(
        self, request: RuntimeSettingsUpdate
    ) -> RuntimeSettings:
        if request.settings.model_channel == ModelChannel.OPENAI_COMPATIBLE and (
            request.settings.openai_base_url != self.settings.openai_base_url
            or request.settings.openai_protocol != self.settings.openai_protocol
        ):
            raise fail(
                ErrorCode.INVALID_INPUT,
                "OpenAI endpoint and protocol must match deployment configuration",
            )
        if (
            self.settings.openai_api_key is not None
            and self.settings.openai_api_key.get_secret_value()
            in request.settings.openai_base_url
        ):
            raise fail(
                ErrorCode.INVALID_INPUT,
                "OpenAI API key must not appear in the base URL",
            )
        async with self.database.sessions() as session, session.begin():
            await self.runtime_settings_in_session(session)
            row = await session.scalar(
                select(RuntimeConfig)
                .where(RuntimeConfig.id == self.defaults.runtime_config_slot)
                .with_for_update()
            )
            if row.revision != request.expected_revision:
                raise fail(ErrorCode.CONFLICT, "Runtime settings revision changed")
            updated = RuntimeSettings(
                revision=row.revision + self.settings.task_defaults.revision_increment,
                model_name=request.settings.model_name,
                model_channel=request.settings.model_channel,
                openai_base_url=request.settings.openai_base_url,
                openai_protocol=request.settings.openai_protocol,
                source_interval_seconds=request.settings.source_interval_seconds,
                model_concurrency=request.settings.model_concurrency,
                document_model_rounds=request.settings.document_model_rounds,
                document_tool_calls=request.settings.document_tool_calls,
                document_timeout_seconds=request.settings.document_timeout_seconds,
                document_visible_pages=request.settings.document_visible_pages,
            )
            row.revision = updated.revision
            row.config_json = updated.model_dump_json()
            return updated


class TaskContext:
    def __init__(
        self,
        service: TaskService,
        task_id: UUID,
        generation: int,
        owner: UUID,
        kind: TaskKind,
        payload: str,
        runtime_settings: RuntimeSettings,
    ):
        self.service = service
        self.defaults = service.settings.task_defaults
        self.task_id = task_id
        self.generation = generation
        self.owner = owner
        self.kind = kind
        self.payload_json = payload
        self.runtime_settings = runtime_settings

    def check_fence(self, task: TaskRun) -> None:
        if (
            task.generation != self.generation
            or task.owner != self.owner
            or task.lease_until is None
            or task.lease_until <= now()
        ):
            raise fail(ErrorCode.LEASE_LOST, "Task execution lease was lost")
        match task.state:
            case TaskState.CANCEL_REQUESTED:
                raise fail(ErrorCode.CANCELLED, "Task cancellation was requested")
            case TaskState.RUNNING:
                pass
            case _:
                raise fail(ErrorCode.LEASE_LOST, "Task execution lease was lost")

    async def check_cancelled(self) -> None:
        async with self.service.database.sessions() as session:
            task = await self.service.repositories(session).get(self.task_id)
            self.check_fence(task)

    async def step[T: BaseModel](
        self,
        key: StepKind | StepKey,
        result_type: type[T],
        action: Callable[[], Awaitable[T]],
        next_step: StepKind | StepKey | None = None,
    ) -> T:
        name = key.storage_key()
        next_name = next_step.storage_key() if next_step is not None else None
        async with self.service.database.sessions() as session:
            task = await self.service.repositories(session).get(self.task_id)
            self.check_fence(task)
            row = await session.scalar(
                select(StepRun).where(
                    StepRun.task_id == self.task_id, StepRun.name == name
                )
            )
            if row:
                return result_type.model_validate_json(row.result_json)
        await self.check_cancelled()
        result = await action()
        async with self.service.database.sessions() as session, session.begin():
            task = await self.service.repositories(session).get(self.task_id, lock=True)
            self.check_fence(task)
            task.checkpoint_seq += self.defaults.revision_increment
            task.next_step = next_name
            session.add(
                StepRun(
                    task_id=self.task_id,
                    name=name,
                    result_json=result.model_dump_json(),
                    generation=self.generation,
                )
            )
            session.add(
                Checkpoint(
                    task_id=self.task_id,
                    sequence=task.checkpoint_seq,
                    workflow_version=task.workflow_version,
                    input_revision=task.input_revision,
                    completed_step=name,
                    next_step=next_name,
                    context_json=result.model_dump_json(),
                )
            )
            task.updated_at = now()
            await session.flush()
            self.check_fence(task)

        return result

    async def model_call[T: BaseModel](
        self,
        key: StepKind | StepKey,
        result_type: type[T],
        action: Callable[[], Awaitable[T]],
    ) -> T:
        name = key.storage_key()
        async with self.service.database.sessions() as session, session.begin():
            task = await self.service.repositories(session).get(self.task_id, lock=True)
            self.check_fence(task)
            previous = await session.scalar(
                select(ModelInvocation)
                .where(
                    ModelInvocation.task_id == self.task_id,
                    ModelInvocation.name == name,
                )
                .order_by(ModelInvocation.attempt.desc())
                .limit(self.defaults.single_record_limit)
            )
            if previous and previous.state == InvocationState.COMPLETE:
                return result_type.model_validate_json(previous.response_json)
            if (
                previous
                and previous.generation == self.generation
                and previous.state in (InvocationState.INTENT, InvocationState.UNKNOWN)
            ):
                raise fail(
                    ErrorCode.UNKNOWN_RESULT,
                    "Model call result is uncertain and cannot be replayed",
                )
            current = await self.service.runtime_settings_in_session(session)
            self.check_fence(task)
            active = await session.scalar(
                select(func.count())
                .select_from(ModelInvocation)
                .join(TaskRun, TaskRun.id == ModelInvocation.task_id)
                .where(
                    ModelInvocation.state == InvocationState.INTENT,
                    TaskRun.state.in_([TaskState.RUNNING, TaskState.CANCEL_REQUESTED]),
                    TaskRun.lease_until > now(),
                    ModelInvocation.generation == TaskRun.generation,
                )
            )
            if active >= current.model_concurrency:
                raise fail(
                    ErrorCode.BUSY,
                    "Model concurrency limit reached",
                    ErrorDetails(
                        retryable=True, retry_after=self.defaults.busy_retry_seconds
                    ),
                )
            invocation = ModelInvocation(
                task_id=self.task_id,
                name=name,
                attempt=previous.attempt + self.defaults.revision_increment
                if previous
                else self.defaults.initial_revision,
                state=InvocationState.INTENT,
                generation=self.generation,
            )
            session.add(invocation)
            await session.flush()
            self.check_fence(task)
            invocation_id = invocation.id
        try:
            await self.check_cancelled()
        except DomainError:
            async with self.service.database.sessions() as session, session.begin():
                invocation = await session.get(ModelInvocation, invocation_id)
                invocation.state = InvocationState.REJECTED
            raise
        try:
            result = await action()
        except DomainError as error:
            async with self.service.database.sessions() as session, session.begin():
                invocation = await session.get(ModelInvocation, invocation_id)
                invocation.state = (
                    InvocationState.REJECTED
                    if error.details.definitive_rejection
                    or error.code in (ErrorCode.WAITING_AUTH, ErrorCode.WAITING_QUOTA)
                    else InvocationState.UNKNOWN
                )
            raise
        except BaseException:
            async with self.service.database.sessions() as session, session.begin():
                invocation = await session.get(ModelInvocation, invocation_id)
                invocation.state = InvocationState.UNKNOWN
            raise
        async with self.service.database.sessions() as session, session.begin():
            invocation = await session.get(ModelInvocation, invocation_id)
            invocation.response_json = result.model_dump_json()
            invocation.state = InvocationState.COMPLETE
        await self.check_cancelled()
        return result

    async def transaction_step[T: BaseModel](
        self,
        key: StepKind | StepKey,
        result_type: type[T],
        action: Callable[[AsyncSession], Awaitable[T]],
        next_step: StepKind | StepKey | None = None,
    ) -> T:
        name = key.storage_key()
        next_name = next_step.storage_key() if next_step is not None else None
        async with self.service.database.sessions() as session, session.begin():
            task = await self.service.repositories(session).get(self.task_id, lock=True)
            self.check_fence(task)
            existing = await session.scalar(
                select(StepRun).where(
                    StepRun.task_id == self.task_id, StepRun.name == name
                )
            )
            if existing:
                return result_type.model_validate_json(existing.result_json)
            result = await action(session)
            await session.flush()
            self.check_fence(task)
            task.checkpoint_seq += self.defaults.revision_increment
            task.next_step = next_name
            session.add(
                StepRun(
                    task_id=self.task_id,
                    name=name,
                    result_json=result.model_dump_json(),
                    generation=self.generation,
                )
            )
            session.add(
                Checkpoint(
                    task_id=self.task_id,
                    sequence=task.checkpoint_seq,
                    workflow_version=task.workflow_version,
                    input_revision=task.input_revision,
                    completed_step=name,
                    next_step=next_name,
                    context_json=result.model_dump_json(),
                )
            )
            task.updated_at = now()
            await session.flush()
            self.check_fence(task)
            return result


type TaskHandler[T: BaseModel] = Callable[[TaskContext, T], Awaitable[TaskOutcome]]
type DueSourceHandler = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class HandlerBinding[T: BaseModel]:
    kind: TaskKind
    payload_type: type[T]
    callback: TaskHandler[T]

    async def execute(self, context: TaskContext) -> TaskOutcome:
        payload = self.payload_type.model_validate_json(context.payload_json)
        return await self.callback(context, payload)


class HandlerRegistry:
    def __init__(self):
        self.handlers: list[HandlerBinding] = []
        self.due_sources: list[DueSourceHandler] = []

    def register[T: BaseModel](
        self, kind: TaskKind, payload_type: type[T], handler: TaskHandler[T]
    ) -> None:
        if self.find(kind) is not None:
            raise ValueError("Task handler already registered")
        self.handlers.append(HandlerBinding(kind, payload_type, handler))

    def find(self, kind: TaskKind) -> HandlerBinding | None:
        return next(
            (handler for handler in self.handlers if handler.kind == kind), None
        )

    def register_due_sources(self, handler: DueSourceHandler) -> None:
        self.due_sources.append(handler)

    async def execute(self, service: TaskService, envelope: TaskEnvelope) -> None:
        context = await service.claim(envelope, uuid4())
        if context is None:
            return
        handler = self.find(context.kind)
        if handler is None:
            await service.fail_task(
                context,
                fail(ErrorCode.INVALID_INPUT, "No handler registered for task kind"),
            )
            return

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(
                    service.settings.lease_seconds
                    / service.settings.task_defaults.heartbeat_divisor
                )
                await service.renew(context)

        lease_task = asyncio.create_task(heartbeat())
        handler_task = asyncio.create_task(handler.execute(context))
        try:
            done, _ = await asyncio.wait(
                [lease_task, handler_task], return_when=asyncio.FIRST_COMPLETED
            )
            if lease_task in done:
                handler_task.cancel()
                await asyncio.gather(handler_task, return_exceptions=True)
                await lease_task
                raise fail(ErrorCode.LEASE_LOST, "Task heartbeat stopped")
            outcome = await handler_task
            await service.finish(context, outcome)
        except DomainError as error:
            await service.fail_task(context, error)
        except Exception as error:
            await service.fail_task(
                context,
                fail(
                    ErrorCode.INTERNAL, f"Task handler failed: {type(error).__name__}"
                ),
            )
            raise
        finally:
            lease_task.cancel()
            handler_task.cancel()
            await asyncio.gather(lease_task, handler_task, return_exceptions=True)
