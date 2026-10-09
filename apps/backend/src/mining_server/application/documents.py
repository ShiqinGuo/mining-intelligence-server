import asyncio
import hashlib
from collections.abc import AsyncIterator
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from mining_contracts.domain.core import ErrorCode
from mining_contracts.domain.documents import (
    DocumentBudget,
    DocumentResponse,
    DocumentStatus,
    DocumentUrlRequest,
    EvidenceKind,
    ExtractionRequest,
    ExtractionResponse,
    ExtractionSubmission,
    ResourceExtractionResult,
)
from mining_contracts.domain.task_views import TaskView
from mining_contracts.domain.tasks import StepKey, StepKind, TaskKind, TaskState
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mining_server.application.tasks import HandlerRegistry, TaskContext, TaskService
from mining_server.domain.core import fail
from mining_server.domain.documents import (
    DocumentIngestPayload,
    ExtractionPayload,
    SavedPageBatch,
    StoredDocumentContent,
)
from mining_server.domain.pdf import ParsedPageBatch, ParsedPdfManifest
from mining_server.domain.tasks import TaskOutcome
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.documents.agent import DocumentAgent
from mining_server.infrastructure.documents.http import PublicHttpClient
from mining_server.infrastructure.documents.models import Document, ResourceExtraction
from mining_server.infrastructure.documents.parser import PdfParser
from mining_server.infrastructure.documents.repository import (
    DocumentRepository,
    RepositoryDocumentPages,
)
from mining_server.infrastructure.documents.storage import (
    MAX_PDF_BYTES,
    DocumentStorage,
)
from mining_server.infrastructure.model.provider import (
    ModelGateway,
)
from mining_server.infrastructure.model.routing import create_model_gateway
from mining_server.infrastructure.settings import Settings


class DocumentService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        tasks: TaskService,
        repository_factory: type[DocumentRepository] = DocumentRepository,
    ):
        self.database = database
        self.settings = settings
        self.tasks = tasks
        self.repositories = repository_factory
        self.storage = DocumentStorage(settings.data_dir)

    async def persist(
        self, session: AsyncSession, content: StoredDocumentContent
    ) -> DocumentResponse:
        await session.execute(
            insert(Document)
            .values(
                id=uuid4(),
                sha256=content.sha256,
                size_bytes=content.size_bytes,
                object_key=content.sha256 + ".pdf",
                source_url=content.source_url,
                status=DocumentStatus.STORED,
            )
            .on_conflict_do_nothing(index_elements=[Document.sha256])
        )
        row = await session.scalar(
            select(Document).where(Document.sha256 == content.sha256)
        )
        return DocumentRepository.view(row)

    async def upload(self, chunks: AsyncIterator[bytes]) -> DocumentResponse:
        content = await self.storage.store(chunks)
        async with self.database.sessions() as session, session.begin():
            document = await self.persist(session, content)
            task = await self.tasks.submit_in_session(
                session,
                TaskKind.DOCUMENT_INGEST,
                DocumentIngestPayload(document_id=document.id),
                "parse:" + content.sha256,
            )
            return DocumentResponse(
                id=document.id,
                task_id=task.id,
                sha256=document.sha256,
                size_bytes=document.size_bytes,
                page_count=document.page_count,
                status=document.status,
                source_url=document.source_url,
                created_at=document.created_at,
            )

    async def from_url(self, request: DocumentUrlRequest, key: str) -> TaskView:
        return await self.tasks.submit(
            TaskKind.DOCUMENT_INGEST,
            DocumentIngestPayload(pdf_url=request.pdf_url),
            key,
        )

    async def get(self, identifier: UUID) -> DocumentResponse:
        async with self.database.sessions() as session:
            return DocumentRepository.view(
                await self.repositories(session).get(identifier)
            )

    async def submit(
        self, request: ExtractionRequest, key: str
    ) -> ExtractionSubmission:
        identifier = uuid5(NAMESPACE_URL, "mining-server:extraction:" + key)
        payload = ExtractionPayload(extraction_id=identifier, request=request)
        async with self.database.sessions() as session, session.begin():
            if request.document_id:
                await self.repositories(session).get(request.document_id)
            task = await self.tasks.submit_in_session(
                session, TaskKind.RESOURCE_EXTRACTION, payload, key
            )
            await session.execute(
                insert(ResourceExtraction)
                .values(
                    id=identifier,
                    task_id=task.id,
                    document_id=request.document_id,
                    request_json=request.model_dump_json(),
                    request_hash=hashlib.sha256(
                        request.model_dump_json().encode()
                    ).hexdigest(),
                )
                .on_conflict_do_nothing()
            )
            row = await self.repositories(session).get_extraction(identifier)
            result = (
                ResourceExtractionResult.model_validate_json(row.result_json)
                if row.result_json
                else None
            )
            return ExtractionSubmission(
                extraction_id=identifier, task_id=task.id, result=result
            )

    async def extraction(self, identifier: UUID) -> ExtractionResponse:
        async with self.database.sessions() as session:
            row = await self.repositories(session).get_extraction(identifier)
        task = await self.tasks.get(row.task_id)
        return ExtractionResponse(
            id=row.id,
            task_id=row.task_id,
            document_id=row.document_id,
            state=task.state,
            result_ready=row.result_json is not None,
            error_message=task.error_message,
        )

    async def result(self, identifier: UUID) -> ResourceExtractionResult:
        async with self.database.sessions() as session:
            row = await self.repositories(session).get_extraction(identifier)
            if row.result_json is None:
                raise fail(
                    ErrorCode.CONFLICT, "Resource extraction result is not ready"
                )
            return ResourceExtractionResult.model_validate_json(row.result_json)


