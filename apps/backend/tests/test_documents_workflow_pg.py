import os
from uuid import uuid4

import pymupdf
import pytest
from cryptography.fernet import Fernet
from mining_contracts.domain.core import ErrorCode
from mining_contracts.domain.documents import (
    DocumentBudget,
    ExtractionRequest,
    ReportingStandard,
)
from mining_contracts.domain.tasks import TaskActionRequest, TaskOperation, TaskState

from mining_server.application.documents import DocumentService, DocumentWorkflow
from mining_server.application.tasks import TaskService
from mining_server.domain.core import DomainError, fail
from mining_server.domain.documents import ExtractionPayload
from mining_server.domain.model import ModelFunctionCall, ModelResponse
from mining_server.domain.tasks import TaskEnvelope
from mining_server.infrastructure.database import Base, Database
from mining_server.infrastructure.documents.agent import DocumentAgent
from mining_server.infrastructure.settings import Settings


class ScriptedDocumentModel:
    def __init__(self):
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            name = "read_pages"
            arguments = '{"pages":[1]}'
        else:
            name = "finish_extraction"
            arguments = '{"reporting_standard":"jorc","records":[{"project":"Test Mine","reporting_standard":"jorc","category":"indicated","scope":"in_situ","tonnage":{"value":"100","unit":"Mt","material":"ore"},"grade":[{"value":"1.2","unit":"%","material":"Li2O"}],"evidence":[{"pdf_page":1,"quote":"JORC In-situ Indicated 100 Mt 1.2% Li2O"}]}],"complete":true,"limitations":[]}'
        call = ModelFunctionCall(
            call_id=f"call_{self.calls}", name=name, arguments=arguments
        )
        return ModelResponse(
            response_id=f"resp_{self.calls}", output=[call], tool_calls=[call]
        )


async def test_document_workflow_reuses_model_and_tool_checkpoints(
    tmp_path, monkeypatch
):
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
            oauth_host_id=f"urn:uuid:{uuid4()}",
            data_dir=tmp_path,
        )
        tasks = TaskService(database, settings)
        service = DocumentService(database, settings, tasks)
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((40, 40), "JORC In-situ Indicated 100 Mt 1.2% Li2O")
            data = pdf.tobytes()

        async def chunks():
            yield data

        document = await service.upload(chunks())
        submission = await service.submit(
            ExtractionRequest(document_id=document.id, standard=ReportingStandard.JORC),
            f"test-extraction:{uuid4()}",
        )
        context = await tasks.claim(
            TaskEnvelope(task_id=submission.task_id, generation=0), uuid4()
        )
        model = ScriptedDocumentModel()
        workflow = DocumentWorkflow(service, model)
        first = await workflow.handle_extraction(
            context, ExtractionPayload.model_validate_json(context.payload_json)
        )
        second = await workflow.handle_extraction(
            context, ExtractionPayload.model_validate_json(context.payload_json)
        )
        assert first == second
        assert model.calls == 2
        result = await service.result(submission.extraction_id)
        assert result.complete
        assert result.records[0].tonnage.value == 100
        await tasks.finish(context, second)
        revised = await tasks.action(
            submission.task_id, TaskActionRequest(operation=TaskOperation.REPROCESS)
        )
        new_context = await tasks.claim(
            TaskEnvelope(task_id=revised.id, generation=0), uuid4()
        )
        new_workflow = DocumentWorkflow(service, ScriptedDocumentModel())
        new_outcome = await new_workflow.handle_extraction(
            new_context, ExtractionPayload.model_validate_json(new_context.payload_json)
        )
        await tasks.finish(new_context, new_outcome)
        assert (
            await service.result(submission.extraction_id)
        ).extraction_id == submission.extraction_id
        assert (await service.result(revised.id)).extraction_id == revised.id
        limited = await service.submit(
            ExtractionRequest(
                document_id=document.id, budget=DocumentBudget(model_rounds=1)
            ),
            f"budget:{uuid4()}",
        )
        limited_context = await tasks.claim(
            TaskEnvelope(task_id=limited.task_id, generation=0), uuid4()
        )
        partial = await DocumentWorkflow(
            service, ScriptedDocumentModel()
        ).handle_extraction(
            limited_context,
            ExtractionPayload.model_validate_json(limited_context.payload_json),
        )
        assert partial.state == TaskState.PARTIAL
        assert not (await service.result(limited.extraction_id)).complete
        await tasks.finish(limited_context, partial)
        resumed = await service.submit(
            ExtractionRequest(document_id=document.id), f"interrupt:{uuid4()}"
        )
        interrupted_context = await tasks.claim(
            TaskEnvelope(task_id=resumed.task_id, generation=0), uuid4()
        )
        recovering_model = ScriptedDocumentModel()
        original_execute = DocumentAgent.execute_checkpoint

        async def interrupt_before_tool(self, *args):
            raise fail(
                ErrorCode.CANCELLED,
                "Scripted interruption after persisted model response",
            )

        monkeypatch.setattr(DocumentAgent, "execute_checkpoint", interrupt_before_tool)
        with pytest.raises(DomainError) as interrupted:
            await DocumentWorkflow(service, recovering_model).handle_extraction(
                interrupted_context,
                ExtractionPayload.model_validate_json(interrupted_context.payload_json),
            )
        await tasks.fail_task(interrupted_context, interrupted.value)
        monkeypatch.setattr(DocumentAgent, "execute_checkpoint", original_execute)
        await tasks.action(
            resumed.task_id, TaskActionRequest(operation=TaskOperation.RESUME)
        )
        queued = await tasks.get(resumed.task_id)
        restarted_context = await tasks.claim(
            TaskEnvelope(task_id=resumed.task_id, generation=queued.generation), uuid4()
        )
        recovered = await DocumentWorkflow(service, recovering_model).handle_extraction(
            restarted_context,
            ExtractionPayload.model_validate_json(restarted_context.payload_json),
        )
        assert recovered.state == TaskState.SUCCEEDED
        assert recovering_model.calls == 2
        await tasks.finish(restarted_context, recovered)
    finally:
        await database.close()
