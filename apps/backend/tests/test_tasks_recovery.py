import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from mining_contracts.domain.core import ErrorCode
from mining_contracts.domain.news import Article
from mining_contracts.domain.tasks import (
    StepKind,
    TaskActionRequest,
    TaskKind,
    TaskOperation,
    TaskState,
)
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from mining_server.application.tasks import HandlerRegistry, TaskService, now
from mining_server.domain.core import DomainError, fail
from mining_server.domain.tasks import (
    InvocationState,
    RuntimeSettingsUpdate,
    RuntimeSettingsValues,
    TaskEnvelope,
    TaskOutcome,
)
from mining_server.infrastructure.database import Base, Database
from mining_server.infrastructure.settings import Settings
from mining_server.infrastructure.task_models import (
    Checkpoint,
    ModelInvocation,
    OutboxMessage,
    StepRun,
    TaskRun,
)


class Input(BaseModel):
    value: int


def article_result(value: int = 1) -> Article:
    return Article(
        id=uuid4(),
        revision=value,
        url="https://example.com/article",
        title="Task recovery evidence",
        content="Stored article content",
        content_hash="test-hash",
        fetched_at=now(),
        discovered_at=now(),
        publisher="Test publisher",
    )


@pytest.fixture
async def service():
    url = os.environ.get("MINING_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "MINING_TEST_DATABASE_URL is required for PostgreSQL integration tests"
        )
    settings = Settings(
        database_url=url,
        admin_token="test-admin-token-long-enough",
        service_token="test-service-token-long-enough",
        master_key="MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE=",
        oauth_host_id=f"urn:uuid:{uuid4()}",
        lease_seconds=15,
    )
    database = Database(url)
    schema = f"tasks_test_{uuid4().hex}"
    async with database.engine.begin() as connection:
        await connection.execute(CreateSchema(schema))
        await connection.execution_options(schema_translate_map={None: schema})
        await connection.run_sync(Base.metadata.create_all)
    database.sessions = async_sessionmaker(
        database.engine.execution_options(schema_translate_map={None: schema}),
        expire_on_commit=False,
    )
    try:
        yield TaskService(database, settings)
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await database.close()


async def claim(service: TaskService, task_id):
    view = await service.get(task_id)
    return await service.claim(
        TaskEnvelope(task_id=task_id, generation=view.generation), uuid4()
    )


async def expire(service: TaskService, context):
    async with service.database.sessions() as session, session.begin():
        task = await session.get(TaskRun, context.task_id)
        task.lease_until = now() - timedelta(seconds=1)


async def test_concurrent_idempotency_and_duplicate_delivery(service):
    results = await asyncio.gather(
        *[
            service.submit(TaskKind.FETCH_ARTICLE, Input(value=1), "same-key")
            for _ in range(12)
        ]
    )
    assert len({item.id for item in results}) == 1
    async with service.database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(OutboxMessage)) == 1
        )
    with pytest.raises(DomainError) as raised:
        await service.submit(TaskKind.FETCH_ARTICLE, Input(value=2), "same-key")
    assert raised.value.code == ErrorCode.CONFLICT
    envelope = TaskEnvelope(task_id=results[0].id, generation=0)
    contexts = await asyncio.gather(
        *[service.claim(envelope, uuid4()) for _ in range(5)]
    )
    assert sum(context is not None for context in contexts) == 1


async def test_lease_recovery_fences_old_worker_and_reuses_checkpoint(service):
    view = await service.submit(TaskKind.FETCH_ARTICLE, Input(value=1), "recovery")
    original = await claim(service, view.id)
    calls = 0

    async def action():
        nonlocal calls
        calls += 1
        return Input(value=5)

    await original.step(StepKind.ARTICLE_FETCH, Input, action)
    await expire(service, original)
    assert await service.recover_due() == 1
    replacement = await claim(service, view.id)
    result = await replacement.step(StepKind.ARTICLE_FETCH, Input, action)
    assert result.value == 5
    assert calls == 1
    with pytest.raises(DomainError) as raised:
        await service.finish(original, TaskOutcome(result=article_result()))
    assert raised.value.code == ErrorCode.LEASE_LOST
    await service.finish(replacement, TaskOutcome(result=article_result(value=5)))
    assert (await service.get(view.id)).state == TaskState.SUCCEEDED


