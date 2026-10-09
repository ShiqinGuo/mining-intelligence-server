from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends

from mining_server.api.dependencies import (
    get_database,
    get_settings,
    get_task_service,
    require_admin,
)
from mining_server.application.auth import AuthService
from mining_server.application.tasks import TaskService
from mining_server.domain.auth import (
    ConnectionAction,
    ConnectionView,
    InstallationRequest,
    InstallationView,
    utcnow,
)
from mining_server.domain.model import (
    ModelChannel,
    ModelChannelCheck,
    ModelChannelState,
)
from mining_server.infrastructure.auth.oauth import OAuthClient
from mining_server.infrastructure.auth.repository import AuthRepository
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.settings import Settings

router = APIRouter(
    prefix="/api/v1/model-connection",
    dependencies=[Depends(require_admin)],
    tags=["model connection"],
)


def repository_factory() -> type[AuthRepository]:
    return AuthRepository


async def service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    repository: Annotated[type[AuthRepository], Depends(repository_factory)],
) -> AsyncIterator[AuthService]:
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds, trust_env=False
    ) as client:
        yield AuthService(database, settings, OAuthClient(client), repository)


@router.get("", response_model=ConnectionView)
async def status(auth: Annotated[AuthService, Depends(service)]) -> ConnectionView:
    return await auth.status()


@router.post("/install", response_model=InstallationView)
async def install(
    request: InstallationRequest, auth: Annotated[AuthService, Depends(service)]
) -> InstallationView:
    return await auth.install(request)


@router.post("/check", response_model=ConnectionView)
async def check(
    request: ConnectionAction,
    auth: Annotated[AuthService, Depends(service)],
    tasks: Annotated[TaskService, Depends(get_task_service)],
) -> ConnectionView:
    connection = await auth.check(request)

    async def checked(channel: ModelChannel) -> ModelChannelCheck:
        return ModelChannelCheck(
            channel=channel,
            state=ModelChannelState.READY,
            checked_at=utcnow(),
            models=connection.models,
        )

    await tasks.recover_waiting_auth(
        checked, channel=ModelChannel.CHATGPT_SUBSCRIPTION, immediate=True
    )
    return connection


@router.post("/disconnect", response_model=ConnectionView)
async def disconnect(
    request: ConnectionAction, auth: Annotated[AuthService, Depends(service)]
) -> ConnectionView:
    return await auth.disconnect(request)
