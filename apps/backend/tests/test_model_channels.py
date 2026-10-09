import httpx
import pytest
from apps.backend.tests.test_tasks_recovery import service
from mining_contracts.domain.core import ErrorCode
from pydantic import SecretStr, ValidationError

from mining_server.api.app import create_app
from mining_server.api.dependencies import ApplicationState
from mining_server.application.health import HealthService
from mining_server.application.model_channels import ModelChannelsService
from mining_server.application.tasks import TaskService
from mining_server.domain.auth import InstallationRequest
from mining_server.domain.core import DomainError
from mining_server.domain.model import (
    ModelChannel,
    ModelChannelReason,
    ModelChannelState,
)
from mining_server.domain.tasks import (
    RuntimeSettings,
    RuntimeSettingsUpdate,
    RuntimeSettingsValues,
)
from mining_server.infrastructure.settings import Settings

__all__ = ["service"]


def channel_settings(service: TaskService, key: str | None = None) -> Settings:
    return Settings(
        database_url=service.settings.database_url,
        admin_token=service.settings.admin_token,
        service_token=service.settings.service_token,
        master_key=service.settings.master_key,
        oauth_host_id=None,
        openai_api_key=SecretStr(key) if key is not None else None,
        openai_base_url=service.settings.openai_base_url,
        openai_protocol=service.settings.openai_protocol,
        model_name=service.settings.model_name,
        model_channel=service.settings.model_channel,
    )


def runtime_values(
    current: RuntimeSettings,
    model_name: str | None = None,
    model_channel: ModelChannel | None = None,
    base_url: str | None = None,
) -> RuntimeSettingsValues:
    return RuntimeSettingsValues(
        model_name=model_name if model_name is not None else current.model_name,
        model_channel=model_channel
        if model_channel is not None
        else current.model_channel,
        openai_base_url=base_url if base_url is not None else current.openai_base_url,
        openai_protocol=current.openai_protocol,
        source_interval_seconds=current.source_interval_seconds,
        model_concurrency=current.model_concurrency,
        document_model_rounds=current.document_model_rounds,
        document_tool_calls=current.document_tool_calls,
        document_timeout_seconds=current.document_timeout_seconds,
        document_visible_pages=current.document_visible_pages,
    )


async def test_catalog_probe_and_missing_subscription(service):
    settings = channel_settings(service, "fake-test-key")
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        assert request.headers["Authorization"] == "Bearer fake-test-key"
        return httpx.Response(
            200, json={"data": [{"id": "test-model", "owned_by": "test"}]}
        )

    channels = ModelChannelsService(
        service.database,
        settings,
        TaskService(service.database, settings),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    status = await channels.status()
    assert status.selected_channel == ModelChannel.OPENAI_COMPATIBLE
    assert "fake-test-key" not in status.model_dump_json()
    result = await channels.check(ModelChannel.OPENAI_COMPATIBLE)
    assert result.state == ModelChannelState.READY
    assert result.models[0].slug == "test-model"
    missing = await channels.check(ModelChannel.CHATGPT_SUBSCRIPTION)
    assert missing.reason == ModelChannelReason.MISSING_OAUTH_HOST
    assert len(calls) == 1


async def test_missing_key_is_not_masked_by_runtime_endpoint_mismatch(service):
    settings = channel_settings(service).model_copy(
        update={"openai_base_url": "https://other.example/v1"}
    )
    current = await service.runtime_settings()
    await service.update_runtime_settings(
        RuntimeSettingsUpdate(
            expected_revision=current.revision,
            settings=runtime_values(
                current,
                model_channel=ModelChannel.OPENAI_COMPATIBLE,
            ),
        )
    )

    def unexpected_client():
        raise AssertionError("Missing credentials must not trigger an upstream request")

    channels = ModelChannelsService(
        service.database, settings, service, client_factory=unexpected_client
    )
    status = await channels.status()
    compatible = next(
        item
        for item in status.channels
        if item.channel == ModelChannel.OPENAI_COMPATIBLE
    )
    assert compatible.state == ModelChannelState.NOT_CONFIGURED
    assert compatible.reason == ModelChannelReason.MISSING_API_KEY
    result = await channels.check_for_recovery(ModelChannel.OPENAI_COMPATIBLE)
    assert result.state == ModelChannelState.NOT_CONFIGURED
    assert result.reason == ModelChannelReason.MISSING_API_KEY


@pytest.mark.parametrize(
    "status,body,reason",
    [
        (401, b"secret", ModelChannelReason.UPSTREAM_HTTP_FAILURE),
        (302, b"", ModelChannelReason.UPSTREAM_HTTP_FAILURE),
        (200, b"bad", ModelChannelReason.INVALID_CATALOG),
        (200, b"x" * 1048577, ModelChannelReason.CATALOG_TOO_LARGE),
        (200, b'{"data":[{"id":"fake-test-key"}]}', ModelChannelReason.INVALID_CATALOG),
    ],
    ids=["unauthorized", "redirect", "invalid-json", "oversize", "secret-echo"],
)
async def test_catalog_failures_are_bounded_and_redacted(service, status, body, reason):
    settings = channel_settings(service, "fake-test-key")
    channels = ModelChannelsService(
        service.database,
        settings,
        TaskService(service.database, settings),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, content=body)
            )
        ),
    )
    result = await channels.check(ModelChannel.OPENAI_COMPATIBLE)
    assert result.reason == reason
    assert "fake-test-key" not in result.model_dump_json()


