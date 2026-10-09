from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_documents_agent_boundaries import MemoryContext
from test_tasks_recovery import Input, claim, expire, service

from mining_server.domain.core import DomainError, ErrorCode, fail
from mining_server.domain.document_validation import (
    FinishValidationDisposition,
    FinishValidationReason,
    FinishValidationResult,
)
from mining_server.domain.documents import (
    DocumentBudget,
    FinishExtractionArguments,
    PageEvidence,
    ParsedDocument,
    ParsedPage,
    QuantityUnit,
    ReadPagesArguments,
    ReportingStandard,
    ResourceCategory,
    ResourceQuantity,
    ResourceRecord,
    ResourceScope,
)
from mining_server.domain.model import (
    ModelFunctionCall,
    ModelFunctionCallOutput,
    ModelRequest,
    ModelResponse,
)
from mining_server.domain.tasks import InvocationState, StepKind, TaskKind
from mining_server.infrastructure.documents.agent import (
    DocumentAgent,
    DocumentTool,
    InMemoryDocumentPages,
)
from mining_server.infrastructure.documents.parser import PdfParser
from mining_server.infrastructure.task_models import ModelInvocation, StepRun

__all__ = ["service"]

HEADER = "JORC Resource Table. Category Tonnes (Mt) Grade (% Li2O)"
ROW = "Indicated 100 1.2"
NOTE = "Note: rounding applied"
SOURCE = HEADER + "\nMeasured 50 1.0\n" + ROW + "\nInferred 80 0.9\n" + NOTE


def candidate(
    split: bool, scope: ResourceScope = ResourceScope.IN_SITU, complete: bool = True
) -> FinishExtractionArguments:
    quotes = [HEADER, ROW, NOTE] if split else [HEADER + " " + ROW + " " + NOTE]
    return FinishExtractionArguments(
        reporting_standard=ReportingStandard.JORC,
        complete=complete,
        limitations=[] if complete else ["Resource scope could not be established"],
        records=[
            ResourceRecord(
                project="Synthetic resource table",
                reporting_standard=ReportingStandard.JORC,
                category=ResourceCategory.INDICATED,
                scope=scope,
                tonnage=ResourceQuantity(
                    value=100, unit=QuantityUnit.MEGATONNE, material="ore"
                ),
                grade=[
                    ResourceQuantity(
                        value="1.2", unit=QuantityUnit.PERCENT, material="Li2O"
                    )
                ],
                evidence=[PageEvidence(pdf_page=1, quote=quote) for quote in quotes],
            )
        ],
    )


def document() -> InMemoryDocumentPages:
    return InMemoryDocumentPages(
        ParsedDocument(
            pages=[ParsedPage(page_number=1, text=SOURCE, width=600, height=800)]
        )
    )


class FeedbackModel:
    def __init__(self):
        self.requests: list[ModelRequest] = []
        self.feedback: FinishValidationResult | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        match len(self.requests):
            case 1:
                call = ModelFunctionCall(
                    call_id="bad-finish",
                    name=DocumentTool.FINISH,
                    arguments=candidate(False).model_dump_json(),
                )
            case 2:
                for item in request.input:
                    match item:
                        case ModelFunctionCallOutput(
                            call_id="bad-finish", output=output
                        ):
                            self.feedback = FinishValidationResult.model_validate_json(
                                output
                            )
                assert self.feedback is not None
                assert self.feedback.disposition == FinishValidationDisposition.REJECTED
                assert (
                    self.feedback.issues[0].reason
                    == FinishValidationReason.QUOTE_NOT_CONTIGUOUS
                )
                assert self.feedback.issues[0].location.record_index == 0
                assert self.feedback.issues[0].location.evidence_index == 0
                assert self.feedback.issues[0].pdf_page == 1
                call = ModelFunctionCall(
                    call_id="reread",
                    name=DocumentTool.READ,
                    arguments=ReadPagesArguments(pages=[1]).model_dump_json(),
                )
            case 3:
                call = ModelFunctionCall(
                    call_id="corrected-finish",
                    name=DocumentTool.FINISH,
                    arguments=candidate(True).model_dump_json(),
                )
            case _:
                raise AssertionError("Completed model response must be reused")
        return ModelResponse(
            response_id=f"feedback-{len(self.requests)}",
            output=[call],
            tool_calls=[call],
        )


