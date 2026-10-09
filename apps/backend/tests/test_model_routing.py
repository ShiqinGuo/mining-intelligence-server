from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from mining_contracts.domain.core import ErrorCode

from mining_server.domain.core import DomainError
from mining_server.domain.model import (
    ModelChannel,
    ModelMessage,
    ModelRequest,
    ModelRole,
    OpenAIProtocol,
)
from mining_server.domain.tasks import (
    RuntimeSettings,
)
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.model.openai_compatible import (
    ChatCompletionsModelGateway,
)
from mining_server.infrastructure.model.provider import ResponsesModelGateway
from mining_server.infrastructure.model.routing import create_model_gateway
from mining_server.infrastructure.settings import Settings


def settings(
    openai_api_key: str | None = None,
    oauth_host_id: str | None = None,
    openai_protocol: OpenAIProtocol = OpenAIProtocol.CHAT_COMPLETIONS,
) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test",
        admin_token="test-admin-token-long-enough",
        service_token="test-service-token-long-enough",
        master_key=Fernet.generate_key().decode(),
        openai_base_url="https://vendor.example/v1",
        openai_api_key=openai_api_key,
        oauth_host_id=oauth_host_id,
        openai_protocol=openai_protocol,
    )


def runtime(
    openai_protocol: OpenAIProtocol = OpenAIProtocol.CHAT_COMPLETIONS,
    model_channel: ModelChannel = ModelChannel.OPENAI_COMPATIBLE,
    openai_base_url: str = "https://vendor.example/v1",
) -> RuntimeSettings:
    return RuntimeSettings(
        revision=1,
        model_channel=model_channel,
        openai_base_url=openai_base_url,
        openai_protocol=openai_protocol,
    )


async def test_routing_requires_api_key_and_rejects_changed_endpoint_snapshot():
    configuration = settings(oauth_host_id=f"urn:uuid:{uuid4()}")
    database = Database(configuration.database_url)
    async with httpx.AsyncClient() as client:
        with pytest.raises(DomainError) as missing:
            create_model_gateway(client, database, configuration, runtime())
        assert missing.value.code == ErrorCode.WAITING_AUTH
        with pytest.raises(DomainError) as host_missing:
            create_model_gateway(
                client,
                database,
                settings(oauth_host_id=None),
                runtime(model_channel=ModelChannel.CHATGPT_SUBSCRIPTION),
            )
        assert host_missing.value.code == ErrorCode.WAITING_AUTH
        for snapshot in [
            runtime(openai_base_url="https://changed.example/v1"),
            runtime(openai_protocol=OpenAIProtocol.RESPONSES),
        ]:
            with pytest.raises(DomainError) as changed:
                create_model_gateway(
                    client, database, settings(openai_api_key="test-key"), snapshot
                )
            assert changed.value.code == ErrorCode.CONFLICT
    await database.close()


async def test_chat_route_and_compatible_responses_route_use_explicit_protocol():
    configuration = settings(
        openai_api_key="test-key", openai_protocol=OpenAIProtocol.RESPONSES
    )
    database = Database(configuration.database_url)
    calls = []

    def endpoint(incoming):
        calls.append(incoming)
        return httpx.Response(
            200,
            text='data: {"type":"response.completed","response":{"id":"done","status":"completed","output":[]}}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        gateway = create_model_gateway(
            client,
            database,
            configuration,
            runtime(openai_protocol=OpenAIProtocol.RESPONSES),
        )
        assert isinstance(gateway, ResponsesModelGateway)
        await gateway.complete(
            ModelRequest(
                model="test-model",
                input=[ModelMessage(role=ModelRole.USER, content="hello")],
            )
        )
        assert calls[0].url == "https://vendor.example/v1/responses"
        assert calls[0].headers["Authorization"] == "Bearer test-key"
        chat = create_model_gateway(
            client, database, settings(openai_api_key="test-key"), runtime()
        )
        assert isinstance(chat, ChatCompletionsModelGateway)
    await database.close()


async def test_subscription_route_keeps_official_endpoint_without_api_key():
    configuration = settings(oauth_host_id=f"urn:uuid:{uuid4()}")
    database = Database(configuration.database_url)
    snapshot = runtime(model_channel=ModelChannel.CHATGPT_SUBSCRIPTION)
    async with httpx.AsyncClient() as client:
        gateway = create_model_gateway(client, database, configuration, snapshot)
        assert isinstance(gateway, ResponsesModelGateway)
        assert gateway.endpoint == "https://api.openai.com/v1/responses"
    await database.close()
