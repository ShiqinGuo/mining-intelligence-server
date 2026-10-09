from http import HTTPStatus
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, Response
from mining_contracts.domain.news import (
    Article,
    ArticleFetchRequest,
    NewsSearchRequest,
    NewsSearchResponse,
)
from mining_contracts.domain.task_views import TaskView

from mining_server.api.dependencies import (
    get_database,
    get_settings,
    get_task_service,
    require_service,
)
from mining_server.application.news import NewsService
from mining_server.application.tasks import TaskService
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.documents.http import PublicHttpClient
from mining_server.infrastructure.news.repository import NewsRepository
from mining_server.infrastructure.settings import Settings

router = APIRouter(
    prefix="/api/v1", tags=["news"], dependencies=[Depends(require_service)]
)


def get_news_repository_factory() -> type[NewsRepository]:
    return NewsRepository


def get_news_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    tasks: Annotated[TaskService, Depends(get_task_service)],
    repository_factory: Annotated[
        type[NewsRepository], Depends(get_news_repository_factory)
    ],
) -> NewsService:
    return NewsService(
        database,
        settings,
        tasks,
        PublicHttpClient(settings.request_timeout_seconds),
        repository_factory,
    )


@router.get("/news", response_model=NewsSearchResponse)
async def search_news(
    service: Annotated[NewsService, Depends(get_news_service)],
    query: Annotated[str, Query(min_length=1, max_length=500)],
    days: Annotated[int, Query(ge=1, le=3650)] = 7,
    project: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> NewsSearchResponse:
    return await service.search(
        NewsSearchRequest(query=query, days=days, project=project, limit=limit)
    )


@router.post("/articles/fetch", response_model=Article | TaskView)
async def fetch_article(
    request: ArticleFetchRequest,
    response: Response,
    service: Annotated[NewsService, Depends(get_news_service)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Article | TaskView:
    result = await service.fetch(
        str(request.url), idempotency_key or f"article:{uuid4()}"
    )
    match result:
        case TaskView():
            response.status_code = HTTPStatus.ACCEPTED
        case Article():
            response.status_code = HTTPStatus.OK
    return result