async def test_legacy_defaults_and_configuration_fail_fast(service):
    assert (
        RuntimeSettings.model_validate({}).model_channel
        == ModelChannel.CHATGPT_SUBSCRIPTION
    )
    current = await service.runtime_settings()
    changed = runtime_values(current, base_url="https://other.example/v1")
    with pytest.raises(DomainError) as error:
        await service.update_runtime_settings(
            RuntimeSettingsUpdate(expected_revision=current.revision, settings=changed)
        )
    assert error.value.code == ErrorCode.INVALID_INPUT
    valid = service.settings.model_dump()
    for field, value in [
        ("openai_api_key", ""),
        ("openai_api_key", "  "),
        ("openai_base_url", "https://user:pass@example.com/v1"),
        ("openai_base_url", "https://example.com/v1?key=x"),
        ("openai_protocol", "invalid"),
    ]:
        with pytest.raises(ValidationError):
            Settings(**(valid | {field: value}))


async def test_missing_configuration_is_queryable_and_auth_install_waits(service):
    settings = channel_settings(service)
    app = create_app(settings, start_publisher=False)
    app.state.runtime = ApplicationState(
        settings=settings,
        database=service.database,
        health_service=HealthService(service.database, settings, None, lambda: False),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        readiness = await client.get("/health/ready")
        assert readiness.status_code == 503
        assert readiness.json()["ready"] is False
        assert (await client.get("/api/v1/model-channels")).status_code == 401
        headers = {"Authorization": "Bearer " + settings.admin_token.get_secret_value()}
        response = await client.get("/api/v1/model-channels", headers=headers)
        assert response.status_code == 200
        assert all(
            item["state"] == "not_configured" for item in response.json()["channels"]
        )
        response = await client.get("/api/v1/model-connection", headers=headers)
        assert response.status_code == 200
        assert response.json()["host_id"] is None
        response = await client.post(
            "/api/v1/model-connection/install",
            headers=headers,
            json=InstallationRequest(expected_revision=0).model_dump(mode="json"),
        )
        assert response.status_code == 401
        assert response.json()["code"] == ErrorCode.WAITING_AUTH.value


async def test_task_channel_snapshot_is_fixed_and_has_no_secret(service):
    from apps.backend.tests.test_tasks_recovery import Input, claim
    from mining_contracts.domain.tasks import TaskKind

    from mining_server.infrastructure.task_models import TaskRun

    settings = channel_settings(service, "fake-snapshot-key")
    tasks = TaskService(service.database, settings)
    task = await tasks.submit(
        TaskKind.FETCH_ARTICLE, Input(value=1), "channel-snapshot"
    )
    current = await tasks.runtime_settings()
    values = runtime_values(
        current,
        model_channel=ModelChannel.CHATGPT_SUBSCRIPTION,
        model_name="changed-model",
    )
    await tasks.update_runtime_settings(
        RuntimeSettingsUpdate(expected_revision=current.revision, settings=values)
    )
    context = await claim(tasks, task.id)
    assert context.runtime_settings.model_channel == ModelChannel.OPENAI_COMPATIBLE
    assert context.runtime_settings.model_name == settings.model_name
    async with service.database.sessions() as session:
        row = await session.get(TaskRun, task.id)
        assert "fake-snapshot-key" not in row.settings_json
        assert "fake-snapshot-key" not in row.payload_json