async def test_complete_model_response_reused_and_unknown_requires_explicit_action(
    service,
):
    view = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "model-complete"
    )
    context = await claim(service, view.id)
    calls = 0

    async def complete():
        nonlocal calls
        calls += 1
        return Input(value=7)

    await context.model_call(StepKind.MODEL_DECISION, Input, complete)
    await expire(service, context)
    await service.recover_due()
    resumed = await claim(service, view.id)
    assert (
        await resumed.model_call(StepKind.MODEL_DECISION, Input, complete)
    ).value == 7
    assert calls == 1
    second = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=2), "model-unknown"
    )
    uncertain = await claim(service, second.id)

    async def broken():
        raise fail(ErrorCode.UNKNOWN_RESULT, "Stream was interrupted")

    with pytest.raises(DomainError):
        await uncertain.model_call(StepKind.MODEL_DECISION, Input, broken)
    await expire(service, uncertain)
    await service.recover_due()
    assert (await service.get(second.id)).state == TaskState.UNKNOWN
    with pytest.raises(DomainError) as raised:
        await service.action(
            second.id, TaskActionRequest(operation=TaskOperation.RESUME)
        )
    assert raised.value.code == ErrorCode.UNKNOWN_RESULT
    await service.action(
        second.id,
        TaskActionRequest(operation=TaskOperation.RESUME, acknowledge_unknown=True),
    )
    intentional = await claim(service, second.id)
    assert (
        await intentional.model_call(StepKind.MODEL_DECISION, Input, complete)
    ).value == 7


@pytest.mark.parametrize("code", [ErrorCode.WAITING_AUTH, ErrorCode.WAITING_QUOTA])
async def test_definitive_model_rejection_is_not_unknown(service, code):
    task = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "rejected"
    )
    context = await claim(service, task.id)

    async def rejected():
        raise fail(code, "Request was rejected before execution")

    with pytest.raises(DomainError) as raised:
        await context.model_call(StepKind.MODEL_DECISION, Input, rejected)
    await service.fail_task(context, raised.value)
    async with service.database.sessions() as session:
        invocation = await session.scalar(select(ModelInvocation))
        assert invocation.state == InvocationState.REJECTED
    resumed = await service.action(
        task.id, TaskActionRequest(operation=TaskOperation.RESUME)
    )
    assert resumed.state == TaskState.QUEUED


async def test_business_result_and_checkpoint_rollback_together(service):
    task = await service.submit(TaskKind.COLLECT_SOURCE, Input(value=1), "atomic")
    context = await claim(service, task.id)

    async def invalid_action(session):
        session.add(OutboxMessage(task_id=uuid4(), generation=0))
        return Input(value=1)

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await context.transaction_step(StepKind.SOURCE_COMMIT, Input, invalid_action)
    async with service.database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StepRun)) == 0
        assert await session.scalar(select(func.count()).select_from(Checkpoint)) == 0
        assert (await session.get(TaskRun, task.id)).checkpoint_seq == 0


async def test_heartbeat_failure_cancels_handler_before_later_action(service):
    task = await service.submit(TaskKind.FETCH_ARTICLE, Input(value=1), "heartbeat")
    registry = HandlerRegistry()
    cancelled = asyncio.Event()
    reached_model = False

    async def handler(context, payload):
        nonlocal reached_model
        try:
            await asyncio.sleep(20)
            reached_model = True
            return TaskOutcome(result=article_result())
        finally:
            cancelled.set()

    async def failed_renew(context):
        raise fail(ErrorCode.LEASE_LOST, "Lease was stolen")

    service.renew = failed_renew
    registry.register(TaskKind.FETCH_ARTICLE, Input, handler)
    await asyncio.wait_for(
        registry.execute(service, TaskEnvelope(task_id=task.id, generation=0)),
        timeout=8,
    )
    assert cancelled.is_set()
    assert reached_model is False


