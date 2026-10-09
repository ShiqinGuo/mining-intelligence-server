import secrets
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mining_contracts.domain.core import ErrorCode
from pydantic import BaseModel, ConfigDict

from mining_server.application.health import HealthService
from mining_server.application.tasks import TaskService
from mining_server.domain.core import fail
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.settings import Settings
from mining_server.infrastructure.task_repository import TaskRepository


class ApplicationState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", frozen=True)
    database: Database
    settings: Settings
    health_service: HealthService


security = HTTPBearer(auto_error=False)


def get_database(request: Request) -> Database:
    state: ApplicationState = request.app.state.runtime
    return state.database


def get_health_service(request: Request) -> HealthService:
    state: ApplicationState = request.app.state.runtime
    return state.health_service


def get_settings(request: Request) -> Settings:
    state: ApplicationState = request.app.state.runtime
    return state.settings


def get_task_repository_factory() -> type[TaskRepository]:
    return TaskRepository


def get_task_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    repository_factory: Annotated[
        type[TaskRepository], Depends(get_task_repository_factory)
    ],
) -> TaskService:
    return TaskService(database, settings, repository_factory)


async def require_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> None:
    if credentials is None or not secrets.compare_digest(
        credentials.credentials, settings.admin_token.get_secret_value()
    ):
        raise fail(ErrorCode.UNAUTHORIZED, "Administrator authentication required")


async def require_service(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> None:
    if credentials is None:
        raise fail(ErrorCode.UNAUTHORIZED, "Service authentication required")
    supplied = credentials.credentials
    if not secrets.compare_digest(
        supplied, settings.service_token.get_secret_value()
    ) and not secrets.compare_digest(supplied, settings.admin_token.get_secret_value()):
        raise fail(ErrorCode.UNAUTHORIZED, "Service authentication required")
