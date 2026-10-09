import asyncio
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from test_tasks_recovery import Input, claim, service

from mining_server.application.model_channels import ModelChannelsService
from mining_server.application.tasks import now
from mining_server.domain.core import ErrorCode, ErrorDetails, fail
from mining_server.domain.model import (
    ModelCatalogItem,
    ModelChannel,
    ModelChannelCheck,
    ModelChannelState,
)
from mining_server.domain.task_lifecycle import TaskLifecycle
from mining_server.domain.tasks import (
    InvocationState,
    RuntimeSettings,
    StepKind,
    TaskActionRequest,
    TaskKind,
    TaskOperation,
    TaskState,
)
from mining_server.infrastructure.task_models import (
    ModelInvocation,
    OutboxMessage,
    TaskRetryBudget,
    TaskRun,
)

__all__ = ["service"]


async def waiting(service, key, channel=ModelChannel.CHATGPT_SUBSCRIPTION):
    task = await service.submit(TaskKind.RESOURCE_EXTRACTION, Input(value=1), key)
    context = await claim(service, task.id)
    await service.fail_task(
        context, fail(ErrorCode.WAITING_AUTH, "Authorization required")
    )
    async with service.database.sessions() as session, session.begin():
        row = await session.get(TaskRun, task.id)
        snapshot = RuntimeSettings.model_validate_json(row.settings_json)
        row.settings_json = snapshot.model_copy(
            update={"model_channel": channel}
        ).model_dump_json()
        row.retry_at = now() - timedelta(seconds=1)
    return task.id


async def ready(channel):
    return ModelChannelCheck(
        channel=channel,
        state=ModelChannelState.READY,
        checked_at=now(),
        models=[ModelCatalogItem(slug="gpt-6.1-sol", display_name="GPT 6.1")],
    )


async def test_auth_recovery_concurrent_atomic_dedup_and_checkpoint_reuse(service):
    task = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "checkpoint-auth"
    )
    context = await claim(service, task.id)
    calls = 0

    async def completed():
        nonlocal calls
        calls += 1
        return Input(value=9)

    await context.model_call(StepKind.MODEL_DECISION, Input, completed)

    async def stored():
        return Input(value=3)

    await context.step(StepKind.ARTICLE_FETCH, Input, stored)
    await service.fail_task(
        context, fail(ErrorCode.WAITING_AUTH, "Later step requires auth")
    )
    assert await service.recover_waiting_auth(ready) == 0
    results = await asyncio.gather(
        *[service.recover_waiting_auth(ready, immediate=True) for _ in range(5)]
    )
    assert sum(results) == 1
    resumed = await claim(service, task.id)
    assert (
        await resumed.model_call(StepKind.MODEL_DECISION, Input, completed)
    ).value == 9
    assert calls == 1
    assert (await resumed.step(StepKind.ARTICLE_FETCH, Input, stored)).value == 3
    async with service.database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(OutboxMessage)) == 2
        )
        row = await session.get(TaskRun, task.id)
        assert row.checkpoint_seq == 1
        assert row.error_code is None


async def test_invalid_auth_has_backoff_and_preserves_generation(service):
    task_id = await waiting(service, "invalid-auth")
    calls = 0

    async def invalid(channel):
        nonlocal calls
        calls += 1
        return ModelChannelCheck(
            channel=channel, state=ModelChannelState.UNAVAILABLE, checked_at=now()
        )

    before = await service.get(task_id)
    assert await service.recover_waiting_auth(invalid) == 0
    assert await service.recover_waiting_auth(invalid) == 0
    assert calls == 1
    after = await service.get(task_id)
    assert after.state == TaskState.WAITING_AUTH
    assert after.generation == before.generation
    async with service.database.sessions() as session:
        row = await session.get(TaskRun, task_id)
        assert row.retry_at > now()
        assert (
            await session.scalar(select(func.count()).select_from(OutboxMessage)) == 1
        )


async def test_auth_recovery_excludes_unknown_cancelled_and_other_channel(service):
    blocked = await waiting(service, "unknown-auth")
    cancelled = await waiting(service, "cancelled-auth")
    other = await waiting(service, "other-auth", ModelChannel.OPENAI_COMPATIBLE)
    valid = await waiting(service, "valid-auth")
    async with service.database.sessions() as session, session.begin():
        session.add(
            ModelInvocation(
                task_id=blocked,
                name=StepKind.MODEL_DECISION.storage_key(),
                attempt=1,
                state=InvocationState.UNKNOWN,
                generation=0,
            )
        )
        (await session.get(TaskRun, cancelled)).state = TaskState.CANCELLED
    assert (
        await service.recover_waiting_auth(
            ready, channel=ModelChannel.CHATGPT_SUBSCRIPTION, immediate=True
        )
        == 1
    )
    assert (await service.get(valid)).state == TaskState.QUEUED
    assert (await service.get(blocked)).state == TaskState.WAITING_AUTH
    assert (await service.get(cancelled)).state == TaskState.CANCELLED
    assert (await service.get(other)).state == TaskState.WAITING_AUTH


