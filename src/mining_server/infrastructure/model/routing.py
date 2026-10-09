import httpx

from mining_server.application.auth import AuthService
from mining_server.domain.core import ErrorCode, fail
from mining_server.domain.model import (
    ModelChannel,
    ModelTransportLimits,
    OpenAIProtocol,
)
from mining_server.domain.tasks import RuntimeSettings
from mining_server.infrastructure.auth.oauth import OAuthClient
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.model.openai_compatible import (
    ChatCompletionsModelGateway,
)
from mining_server.infrastructure.model.provider import (
    ModelGateway,
    ResponsesModelGateway,
)
from mining_server.infrastructure.settings import Settings


def create_model_gateway(
    client: httpx.AsyncClient,
    database: Database,
    settings: Settings,
    runtime: RuntimeSettings,
) -> ModelGateway:
    limits = ModelTransportLimits(
        read_timeout_seconds=settings.model_read_timeout_seconds,
        connect_timeout_seconds=settings.model_connect_timeout_seconds,
        write_timeout_seconds=settings.model_write_timeout_seconds,
        pool_timeout_seconds=settings.model_pool_timeout_seconds,
    )
    match runtime.model_channel:
        case ModelChannel.CHATGPT_SUBSCRIPTION:
            if settings.oauth_host_id is None:
                raise fail(
                    ErrorCode.WAITING_AUTH,
                    "ChatGPT subscription authorization host is required",
                )
            auth = AuthService(database, settings, OAuthClient(client))
            return ResponsesModelGateway(client, auth.access_token, limits)
        case ModelChannel.OPENAI_COMPATIBLE:
            if (
                runtime.openai_base_url != settings.openai_base_url
                or runtime.openai_protocol != settings.openai_protocol
            ):
                raise fail(
                    ErrorCode.CONFLICT,
                    "Task model endpoint snapshot differs from current credential configuration",
                )
            if settings.openai_api_key is None:
                raise fail(
                    ErrorCode.WAITING_AUTH, "Compatible model API key is required"
                )
            key = settings.openai_api_key.get_secret_value()
            match runtime.openai_protocol:
                case OpenAIProtocol.CHAT_COMPLETIONS:
                    return ChatCompletionsModelGateway(
                        client, runtime.openai_base_url, key, limits
                    )
                case OpenAIProtocol.RESPONSES:

                    async def token() -> str:
                        return key

                    return ResponsesModelGateway(
                        client,
                        token,
                        limits,
                        endpoint=runtime.openai_base_url.rstrip("/") + "/responses",
                    )