async def test_runtime_revision_and_submission_snapshot(service):
    first = await service.runtime_settings()
    changed = await service.update_runtime_settings(
        RuntimeSettingsUpdate(
            expected_revision=first.revision,
            settings=RuntimeSettingsValues(document_model_rounds=12),
        )
    )
    task = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "snapshot"
    )
    await service.update_runtime_settings(
        RuntimeSettingsUpdate(
            expected_revision=changed.revision,
            settings=RuntimeSettingsValues(document_model_rounds=3),
        )
    )
    context = await claim(service, task.id)
    assert context.runtime_settings.document_model_rounds == 12
    with pytest.raises(DomainError) as raised:
        await service.update_runtime_settings(
            RuntimeSettingsUpdate(expected_revision=1, settings=RuntimeSettingsValues())
        )
    assert raised.value.code == ErrorCode.CONFLICT


async def test_model_admission_is_shared_between_workers(service):
    first = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "capacity-one"
    )
    second = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=2), "capacity-two"
    )
    first_context = await claim(service, first.id)
    second_context = await claim(service, second.id)
    entered = asyncio.Event()
    release = asyncio.Event()
    second_calls = 0

    async def held():
        entered.set()
        await release.wait()
        return Input(value=1)

    async def later():
        nonlocal second_calls
        second_calls += 1
        return Input(value=2)

    pending = asyncio.create_task(
        first_context.model_call(StepKind.MODEL_DECISION, Input, held)
    )
    await entered.wait()
    try:
        with pytest.raises(DomainError) as raised:
            await second_context.model_call(StepKind.MODEL_DECISION, Input, later)
        assert raised.value.code == ErrorCode.BUSY
        assert second_calls == 0
        await service.fail_task(second_context, raised.value)
        assert (await service.get(second.id)).state == TaskState.RETRY_WAIT
        async with service.database.sessions() as session:
            assert (
                await session.scalar(select(func.count()).select_from(ModelInvocation))
                == 1
            )
    finally:
        release.set()
        await pending
    async with service.database.sessions() as session, session.begin():
        row = await session.get(TaskRun, second.id)
        row.retry_at = now() - timedelta(seconds=1)
    await service.recover_due()
    second_context = await claim(service, second.id)
    assert (
        await second_context.model_call(StepKind.MODEL_DECISION, Input, later)
    ).value == 2


async def test_new_global_model_limit_overrides_old_task_snapshot(service):
    await service.update_runtime_settings(
        RuntimeSettingsUpdate(
            expected_revision=1, settings=RuntimeSettingsValues(model_concurrency=4)
        )
    )
    first = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "old-limit-one"
    )
    second = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=2), "old-limit-two"
    )
    await service.update_runtime_settings(
        RuntimeSettingsUpdate(
            expected_revision=2, settings=RuntimeSettingsValues(model_concurrency=1)
        )
    )
    one = await claim(service, first.id)
    two = await claim(service, second.id)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()
        return Input(value=1)

    async def another():
        return Input(value=2)

    running = asyncio.create_task(one.model_call(StepKind.MODEL_DECISION, Input, hold))
    await entered.wait()
    try:
        with pytest.raises(DomainError) as raised:
            await two.model_call(StepKind.MODEL_DECISION, Input, another)
        assert raised.value.code == ErrorCode.BUSY
        assert two.runtime_settings.model_concurrency == 4
    finally:
        release.set()
        await running


async def test_transaction_step_rolls_back_when_lease_expires_during_database_work(
    service,
):
    view = await service.submit(
        TaskKind.COLLECT_SOURCE, Input(value=1), "commit-expiry"
    )
    context = await claim(service, view.id)
    async with service.database.sessions() as session, session.begin():
        task = await session.get(TaskRun, view.id)
        task.lease_until = now() + timedelta(seconds=0.15)

    async def slow_database_work(session):
        session.add(OutboxMessage(task_id=view.id, generation=context.generation))
        await asyncio.sleep(0.25)
        return Input(value=1)

    with pytest.raises(DomainError) as raised:
        await context.transaction_step(
            StepKind.SOURCE_COMMIT, Input, slow_database_work
        )
    assert raised.value.code == ErrorCode.LEASE_LOST
    async with service.database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(OutboxMessage)) == 1
        )
        assert await session.scalar(select(func.count()).select_from(StepRun)) == 0