async def test_auth_recovery_channels_are_not_starved_by_full_failed_batch(service):
    service.defaults = service.defaults.model_copy(update={"recovery_batch_size": 1})
    one = await waiting(service, "unready-channel")
    two = await waiting(service, "ready-channel", ModelChannel.OPENAI_COMPATIBLE)
    calls = []

    async def check(channel):
        calls.append(channel)
        return ModelChannelCheck(
            channel=channel,
            state=ModelChannelState.READY
            if channel == ModelChannel.OPENAI_COMPATIBLE
            else ModelChannelState.UNAVAILABLE,
            checked_at=now(),
            models=[ModelCatalogItem(slug="gpt-6.1-sol", display_name="GPT 6.1")],
        )

    assert await service.recover_waiting_auth(check) == 1
    assert set(calls) == set(ModelChannel)
    assert (await service.get(one)).state == TaskState.WAITING_AUTH
    assert (await service.get(two)).state == TaskState.QUEUED


async def test_retryable_failure_budget_backoff_exhaustion_and_manual_reset(service):
    task = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "retryable-failure"
    )
    for count in range(service.defaults.failure_retry_limit + 1):
        context = await claim(service, task.id)
        await service.fail_task(
            context,
            fail(
                ErrorCode.UPSTREAM_FAILURE,
                "Known transient failure",
                ErrorDetails(retryable=True, definitive_rejection=True),
            ),
        )
        async with service.database.sessions() as session, session.begin():
            row = await session.get(TaskRun, task.id)
            budget = await session.get(TaskRetryBudget, task.id)
            assert budget.failure_retry_count == min(
                count + 1, service.defaults.failure_retry_limit
            )
            if count == service.defaults.failure_retry_limit:
                assert row.state == TaskState.FAILED
                assert row.retry_at is None
                break
            expected = (
                service.defaults.failure_retry_seconds
                * service.defaults.failure_retry_multiplier**count
            )
            assert expected - 2 < (row.retry_at - now()).total_seconds() <= expected
            assert row.state == TaskState.RETRY_WAIT
            row.retry_at = now() - timedelta(seconds=1)
        assert await service.recover_due() == 1
    assert await service.recover_due() == 0
    await service.action(task.id, TaskActionRequest(operation=TaskOperation.RESUME))
    async with service.database.sessions() as session:
        assert (await session.get(TaskRetryBudget, task.id)).failure_retry_count == 0


async def test_auth_quota_busy_do_not_consume_transient_failure_budget(service):
    for code in (ErrorCode.WAITING_AUTH, ErrorCode.WAITING_QUOTA, ErrorCode.BUSY):
        task = await service.submit(
            TaskKind.RESOURCE_EXTRACTION, Input(value=1), str(code)
        )
        context = await claim(service, task.id)
        await service.fail_task(
            context, fail(code, "Deferred execution", ErrorDetails(retryable=True))
        )
        async with service.database.sessions() as session:
            assert await session.get(TaskRetryBudget, task.id) is None


async def test_cancel_race_wins_over_retryable_failure_and_latest_unknown_blocks(
    service,
):
    task = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "cancel-race"
    )
    context = await claim(service, task.id)
    await service.action(task.id, TaskActionRequest(operation=TaskOperation.CANCEL))
    await service.fail_task(
        context,
        fail(
            ErrorCode.UPSTREAM_FAILURE, "Retryable error", ErrorDetails(retryable=True)
        ),
    )
    assert (await service.get(task.id)).state == TaskState.CANCELLED
    second = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "historic-unknown"
    )
    context = await claim(service, second.id)
    async with service.database.sessions() as session, session.begin():
        session.add(
            ModelInvocation(
                task_id=second.id,
                name=StepKind.MODEL_DECISION.storage_key(),
                attempt=1,
                state=InvocationState.UNKNOWN,
                generation=context.generation - 1,
            )
        )
    await service.fail_task(
        context,
        fail(
            ErrorCode.UPSTREAM_FAILURE, "Retryable error", ErrorDetails(retryable=True)
        ),
    )
    assert (await service.get(second.id)).state == TaskState.UNKNOWN
    assert await service.recover_due() == 0