async def test_bad_finish_feedback_reread_correct_finish_and_recovery(service):
    task = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "finish-feedback"
    )
    context = await claim(service, task.id)
    model = FeedbackModel()
    agent = DocumentAgent(model, PdfParser(), "test-model")
    budget = DocumentBudget(model_rounds=3, tool_calls=3)
    first = await agent.extract(
        context, document(), Path("unused.pdf"), ReportingStandard.JORC, budget
    )
    assert first == candidate(True)
    assert first.records[0].tonnage.value == candidate(False).records[0].tonnage.value
    async with service.database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ModelInvocation)
                .where(ModelInvocation.state == InvocationState.COMPLETE)
            )
            == 3
        )
        validation_row = await session.scalar(
            select(StepRun).where(StepRun.name == f"{StepKind.DOCUMENT_TOOL}:0")
        )
        validation = FinishValidationResult.model_validate_json(
            validation_row.result_json
        )
        assert validation.disposition == FinishValidationDisposition.REJECTED
    await expire(service, context)
    await service.recover_due()
    recovered = await claim(service, task.id)
    second = await agent.extract(
        recovered, document(), Path("unused.pdf"), ReportingStandard.JORC, budget
    )
    assert second == first
    assert len(model.requests) == 3


async def test_scope_feedback_preserves_honest_partial_and_public_raise_contract():
    agent = DocumentAgent(FeedbackModel(), PdfParser(), "test-model")
    partial = candidate(True, ResourceScope.UNSPECIFIED, complete=False)
    assert (
        await agent.validate_finish(partial, document(), ReportingStandard.JORC, set())
    ).disposition == FinishValidationDisposition.ACCEPTED
    complete = candidate(True, ResourceScope.UNSPECIFIED)
    result = await agent.validate_finish(
        complete, document(), ReportingStandard.JORC, set()
    )
    assert result.issues[0].reason == FinishValidationReason.SCOPE_UNRESOLVED
    with pytest.raises(DomainError) as rejected:
        await agent.verify(complete, document(), ReportingStandard.JORC, set())
    assert rejected.value.code == ErrorCode.INVALID_INPUT


async def test_rejected_finish_exhausts_budget_without_accepting_candidate():
    model = FeedbackModel()
    context = MemoryContext()
    result = await DocumentAgent(model, PdfParser(), "test-model").extract(
        context,
        document(),
        Path("unused.pdf"),
        ReportingStandard.JORC,
        DocumentBudget(model_rounds=1, tool_calls=1),
    )
    assert not result.complete
    assert result.records == []
    assert len(model.requests) == 1
    assert context.steps[0].value.disposition == FinishValidationDisposition.REJECTED


async def test_all_stitched_quotes_are_reported_with_locations():
    agent = DocumentAgent(FeedbackModel(), PdfParser(), "test-model")
    original = candidate(False)
    multiple = FinishExtractionArguments(
        reporting_standard=original.reporting_standard,
        records=[original.records[0]] * 18,
        complete=True,
    )
    result = await agent.validate_finish(
        multiple, document(), ReportingStandard.JORC, set()
    )
    assert len(result.issues) == 18
    assert [issue.location.record_index for issue in result.issues] == list(range(18))
    assert all(
        issue.reason == FinishValidationReason.QUOTE_NOT_CONTIGUOUS
        for issue in result.issues
    )


class BrokenPages(InMemoryDocumentPages):
    async def has_quote(self, evidence: PageEvidence) -> bool:
        raise fail(ErrorCode.CONFLICT, "Immutable page read failed")


