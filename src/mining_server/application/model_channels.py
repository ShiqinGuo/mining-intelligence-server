import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus

import httpx
from pydantic import ValidationError

from mining_server.application.auth import AuthService
from mining_server.application.tasks import TaskService
from mining_server.domain.auth import ConnectionAction, ConnectionStatus
from mining_server.domain.core import DomainError
from mining_server.domain.http import HttpHeader, HttpMethod
from mining_server.domain.model import (
    ModelCatalogItem,
    ModelChannel,
    ModelChannelCheck,
    ModelChannelList,
    ModelChannelReason,
    ModelChannelState,
    ModelChannelView,
    OpenAIModelsPayload,
    OpenAIProtocol,
)
from mining_server.infrastructure.auth.oauth import OAuthClient
from mining_server.infrastructure.auth.repository import AuthRepository
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.settings import Settings


class ModelChannelsService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        tasks: TaskService,
        repository_factory: type[AuthRepository] = AuthRepository,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self.database = database
        self.settings = settings
        self.tasks = tasks
        self.repositories = repository_factory
        self.clients = client_factory or (
            lambda: httpx.AsyncClient(
                timeout=min(
                    settings.request_timeout_seconds,
                    settings.model_catalog_timeout_seconds,
                ),
                follow_redirects=False,
                trust_env=False,
            )
        )

    async def status(self) -> ModelChannelList:
        current = await self.tasks.runtime_settings()
        key_configured = self.settings.openai_api_key is not None
        compatible_state = (
            ModelChannelState.CONFIGURED
            if key_configured
            else ModelChannelState.NOT_CONFIGURED
        )
        reason = None if key_configured else ModelChannelReason.MISSING_API_KEY
        if (
            key_configured
            and current.model_channel == ModelChannel.OPENAI_COMPATIBLE
            and (
                current.openai_base_url != self.settings.openai_base_url
                or current.openai_protocol != self.settings.openai_protocol
            )
        ):
            compatible_state = ModelChannelState.UNAVAILABLE
            reason = ModelChannelReason.CONFIGURATION_MISMATCH
        async with self.database.sessions() as session:
            row = await self.repositories(session).current()
            connected = (
                row is not None
                and row.status
                in (ConnectionStatus.CONNECTED, ConnectionStatus.REFRESHING)
                and row.encrypted_credentials is not None
            )
        host_configured = self.settings.oauth_host_id is not None
        subscription_state = (
            ModelChannelState.CONFIGURED
            if host_configured and connected
            else ModelChannelState.NOT_CONFIGURED
        )
        subscription_reason = (
            None
            if subscription_state == ModelChannelState.CONFIGURED
            else (
                ModelChannelReason.SUBSCRIPTION_NOT_CONNECTED
                if host_configured
                else ModelChannelReason.MISSING_OAUTH_HOST
            )
        )
        return ModelChannelList(
            selected_channel=current.model_channel,
            channels=[
                ModelChannelView(
                    channel=ModelChannel.OPENAI_COMPATIBLE,
                    state=compatible_state,
                    protocol=self.settings.openai_protocol,
                    base_url=self.settings.openai_base_url,
                    key_configured=key_configured,
                    reason=reason,
                ),
                ModelChannelView(
                    channel=ModelChannel.CHATGPT_SUBSCRIPTION,
                    state=subscription_state,
                    protocol=OpenAIProtocol.RESPONSES,
                    base_url="https://api.openai.com/v1",
                    key_configured=False,
                    oauth_host_configured=host_configured,
                    reason=subscription_reason,
                ),
            ],
        )

    async def check_for_recovery(self, channel: ModelChannel) -> ModelChannelCheck:
        return await self.check(channel, configured_endpoint_only=True)

    async def check(
        self, channel: ModelChannel, configured_endpoint_only: bool = False
    ) -> ModelChannelCheck:
        status = await self.status()
        view = next(item for item in status.channels if item.channel == channel)
        if view.state in (
            ModelChannelState.NOT_CONFIGURED,
            ModelChannelState.UNAVAILABLE,
        ) and not (
            configured_endpoint_only
            and view.reason == ModelChannelReason.CONFIGURATION_MISMATCH
        ):
            return ModelChannelCheck(
                channel=channel,
                state=view.state,
                checked_at=datetime.now(UTC),
                reason=view.reason,
            )
        try:
            async with asyncio.timeout(
                min(
                    self.settings.request_timeout_seconds,
                    self.settings.model_catalog_timeout_seconds,
                )
            ):
                async with self.clients() as client:
                    match channel:
                        case ModelChannel.OPENAI_COMPATIBLE:
                            async with client.stream(
                                HttpMethod.GET,
                                self.settings.openai_base_url.rstrip("/") + "/models",
                                headers=[
                                    (
                                        HttpHeader.AUTHORIZATION,
                                        "Bearer "
                                        + self.settings.openai_api_key.get_secret_value(),
                                    )
                                ],
                                follow_redirects=False,
                            ) as response:
                                if response.status_code != HTTPStatus.OK:
                                    return ModelChannelCheck(
                                        channel=channel,
                                        state=ModelChannelState.UNAVAILABLE,
                                        checked_at=datetime.now(UTC),
                                        reason=ModelChannelReason.UPSTREAM_HTTP_FAILURE,
                                        upstream_status=response.status_code,
                                    )
                                raw = bytearray()
                                async for chunk in response.aiter_bytes(
                                    chunk_size=self.settings.network_chunk_bytes
                                ):
                                    raw.extend(chunk)
                                    if len(raw) > self.settings.model_catalog_max_bytes:
                                        return ModelChannelCheck(
                                            channel=channel,
                                            state=ModelChannelState.UNAVAILABLE,
                                            checked_at=datetime.now(UTC),
                                            reason=ModelChannelReason.CATALOG_TOO_LARGE,
                                        )
                                catalog = OpenAIModelsPayload.model_validate_json(raw)
                                key = self.settings.openai_api_key.get_secret_value()
                                if any(
                                    key in item.id
                                    or any(character.isspace() for character in item.id)
                                    for item in catalog.data
                                ):
                                    return ModelChannelCheck(
                                        channel=channel,
                                        state=ModelChannelState.UNAVAILABLE,
                                        checked_at=datetime.now(UTC),
                                        reason=ModelChannelReason.INVALID_CATALOG,
                                    )
                                models = [
                                    ModelCatalogItem(slug=item.id, display_name=item.id)
                                    for item in catalog.data
                                ]
                        case ModelChannel.CHATGPT_SUBSCRIPTION:
                            async with self.database.sessions() as session:
                                row = await self.repositories(session).current()
                                if row is None or row.status not in (
                                    ConnectionStatus.CONNECTED,
                                    ConnectionStatus.REFRESHING,
                                ):
                                    return ModelChannelCheck(
                                        channel=channel,
                                        state=ModelChannelState.NOT_CONFIGURED,
                                        checked_at=datetime.now(UTC),
                                        reason=ModelChannelReason.SUBSCRIPTION_NOT_CONNECTED,
                                    )
                                revision = row.revision
                            auth = AuthService(
                                self.database,
                                self.settings,
                                OAuthClient(client),
                                self.repositories,
                            )
                            connection = await auth.check(
                                ConnectionAction(expected_revision=revision)
                            )
                            models = connection.models
            return ModelChannelCheck(
                channel=channel,
                state=ModelChannelState.READY,
                checked_at=datetime.now(UTC),
                models=models,
            )
        except ValidationError:
            return ModelChannelCheck(
                channel=channel,
                state=ModelChannelState.UNAVAILABLE,
                checked_at=datetime.now(UTC),
                reason=ModelChannelReason.INVALID_CATALOG,
            )
        except (httpx.HTTPError, TimeoutError):
            return ModelChannelCheck(
                channel=channel,
                state=ModelChannelState.UNAVAILABLE,
                checked_at=datetime.now(UTC),
                reason=ModelChannelReason.UPSTREAM_NETWORK_FAILURE,
            )
        except DomainError:
            return ModelChannelCheck(
                channel=channel,
                state=ModelChannelState.UNAVAILABLE,
                checked_at=datetime.now(UTC),
                reason=ModelChannelReason.SUBSCRIPTION_AUTH_FAILURE,
            )