async def test_expired_worker_cannot_write_failure_state(service):
    view = await service.submit(TaskKind.FETCH_ARTICLE, Input(value=1), "failure-fence")
    context = await claim(service, view.id)
    await expire(service, context)
    await service.fail_task(
        context, fail(ErrorCode.INVALID_INPUT, "Late worker failure")
    )
    assert (await service.get(view.id)).state == TaskState.RUNNING
    assert await service.recover_due() == 1


async def test_resolved_unknown_attempt_does_not_block_later_resume(service):
    view = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "resolved-unknown"
    )
    context = await claim(service, view.id)

    async def uncertain():
        raise fail(ErrorCode.UNKNOWN_RESULT, "Incomplete response")

    async def complete():
        return Input(value=2)

    with pytest.raises(DomainError) as raised:
        await context.model_call(StepKind.MODEL_DECISION, Input, uncertain)
    await service.fail_task(context, raised.value)
    await service.action(
        view.id,
        TaskActionRequest(operation=TaskOperation.RESUME, acknowledge_unknown=True),
    )
    resumed = await claim(service, view.id)
    await resumed.model_call(StepKind.MODEL_DECISION, Input, complete)
    await service.fail_task(
        resumed, fail(ErrorCode.WAITING_AUTH, "A later step needs authorization")
    )
    result = await service.action(
        view.id, TaskActionRequest(operation=TaskOperation.RESUME)
    )
    assert result.state == TaskState.QUEUED
    async with service.database.sessions() as session:
        old = await session.scalar(
            select(ModelInvocation).where(ModelInvocation.attempt == 1)
        )
        assert old.state == InvocationState.UNKNOWN


async def test_reprocess_uses_current_budgets_and_preserves_input_versions(service):
    original = await service.submit(
        TaskKind.RESOURCE_EXTRACTION,
        Input(value=1),
        "reprocess-config",
        workflow_version="parser-1",
        input_revision="document-hash-v1",
    )
    await service.update_runtime_settings(
        RuntimeSettingsUpdate(
            expected_revision=1,
            settings=RuntimeSettingsValues(document_model_rounds=40),
        )
    )
    revised = await service.action(
        original.id, TaskActionRequest(operation=TaskOperation.REPROCESS)
    )
    context = await claim(service, revised.id)
    assert revised.id != original.id
    assert revised.workflow_version == original.workflow_version
    assert revised.input_revision == original.input_revision
    assert context.runtime_settings.document_model_rounds == 40


async def test_definitive_http_rejection_is_recorded_without_unknown(service):
    view = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "http-rejection"
    )
    context = await claim(service, view.id)

    async def rejected():
        from mining_contracts.domain.core import ErrorDetails

        raise fail(
            ErrorCode.UPSTREAM_FAILURE,
            "HTTP 400 model not found",
            ErrorDetails(definitive_rejection=True),
        )

    with pytest.raises(DomainError) as raised:
        await context.model_call(StepKind.MODEL_DECISION, Input, rejected)
    await service.fail_task(context, raised.value)
    async with service.database.sessions() as session:
        assert (
            await session.scalar(select(ModelInvocation))
        ).state == InvocationState.REJECTED
    result = await service.action(
        view.id, TaskActionRequest(operation=TaskOperation.RETRY_FAILED_STEP)
    )
    assert result.state == TaskState.QUEUED


async def test_repeated_cancel_stays_requested_until_worker_stops(service):
    view = await service.submit(TaskKind.FETCH_ARTICLE, Input(value=1), "repeat-cancel")
    context = await claim(service, view.id)
    first = await service.action(
        view.id, TaskActionRequest(operation=TaskOperation.CANCEL)
    )
    second = await service.action(
        view.id, TaskActionRequest(operation=TaskOperation.CANCEL)
    )
    assert first.state == second.state == TaskState.CANCEL_REQUESTED
    assert first.generation == second.generation == context.generation

    async def action():
        return Input(value=1)

    with pytest.raises(DomainError) as raised:
        await context.step(StepKind.ARTICLE_FETCH, Input, action)
    assert raised.value.code == ErrorCode.CANCELLED
    await service.fail_task(context, raised.value)
    assert (await service.get(view.id)).state == TaskState.CANCELLED