async def test_page_read_failure_is_not_candidate_feedback():
    pages = BrokenPages(
        ParsedDocument(
            pages=[ParsedPage(page_number=1, text=SOURCE, width=600, height=800)]
        )
    )
    with pytest.raises(DomainError) as failed:
        await DocumentAgent(FeedbackModel(), PdfParser(), "test-model").validate_finish(
            candidate(True), pages, ReportingStandard.JORC, set()
        )
    assert failed.value.code == ErrorCode.CONFLICT


async def test_validation_feedback_counts_against_context_budget():
    initial_context = MemoryContext()
    initial_model = FeedbackModel()
    initial_agent = DocumentAgent(initial_model, PdfParser(), "test-model")
    await initial_agent.extract(
        initial_context,
        document(),
        Path("unused.pdf"),
        ReportingStandard.JORC,
        DocumentBudget(model_rounds=1),
    )
    request = initial_model.requests[0]
    response = ModelResponse.model_validate(initial_context.models[0].value)
    limit = initial_agent.context_size(
        [*request.input, *response.output], request.tools
    )
    model = FeedbackModel()
    result = await DocumentAgent(model, PdfParser(), "test-model").extract(
        MemoryContext(),
        document(),
        Path("unused.pdf"),
        ReportingStandard.JORC,
        DocumentBudget(model_rounds=3, context_bytes=limit),
    )
    assert not result.complete and result.records == []
    assert len(model.requests) == 1
    assert any("validation feedback" in limitation for limitation in result.limitations)


class UnknownFeedbackModel(FeedbackModel):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.requests:
            self.requests.append(request)
            raise fail(ErrorCode.UNKNOWN_RESULT, "Follow-up model result is uncertain")
        return await super().complete(request)


async def test_unknown_followup_is_not_replayed_or_candidate_rejected(service):
    from mining_server.domain.tasks import TaskActionRequest, TaskOperation, TaskState

    task = await service.submit(
        TaskKind.RESOURCE_EXTRACTION, Input(value=1), "unknown-feedback"
    )
    context = await claim(service, task.id)
    model = UnknownFeedbackModel()
    with pytest.raises(DomainError) as failed:
        await DocumentAgent(model, PdfParser(), "test-model").extract(
            context,
            document(),
            Path("unused.pdf"),
            ReportingStandard.JORC,
            DocumentBudget(model_rounds=3),
        )
    assert failed.value.code == ErrorCode.UNKNOWN_RESULT
    await service.fail_task(context, failed.value)
    assert (await service.get(task.id)).state == TaskState.UNKNOWN
    with pytest.raises(DomainError) as blocked:
        await service.action(task.id, TaskActionRequest(operation=TaskOperation.RESUME))
    assert blocked.value.code == ErrorCode.UNKNOWN_RESULT
    assert len(model.requests) == 2
    async with service.database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ModelInvocation)
                .where(ModelInvocation.state == InvocationState.COMPLETE)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ModelInvocation)
                .where(ModelInvocation.state == InvocationState.UNKNOWN)
            )
            == 1
        )


async def test_reporting_standard_mismatch_remains_terminal():
    agent = DocumentAgent(FeedbackModel(), PdfParser(), "test-model")
    with pytest.raises(DomainError) as wrong_request:
        await agent.validate_finish(
            candidate(True), document(), ReportingStandard.NI_43_101, set()
        )
    assert wrong_request.value.code == ErrorCode.STANDARD_MISMATCH
    missing = InMemoryDocumentPages(
        ParsedDocument(
            pages=[
                ParsedPage(
                    page_number=1,
                    text="No reporting standard appears",
                    width=600,
                    height=800,
                )
            ]
        )
    )
    with pytest.raises(DomainError) as missing_marker:
        await agent.validate_finish(
            candidate(True), missing, ReportingStandard.JORC, set()
        )
    assert missing_marker.value.code == ErrorCode.STANDARD_MISMATCH
