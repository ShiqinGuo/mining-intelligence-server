from typing import Annotated

from fastapi import APIRouter, Depends

from mining_server.api.dependencies import (
    get_database,
    get_settings,
    get_task_service,
    require_admin,
)
from mining_server.application.model_channels import ModelChannelsService
from mining_server.application.tasks import TaskService
from mining_server.domain.model import (
    ModelChannel,
    ModelChannelCheck,
    ModelChannelList,
    ModelChannelState,
)
from mining_server.infrastructure.auth.repository import AuthRepository
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.settings import Settings

router = APIRouter(
    prefix="/api/v1/model-channels",
    tags=["model-channels"],
    dependencies=[Depends(require_admin)],
)


def get_model_channel_repository_factory() -> type[AuthRepository]:
    return AuthRepository


def get_model_channels_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    tasks: Annotated[TaskService, Depends(get_task_service)],
    repository: Annotated[
        type[AuthRepository], Depends(get_model_channel_repository_factory)
    ],
) -> ModelChannelsService:
    return ModelChannelsService(database, settings, tasks, repository)


@router.get("", response_model=ModelChannelList)
async def list_channels(
    service: Annotated[ModelChannelsService, Depends(get_model_channels_service)],
) -> ModelChannelList:
    return await service.status()


@router.post("/{channel}/check", response_model=ModelChannelCheck)
async def check_channel(
    channel: ModelChannel,
    service: Annotated[ModelChannelsService, Depends(get_model_channels_service)],
) -> ModelChannelCheck:
    result = await service.check(channel)
    if result.state == ModelChannelState.READY:

        async def checked(selected: ModelChannel) -> ModelChannelCheck:
            return result

        await service.tasks.recover_waiting_auth(
            checked, channel=channel, immediate=True
        )
    return result