async def test_nondefinitive_http_failure_exposes_unknown_task_state(service):
    view = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "http-uncertain"
    )
    context = await claim(service, view.id)

    async def failed():
        raise fail(ErrorCode.UPSTREAM_FAILURE, "HTTP 500 response")

    with pytest.raises(DomainError) as raised:
        await context.model_call(StepKind.MODEL_DECISION, Input, failed)
    await service.fail_task(context, raised.value)
    result = await service.get(view.id)
    assert result.state == TaskState.UNKNOWN
    assert result.error_code == ErrorCode.UNKNOWN_RESULT


async def test_stranded_confirmed_queue_delivery_is_recovered_without_duplicate_execution(
    service,
):
    view = await service.submit(TaskKind.FETCH_ARTICLE, Input(value=1), "stranded")
    async with service.database.sessions() as session, session.begin():
        original = await session.scalar(
            select(OutboxMessage).where(OutboxMessage.task_id == view.id)
        )
        original.published_at = now() - timedelta(
            seconds=service.settings.lease_seconds + 1
        )
    assert await service.recover_due() == 1
    assert await service.recover_due() == 0
    recovered = await service.get(view.id)
    assert recovered.generation == view.generation == 0
    async with service.database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(OutboxMessage)) == 2
        )
    registry = HandlerRegistry()
    calls = 0

    async def handler(context, raw):
        nonlocal calls
        calls += 1
        return TaskOutcome(result=article_result())

    registry.register(TaskKind.FETCH_ARTICLE, Input, handler)
    envelope = TaskEnvelope(task_id=view.id, generation=0)
    await asyncio.gather(
        registry.execute(service, envelope), registry.execute(service, envelope)
    )
    assert calls == 1
    assert (await service.get(view.id)).state == TaskState.SUCCEEDED


async def test_pending_and_recent_delivery_are_not_requeued(service):
    await service.submit(TaskKind.FETCH_ARTICLE, Input(value=1), "pending")
    recent = await service.submit(TaskKind.FETCH_ARTICLE, Input(value=2), "recent")
    async with service.database.sessions() as session, session.begin():
        message = await session.scalar(
            select(OutboxMessage).where(OutboxMessage.task_id == recent.id)
        )
        message.published_at = now()
    assert await service.recover_due() == 0
    async with service.database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(OutboxMessage)) == 2
        )


async def test_model_configuration_snapshot_is_immutable(service):
    await service.update_runtime_settings(
        RuntimeSettingsUpdate(
            expected_revision=1, settings=RuntimeSettingsValues(model_name="old-model")
        )
    )
    old = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "old-model"
    )
    await service.update_runtime_settings(
        RuntimeSettingsUpdate(
            expected_revision=2, settings=RuntimeSettingsValues(model_name="new-model")
        )
    )
    new = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=2), "new-model"
    )
    assert (await claim(service, old.id)).runtime_settings.model_name == "old-model"
    assert (await claim(service, new.id)).runtime_settings.model_name == "new-model"
    legacy = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=3), "legacy-model"
    )
    async with service.database.sessions() as session, session.begin():
        task = await session.get(TaskRun, legacy.id)
        import json

        snapshot = json.loads(task.settings_json)
        del snapshot["model_name"]
        task.settings_json = json.dumps(snapshot)
    assert (
        await claim(service, legacy.id)
    ).runtime_settings.model_name == "gpt-6.1-sol"


async def test_unknown_preserves_actionable_safe_message(service):
    task = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "unknown-message"
    )
    context = await claim(service, task.id)

    async def broken():
        raise fail(
            ErrorCode.UNKNOWN_RESULT,
            "Provider stream ended without a terminal response",
        )

    with pytest.raises(DomainError) as raised:
        await context.model_call(StepKind.MODEL_DECISION, Input, broken)
    await service.fail_task(context, raised.value)
    result = await service.get(task.id)
    assert result.state == TaskState.UNKNOWN
    assert result.error_message == "Provider stream ended without a terminal response"
    with pytest.raises(DomainError):
        await service.action(task.id, TaskActionRequest(operation=TaskOperation.RESUME))
