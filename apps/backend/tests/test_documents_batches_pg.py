import asyncio
import os
from uuid import uuid4

import pymupdf
import pytest
from cryptography.fernet import Fernet
from mining_contracts.domain.core import ErrorCode
from mining_contracts.domain.documents import (
    DocumentStatus,
    PageEvidence,
    ReportingStandard,
)
from mining_contracts.domain.tasks import (
    StepKey,
    StepKind,
    TaskActionRequest,
    TaskKind,
    TaskOperation,
)
from sqlalchemy import select

from mining_server.application.documents import DocumentService, DocumentWorkflow
from mining_server.application.tasks import TaskService
from mining_server.domain.core import DomainError, fail
from mining_server.domain.documents import (
    DocumentIngestPayload,
    ReadPagesArguments,
    SearchPagesArguments,
)
from mining_server.domain.pdf import ParsedPdfManifest
from mining_server.domain.tasks import TaskEnvelope
from mining_server.infrastructure.database import Base, Database
from mining_server.infrastructure.documents.repository import (
    DocumentRepository,
    RepositoryDocumentPages,
)
from mining_server.infrastructure.settings import Settings
from mining_server.infrastructure.task_models import StepRun


async def test_batches_resume_without_full_document_checkpoints(tmp_path):
    url = None
    if "MINING_DOCUMENTS_TEST_DATABASE_URL" in os.environ:
        url = os.environ["MINING_DOCUMENTS_TEST_DATABASE_URL"]
    if not url:
        pytest.skip("Dedicated PostgreSQL integration database is required")
    if not url.endswith("/mining_documents_test"):
        raise ValueError("Document integration tests require their dedicated database")
    database = Database(url)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        settings = Settings(
            database_url=url,
            admin_token="test-admin-token-123456",
            service_token="test-service-token-123456",
            master_key=Fernet.generate_key().decode(),
            data_dir=tmp_path,
        )
        tasks = TaskService(database, settings)
        service = DocumentService(database, settings, tasks)
        with pymupdf.open() as pdf:
            for index in range(47):
                page = pdf.new_page()
                text = "NI 43-101 TECHNICAL REPORT"
                if index == 36:
                    text += "\nTable 14 Resource indicated inferred 446Mt 1.28% Li2O"
                if index == 40:
                    text += "\nAn exact citation beyond the preview: 17.5% Li2O"
                page.insert_text((40, 40), text)
            data = pdf.tobytes()

        async def chunks():
            yield data

        document = await service.upload(chunks())
        duplicate = await service.upload(chunks())
        assert duplicate.id == document.id
        assert document.task_id is not None
        assert duplicate.task_id == document.task_id
        assert (await tasks.get(document.task_id)).kind == TaskKind.DOCUMENT_INGEST
        task = await tasks.submit(
            TaskKind.DOCUMENT_INGEST,
            DocumentIngestPayload(document_id=document.id),
            f"batch-test:{uuid4()}",
        )
        context = await tasks.claim(
            TaskEnvelope(task_id=task.id, generation=0), uuid4()
        )

        class InterruptingRepository(DocumentRepository):
            completed_first = False

            async def save_pages(self, identifier, parsed):
                if parsed.pages[0].page_number > 20 and not type(self).completed_first:
                    type(self).completed_first = True
                    raise fail(
                        ErrorCode.CANCELLED,
                        "Scripted interruption before second batch commit",
                    )
                await super().save_pages(identifier, parsed)

        interrupted_service = DocumentService(
            database, settings, tasks, InterruptingRepository
        )
        workflow = DocumentWorkflow(interrupted_service, None)
        with pytest.raises(DomainError) as error:
            await workflow.handle_ingest(
                context, DocumentIngestPayload.model_validate_json(context.payload_json)
            )
        await tasks.fail_task(context, error.value)
        async with database.sessions() as session:
            repository = DocumentRepository(session)
            assert await repository.count_pages(document.id) == 20
            assert (await repository.get(document.id)).status == DocumentStatus.STORED
            steps = list(
                (
                    await session.scalars(
                        select(StepRun).where(StepRun.task_id == task.id)
                    )
                ).all()
            )
            assert all(len(step.result_json) < 2000 for step in steps)
            manifest_step = next(
                step
                for step in steps
                if step.name
                == StepKey(kind=StepKind.PARSE_DOCUMENT, round=1).storage_key()
            )
            manifest = ParsedPdfManifest.model_validate_json(manifest_step.result_json)
            assert manifest.page_count == 47
        await asyncio.to_thread(manifest.manifest_path.unlink)
        await tasks.action(task.id, TaskActionRequest(operation=TaskOperation.RESUME))
        queued = await tasks.get(task.id)
        resumed_context = await tasks.claim(
            TaskEnvelope(task_id=task.id, generation=queued.generation), uuid4()
        )
        outcome = await DocumentWorkflow(service, None).handle_ingest(
            resumed_context,
            DocumentIngestPayload.model_validate_json(resumed_context.payload_json),
        )
        await tasks.finish(resumed_context, outcome)
        async with database.sessions() as session:
            assert await DocumentRepository(session).count_pages(document.id) == 47
            steps = list(
                (
                    await session.scalars(
                        select(StepRun).where(StepRun.task_id == task.id)
                    )
                ).all()
            )
            assert all(len(step.result_json) < 2000 for step in steps)
            assert (
                len(
                    [
                        step
                        for step in steps
                        if step.name.startswith(StepKind.SAVE_DOCUMENT_PAGES + ":")
                    ]
                )
                == 4
            )
        pages = RepositoryDocumentPages(database, document.id, 47)
        candidates = await pages.candidates()
        assert candidates.pages[0] == 37
        assert len(candidates.pages) == 1
        assert await pages.has_standard(ReportingStandard.NI_43_101)
        assert await pages.has_quote(PageEvidence(pdf_page=41, quote="17.5% Li2O"))
        read = await pages.read(ReadPagesArguments(pages=[37, 41]))
        assert len(read.pages) == 2
        search = await pages.search(SearchPagesArguments(query="17.5% Li2O", limit=1))
        assert search.pages[0].page_number == 41
        with pytest.raises(DomainError):
            await pages.read(ReadPagesArguments(pages=[48]))
    finally:
        await database.close()
