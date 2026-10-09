from http import HTTPStatus
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header

from mining_server.api.dependencies import require_admin, require_service
from mining_server.api.news import get_news_service
from mining_server.application.news import NewsService
from mining_server.domain.news import (
    CollectionRunList,
    Source,
    SourceCreate,
    SourceList,
    SourcePreview,
    SourceStateRequest,
    SourceUpdate,
)
from mining_server.domain.task_views import TaskView

router = APIRouter(
    prefix="/api/v1/sources", tags=["sources"], dependencies=[Depends(require_service)]
)


@router.get("", response_model=SourceList)
async def list_sources(
    service: Annotated[NewsService, Depends(get_news_service)],
) -> SourceList:
    return await service.list_sources()


@router.post(
    "",
    response_model=Source,
    status_code=HTTPStatus.CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_source(
    request: SourceCreate, service: Annotated[NewsService, Depends(get_news_service)]
) -> Source:
    return await service.create_source(request)


@router.get("/{source_id}", response_model=Source)
async def get_source(
    source_id: UUID, service: Annotated[NewsService, Depends(get_news_service)]
) -> Source:
    return await service.get_source(source_id)


@router.put(
    "/{source_id}", response_model=Source, dependencies=[Depends(require_admin)]
)
async def update_source(
    source_id: UUID,
    request: SourceUpdate,
    service: Annotated[NewsService, Depends(get_news_service)],
) -> Source:
    return await service.update_source(source_id, request)


@router.post(
    "/{source_id}/state", response_model=Source, dependencies=[Depends(require_admin)]
)
async def set_source_state(
    source_id: UUID,
    request: SourceStateRequest,
    service: Annotated[NewsService, Depends(get_news_service)],
) -> Source:
    return await service.set_source_state(source_id, request)


@router.post(
    "/{source_id}/preview",
    response_model=SourcePreview,
    dependencies=[Depends(require_admin)],
)
async def preview_source(
    source_id: UUID, service: Annotated[NewsService, Depends(get_news_service)]
) -> SourcePreview:
    return await service.preview(source_id)


@router.post(
    "/{source_id}/runs",
    response_model=TaskView,
    status_code=HTTPStatus.ACCEPTED,
    dependencies=[Depends(require_admin)],
)
async def run_source(
    source_id: UUID,
    service: Annotated[NewsService, Depends(get_news_service)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> TaskView:
    return await service.submit_collection(
        source_id, idempotency_key or f"source:{uuid4()}"
    )


@router.get("/{source_id}/runs", response_model=CollectionRunList)
async def source_runs(
    source_id: UUID, service: Annotated[NewsService, Depends(get_news_service)]
) -> CollectionRunList:
    return await service.collection_runs(source_id)
