from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, UploadFile
from mining_contracts.domain.documents import (
    DocumentResponse,
    DocumentUrlRequest,
    ExtractionRequest,
    ExtractionResponse,
    ExtractionSubmission,
    ResourceExtractionResult,
)
from mining_contracts.domain.task_views import TaskView

from mining_server.api.dependencies import (
    get_database,
    get_settings,
    get_task_service,
    require_service,
)
from mining_server.application.documents import DocumentService
from mining_server.application.tasks import TaskService
from mining_server.domain.documents import DocumentTransferLimits
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.documents.repository import DocumentRepository
from mining_server.infrastructure.settings import Settings

router = APIRouter(
    prefix="/api/v1", dependencies=[Depends(require_service)], tags=["documents"]
)


def repository_factory() -> type[DocumentRepository]:
    return DocumentRepository


def service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    tasks: Annotated[TaskService, Depends(get_task_service)],
    repository: Annotated[type[DocumentRepository], Depends(repository_factory)],
) -> DocumentService:
    return DocumentService(database, settings, tasks, repository)


@router.post(
    "/documents/upload", response_model=DocumentResponse, status_code=HTTPStatus.CREATED
)
async def upload(
    document_service: Annotated[DocumentService, Depends(service)],
    file: Annotated[UploadFile, File()],
) -> DocumentResponse:
    async def chunks() -> AsyncIterator[bytes]:
        while chunk := await file.read(DocumentTransferLimits().upload_chunk_bytes):
            yield chunk

    try:
        return await document_service.upload(chunks())
    finally:
        await file.close()


@router.post(
    "/documents/from-url", response_model=TaskView, status_code=HTTPStatus.ACCEPTED
)
async def from_url(
    request: DocumentUrlRequest,
    document_service: Annotated[DocumentService, Depends(service)],
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
) -> TaskView:
    return await document_service.from_url(request, idempotency_key)


@router.get("/documents/{document_id}", response_model=DocumentResponse)
async def get(
    document_id: UUID, document_service: Annotated[DocumentService, Depends(service)]
) -> DocumentResponse:
    return await document_service.get(document_id)


@router.post(
    "/resource-extractions",
    response_model=ExtractionSubmission,
    status_code=HTTPStatus.ACCEPTED,
)
async def extract(
    request: ExtractionRequest,
    document_service: Annotated[DocumentService, Depends(service)],
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
) -> ExtractionSubmission:
    return await document_service.submit(request, idempotency_key)


@router.get("/resource-extractions/{extraction_id}", response_model=ExtractionResponse)
async def extraction(
    extraction_id: UUID, document_service: Annotated[DocumentService, Depends(service)]
) -> ExtractionResponse:
    return await document_service.extraction(extraction_id)


@router.get(
    "/resource-extractions/{extraction_id}/result",
    response_model=ResourceExtractionResult,
)
async def result(
    extraction_id: UUID, document_service: Annotated[DocumentService, Depends(service)]
) -> ResourceExtractionResult:
    return await document_service.result(extraction_id)
