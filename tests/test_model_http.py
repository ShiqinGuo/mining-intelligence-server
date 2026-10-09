from http import HTTPStatus
from typing import TypedDict
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from mining_server.domain.auth import CatalogResponse, Credentials, OAuthScope, utcnow
from mining_server.domain.core import DomainError, ErrorCode
from mining_server.domain.http import HttpMethod
from mining_server.domain.model import (
    ModelChannel,
    ModelMessage,
    ModelRequest,
    ModelRole,
    ModelTransportFailure,
    OpenAIProtocol,
)
from mining_server.domain.tasks import RuntimeSettings
from mining_server.infrastructure.auth.oauth import OAuthClient
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.model.provider import ResponsesModelGateway
from mining_server.infrastructure.model.routing import create_model_gateway
from mining_server.infrastructure.settings import Settings


class TimeoutExtension(TypedDict):
    connect: float
    read: float
    write: float
    pool: float


@pytest.mark.parametrize(
    "protocol", [OpenAIProtocol.CHAT_COMPLETIONS, OpenAIProtocol.RESPONSES]
)
async def test_routing_model_timeout_overrides_post_without_changing_source_and_oauth(
    protocol,
):
    settings = Settings(
        _env_file=None,
        admin_token="test-admin-token-long-enough",
        service_token="test-service-token-long-enough",
        master_key=Fernet.generate_key().decode(),
        openai_api_key="test-key",
        openai_protocol=protocol,
        model_read_timeout_seconds=180,
        model_connect_timeout_seconds=12,
        model_write_timeout_seconds=13,
        model_pool_timeout_seconds=14,
    )
    runtime = RuntimeSettings(
        revision=1,
        model_channel=ModelChannel.OPENAI_COMPATIBLE,
        openai_protocol=protocol,
        openai_base_url=settings.openai_base_url,
    )
    database = Database(settings.database_url)
    calls = []

    def endpoint(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming.method)
        timeout: TimeoutExtension = incoming.extensions["timeout"]
        if incoming.method == HttpMethod.POST:
            assert timeout["read"] == settings.model_read_timeout_seconds
            assert timeout["connect"] == settings.model_connect_timeout_seconds
            assert timeout["write"] == settings.model_write_timeout_seconds
            assert timeout["pool"] == settings.model_pool_timeout_seconds
            match protocol:
                case OpenAIProtocol.RESPONSES:
                    return httpx.Response(
                        HTTPStatus.OK,
                        text='data: {"type":"response.completed","response":{"id":"done","status":"completed","output":[]}}\n\n',
                    )
                case OpenAIProtocol.CHAT_COMPLETIONS:
                    return httpx.Response(
                        HTTPStatus.OK,
                        content='{"id":"done","choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"done"}}]}',
                    )
        assert timeout["read"] == settings.request_timeout_seconds
        return httpx.Response(
            HTTPStatus.OK, content=CatalogResponse(models=[]).model_dump_json()
        )

    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        transport=httpx.MockTransport(endpoint),
    ) as client:
        gateway = create_model_gateway(client, database, settings, runtime)
        await gateway.complete(
            ModelRequest(
                model="test-model",
                input=[ModelMessage(role=ModelRole.USER, content="test")],
            )
        )
        await client.get("https://example.com/source")
        credentials = Credentials(
            subject="test",
            client_id="issued",
            access_token="test-access",
            refresh_token="test-refresh",
            id_token="test-id",
            scopes=[
                OAuthScope.OPENID,
                OAuthScope.OFFLINE_ACCESS,
                OAuthScope.RESOURCE_INVOKE,
                OAuthScope.CHATGPT_DIRECT,
            ],
            expires_at=utcnow(),
        )
        await OAuthClient(client).catalog(credentials)
    assert calls == [HttpMethod.POST, HttpMethod.GET, HttpMethod.GET]
    await database.close()


async def test_subscription_factory_uses_same_model_timeout_configuration():
    settings = Settings(
        _env_file=None,
        admin_token="test-admin-token-long-enough",
        service_token="test-service-token-long-enough",
        master_key=Fernet.generate_key().decode(),
        oauth_host_id=f"urn:uuid:{uuid4()}",
        model_read_timeout_seconds=240,
    )
    runtime = RuntimeSettings(
        revision=1, model_channel=ModelChannel.CHATGPT_SUBSCRIPTION
    )
    database = Database(settings.database_url)
    async with httpx.AsyncClient() as client:
        gateway = create_model_gateway(client, database, settings, runtime)
        assert isinstance(gateway, ResponsesModelGateway)
        assert (
            gateway.limits.read_timeout_seconds == settings.model_read_timeout_seconds
        )
    await database.close()


@pytest.mark.parametrize(
    "exception, expected",
    [
        (httpx.ReadTimeout, ModelTransportFailure.READ_TIMEOUT),
        (httpx.ReadError, ModelTransportFailure.READ_ERROR),
        (httpx.ConnectTimeout, ModelTransportFailure.CONNECT_TIMEOUT),
        (httpx.RemoteProtocolError, ModelTransportFailure.REMOTE_PROTOCOL_ERROR),
    ],
)
async def test_transport_failure_classification_is_safe_and_never_retried(
    exception, expected
):
    calls = []

    def interrupted(incoming: httpx.Request):
        calls.append(incoming)
        raise exception("private-transport-sentinel", request=incoming)

    async def token() -> str:
        return "test-token"

    async with httpx.AsyncClient(transport=httpx.MockTransport(interrupted)) as client:
        with pytest.raises(DomainError) as error:
            await ResponsesModelGateway(client, token).complete(
                ModelRequest(
                    model="test",
                    input=[ModelMessage(role=ModelRole.USER, content="test")],
                )
            )
    assert error.value.code == ErrorCode.UNKNOWN_RESULT
    assert expected in error.value.message
    assert "private-transport-sentinel" not in error.value.message
    assert len(calls) == 1


def test_invalid_model_read_timeout_is_rejected_at_configuration_boundary():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            admin_token="test-admin-token-long-enough",
            service_token="test-service-token-long-enough",
            master_key=Fernet.generate_key().decode(),
            model_read_timeout_seconds=0,
        )
