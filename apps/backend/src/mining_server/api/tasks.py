from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from mining_contracts.domain.task_views import TaskView
from mining_contracts.domain.tasks import TaskActionRequest

from mining_server.api.dependencies import (
    get_task_service,
    require_admin,
    require_service,
)
from mining_server.application.tasks import TaskService

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.get(
    "/{task_id}", response_model=TaskView, dependencies=[Depends(require_service)]
)
async def get_task(
    task_id: UUID, service: Annotated[TaskService, Depends(get_task_service)]
) -> TaskView:
    return await service.get(task_id)


@router.post(
    "/{task_id}/actions", response_model=TaskView, dependencies=[Depends(require_admin)]
)
async def task_action(
    task_id: UUID,
    body: TaskActionRequest,
    service: Annotated[TaskService, Depends(get_task_service)],
) -> TaskView:
    return await service.action(task_id, body)