class DocumentWorkflow:
    def __init__(self, service: DocumentService, gateway: ModelGateway | None):
        self.service = service
        self.parser = PdfParser()
        self.gateway = gateway

    async def ingest(
        self, context: TaskContext, payload: DocumentIngestPayload
    ) -> DocumentResponse:
        if payload.document_id:
            document = await self.service.get(payload.document_id)
        else:

            async def download() -> StoredDocumentContent:
                return await PublicHttpClient(
                    self.service.settings.request_timeout_seconds
                ).download(str(payload.pdf_url), self.service.storage, MAX_PDF_BYTES)

            content = await context.step(
                StepKind.DOWNLOAD_DOCUMENT, StoredDocumentContent, download
            )

            async def persist(session: AsyncSession) -> DocumentResponse:
                return await self.service.persist(session, content)

            document = await context.transaction_step(
                StepKind.STORE_DOCUMENT, DocumentResponse, persist
            )
        if document.status == DocumentStatus.READY and document.page_count is not None:
            async with self.service.database.sessions() as session:
                count = await self.service.repositories(session).count_pages(
                    document.id
                )
            if count == document.page_count:
                await context.check_cancelled()
                return document
        parsed = await context.step(
            StepKey(kind=StepKind.PARSE_DOCUMENT, round=1),
            ParsedPdfManifest,
            lambda: self.parser.parse_to_file(
                self.service.storage.path(document.sha256)
            ),
        )
        if not await asyncio.to_thread(parsed.manifest_path.is_file):
            await context.check_cancelled()
            rebuilt = await self.parser.parse_to_file(
                self.service.storage.path(document.sha256)
            )
            if rebuilt != parsed:
                raise fail(
                    ErrorCode.CONFLICT,
                    "Rebuilt PDF manifest differs from its immutable checkpoint",
                )
        batch_index = 0
        async for batch in self.parser.iter_batches(parsed):
            batch_index += 1
            await context.check_cancelled()

            async def save_batch(
                session: AsyncSession, batch: ParsedPageBatch = batch
            ) -> SavedPageBatch:
                await self.service.repositories(session).save_pages(document.id, batch)
                return SavedPageBatch(
                    first_page=batch.pages[0].page_number,
                    last_page=batch.pages[-1].page_number,
                    page_count=len(batch.pages),
                )

            await context.transaction_step(
                StepKey(kind=StepKind.SAVE_DOCUMENT_PAGES, round=batch_index),
                SavedPageBatch,
                save_batch,
            )

        async def save(session: AsyncSession) -> DocumentResponse:
            repository = self.service.repositories(session)
            row = await repository.get(document.id)
            if await repository.count_pages(row.id) != parsed.page_count:
                raise fail(
                    ErrorCode.CONFLICT, "Persisted document page coverage is incomplete"
                )
            row.page_count = parsed.page_count
            row.status = DocumentStatus.READY
            await session.flush()
            return repository.view(row)

        return await context.transaction_step(
            StepKind.SAVE_DOCUMENT_PAGES, DocumentResponse, save
        )

    async def handle_ingest(
        self, context: TaskContext, payload: DocumentIngestPayload
    ) -> TaskOutcome[DocumentResponse]:
        document = await self.ingest(context, payload)
        return TaskOutcome(result=document)

    async def handle_extraction(
        self, context: TaskContext, payload: ExtractionPayload
    ) -> TaskOutcome[ResourceExtractionResult]:
        document = await self.ingest(
            context,
            DocumentIngestPayload(
                document_id=payload.request.document_id, pdf_url=payload.request.pdf_url
            ),
        )
        async with self.service.database.sessions() as session:
            original = await self.service.repositories(session).get_extraction(
                payload.extraction_id
            )
            extraction_id = (
                original.id if original.task_id == context.task_id else context.task_id
            )
        if document.page_count is None:
            raise fail(ErrorCode.CONFLICT, "Document page coverage is not ready")
        pages = RepositoryDocumentPages(
            self.service.database,
            document.id,
            document.page_count,
            self.service.repositories,
        )
        configured = context.runtime_settings
        requested = payload.request.budget
        budget = DocumentBudget(
            model_rounds=min(requested.model_rounds, configured.document_model_rounds),
            tool_calls=min(requested.tool_calls, configured.document_tool_calls),
            timeout_seconds=min(
                requested.timeout_seconds, configured.document_timeout_seconds
            ),
            visible_pages=min(
                requested.visible_pages, configured.document_visible_pages
            ),
            context_bytes=requested.context_bytes,
        )
        if self.gateway is None:
            raise fail(
                ErrorCode.WAITING_AUTH, "Resource extraction requires a model channel"
            )
        extraction = await DocumentAgent(
            self.gateway, self.parser, context.runtime_settings.model_name
        ).extract(
            context,
            pages,
            self.service.storage.path(document.sha256),
            payload.request.standard,
            budget,
        )
        limitations = extraction.limitations + [
            "Automated evidence checks verify page quotes and numeric, unit and material tokens; table-cell associations remain model interpretations."
        ]
        if any(
            evidence.kind == EvidenceKind.IMAGE
            for evidence in extraction.standard_evidence
            + [
                evidence
                for record in extraction.records
                for evidence in record.evidence
            ]
        ):
            limitations.append(
                "Image evidence is a model interpretation of rendered pages and requires visual review"
            )
        result = ResourceExtractionResult(
            extraction_id=extraction_id,
            document_id=document.id,
            document_sha256=document.sha256,
            reporting_standard=extraction.reporting_standard,
            standard_evidence=extraction.standard_evidence,
            records=extraction.records,
            complete=extraction.complete,
            limitations=limitations,
        )

        async def save(session: AsyncSession) -> ResourceExtractionResult:
            await session.execute(
                insert(ResourceExtraction)
                .values(
                    id=extraction_id,
                    task_id=context.task_id,
                    document_id=document.id,
                    request_json=payload.request.model_dump_json(),
                    request_hash=hashlib.sha256(
                        payload.request.model_dump_json().encode()
                    ).hexdigest(),
                )
                .on_conflict_do_nothing()
            )
            row = await self.service.repositories(session).get_extraction(extraction_id)
            row.document_id = document.id
            row.result_json = result.model_dump_json()
            return result

        stored = await context.transaction_step(
            StepKind.EXTRACT_RESOURCES, ResourceExtractionResult, save
        )
        return TaskOutcome(
            state=TaskState.SUCCEEDED if stored.complete else TaskState.PARTIAL,
            result=stored,
        )


def register_handlers(
    registry: HandlerRegistry, database: Database, settings: Settings
) -> None:
    async def execute(
        context: TaskContext, payload: DocumentIngestPayload | ExtractionPayload
    ) -> TaskOutcome[DocumentResponse] | TaskOutcome[ResourceExtractionResult]:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, trust_env=False
        ) as client:
            gateway = None
            if context.kind == TaskKind.RESOURCE_EXTRACTION:
                gateway = create_model_gateway(
                    client, database, settings, context.runtime_settings
                )
            workflow = DocumentWorkflow(
                DocumentService(database, settings, TaskService(database, settings)),
                gateway,
            )
            match payload:
                case DocumentIngestPayload():
                    return await workflow.handle_ingest(context, payload)
                case ExtractionPayload():
                    return await workflow.handle_extraction(context, payload)
                case _:
                    raise fail(ErrorCode.INVALID_INPUT, "Unsupported document workflow")

    registry.register(TaskKind.DOCUMENT_INGEST, DocumentIngestPayload, execute)
    registry.register(TaskKind.RESOURCE_EXTRACTION, ExtractionPayload, execute)
