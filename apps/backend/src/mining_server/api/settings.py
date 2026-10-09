from typing import Annotated

from fastapi import APIRouter, Depends

from mining_server.api.dependencies import get_task_service, require_admin
from mining_server.application.tasks import TaskService
from mining_server.domain.tasks import RuntimeSettings, RuntimeSettingsUpdate

router = APIRouter(
    prefix="/api/v1/settings", tags=["settings"], dependencies=[Depends(require_admin)]
)


@router.get("", response_model=RuntimeSettings)
async def get_runtime_settings(
    service: Annotated[TaskService, Depends(get_task_service)],
) -> RuntimeSettings:
    return await service.runtime_settings()


@router.put("", response_model=RuntimeSettings)
async def update_runtime_settings(
    body: RuntimeSettingsUpdate,
    service: Annotated[TaskService, Depends(get_task_service)],
) -> RuntimeSettings:
    return await service.update_runtime_settings(body)