async def test_actual_invalid_provider_does_not_wake_and_valid_probe_does(service):
    task_id = await waiting(service, "actual-provider", ModelChannel.OPENAI_COMPATIBLE)
    settings = service.settings.model_copy(
        update={"openai_api_key": SecretStr("test-provider-key")}
    )
    responses = [401, 200]
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == "Bearer test-provider-key"
        return httpx.Response(responses.pop(0), json={"data": [{"id": "gpt-6.1-sol"}]})

    channels = ModelChannelsService(
        service.database,
        settings,
        service,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    assert await service.recover_waiting_auth(channels.check_for_recovery) == 0
    assert (await service.get(task_id)).state == TaskState.WAITING_AUTH
    assert await service.recover_waiting_auth(channels.check_for_recovery) == 0
    assert calls == 1
    assert (
        await service.recover_waiting_auth(channels.check_for_recovery, immediate=True)
        == 1
    )
    assert calls == 2
    assert (await service.get(task_id)).state == TaskState.QUEUED


async def test_old_endpoint_snapshot_cannot_borrow_current_credentials(service):
    task_id = await waiting(service, "old-endpoint", ModelChannel.OPENAI_COMPATIBLE)
    async with service.database.sessions() as session, session.begin():
        row = await session.get(TaskRun, task_id)
        snapshot = RuntimeSettings.model_validate_json(row.settings_json)
        row.settings_json = snapshot.model_copy(
            update={"openai_base_url": "https://old.example/v1"}
        ).model_dump_json()
    calls = 0

    async def check(channel):
        nonlocal calls
        calls += 1
        return await ready(channel)

    assert await service.recover_waiting_auth(check, immediate=True) == 0
    assert calls == 0
    assert (await service.get(task_id)).state == TaskState.WAITING_AUTH


async def test_terminal_unknown_and_partial_are_never_automatically_replayed(service):
    protected = (
        TaskState.SUCCEEDED,
        TaskState.CANCELLED,
        TaskState.UNKNOWN,
        TaskState.PARTIAL,
        TaskState.FAILED,
    )
    for state in protected:
        task_id = await waiting(service, str(state))
        async with service.database.sessions() as session, session.begin():
            row = await session.get(TaskRun, task_id)
            row.state = state
    calls = 0

    async def check(channel):
        nonlocal calls
        calls += 1
        return await ready(channel)

    assert await service.recover_waiting_auth(check, immediate=True) == 0
    assert await service.recover_due() == 0
    assert calls == 0
    async with service.database.sessions() as session:
        assert await session.scalar(
            select(func.count()).select_from(OutboxMessage)
        ) == len(protected)


async def test_legacy_snapshot_without_channel_is_recovered_as_subscription(service):
    task_id = await waiting(service, "legacy-channel")
    async with service.database.sessions() as session, session.begin():
        row = await session.get(TaskRun, task_id)
        snapshot = RuntimeSettings.model_validate_json(row.settings_json)
        row.settings_json = snapshot.model_dump_json(exclude={"model_channel"})
    assert (
        await service.recover_waiting_auth(
            ready, channel=ModelChannel.CHATGPT_SUBSCRIPTION
        )
        == 1
    )
    assert (await service.get(task_id)).state == TaskState.QUEUED


@pytest.mark.parametrize(
    "models", [[], [ModelCatalogItem(slug="other-model", display_name="Other")]]
)
async def test_authenticated_catalog_without_snapshot_model_is_not_usable(
    service, models
):
    task_id = await waiting(service, "unavailable-model")

    async def check(channel):
        return ModelChannelCheck(
            channel=channel,
            state=ModelChannelState.READY,
            checked_at=now(),
            models=models,
        )

    assert await service.recover_waiting_auth(check) == 0
    assert (await service.get(task_id)).state == TaskState.WAITING_AUTH


@pytest.mark.parametrize(
    "state",
    [
        TaskState.SUCCEEDED,
        TaskState.CANCELLED,
        TaskState.UNKNOWN,
        TaskState.PARTIAL,
        TaskState.FAILED,
    ],
)
def test_lifecycle_keeps_protected_states_when_invocation_is_uncertain(state):
    assert TaskLifecycle.recover(state, uncertain=True) == state


@pytest.mark.parametrize("retry_after", [0, -1])
def test_explicit_invalid_retry_after_is_rejected_at_construction(retry_after):
    with pytest.raises(ValidationError):
        ErrorDetails(retry_after=retry_after)
