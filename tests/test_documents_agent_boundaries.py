from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pytest
from pydantic import BaseModel

from mining_server.domain.core import DomainError
from mining_server.domain.documents import (
    DocumentBudget,
    EvidenceKind,
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
    TableResult,
)
from mining_server.domain.model import (
    ModelFunctionCall,
    ModelFunctionCallOutput,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
)
from mining_server.infrastructure.documents.agent import (
    DocumentAgent,
    DocumentTool,
    InMemoryDocumentPages,
)
from mining_server.infrastructure.documents.parser import PdfParser


@dataclass
class MemoryResult:
    key: str
    value: BaseModel


class MemoryContext:
    def __init__(self):
        self.models: list[MemoryResult] = []
        self.steps: list[MemoryResult] = []

    async def check_cancelled(self):
        return None

    async def model_call[T: BaseModel](self, key, result_type: type[T], action) -> T:
        name = key.storage_key()
        for entry in self.models:
            if entry.key == name:
                return result_type.model_validate(entry.value)
        result = await action()
        self.models.append(MemoryResult(name, result))
        return result

    async def step[T: BaseModel](self, key, result_type: type[T], action) -> T:
        name = key.storage_key()
        for entry in self.steps:
            if entry.key == name:
                return result_type.model_validate(entry.value)
        result = await action()
        self.steps.append(MemoryResult(name, result))
        return result


class ScannedModel:
    def __init__(self):
        self.requests: list[ModelRequest] = []

    async def complete(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            call = ModelToolCall(
                call_id="render-call", name=DocumentTool.RENDER, arguments='{"page":1}'
            )
        else:
            quote = "JORC In-situ Indicated 100 Mt 1.2% Li2O"
            evidence = PageEvidence(pdf_page=1, quote=quote, kind=EvidenceKind.IMAGE)
            finish = FinishExtractionArguments(
                reporting_standard=ReportingStandard.JORC,
                standard_evidence=[evidence],
                records=[
                    ResourceRecord(
                        project="Scanned Project",
                        reporting_standard=ReportingStandard.JORC,
                        category=ResourceCategory.INDICATED,
                        scope=ResourceScope.IN_SITU,
                        tonnage=ResourceQuantity(
                            value=100, unit=QuantityUnit.MEGATONNE, material="ore"
                        ),
                        grade=[
                            ResourceQuantity(
                                value="1.2", unit=QuantityUnit.PERCENT, material="Li2O"
                            )
                        ],
                        evidence=[evidence],
                    )
                ],
                complete=True,
            )
            call = ModelToolCall(
                call_id="finish-call",
                name=DocumentTool.FINISH,
                arguments=finish.model_dump_json(),
            )
        return ModelResponse(
            response_id=f"response-{len(self.requests)}",
            tool_calls=[call],
            output=[
                ModelFunctionCall(
                    call_id=call.call_id,
                    name=call.name,
                    namespace=call.namespace,
                    arguments=call.arguments,
                )
            ],
        )


async def test_scanned_standard_can_use_rendered_evidence_and_resume_without_duplicate_image(
    tmp_path,
):
    path = tmp_path / "scan.pdf"
    with pymupdf.open() as original:
        page = original.new_page()
        page.insert_text((40, 40), "JORC In-situ Indicated 100 Mt 1.2% Li2O")
        image = page.get_pixmap().tobytes("png")
        with pymupdf.open() as scanned:
            scanned.new_page().insert_image(page.rect, stream=image)
            scanned.save(path)
    parsed = await PdfParser().parse(path)
    assert parsed.pages[0].text == ""
    context = MemoryContext()
    model = ScannedModel()
    agent = DocumentAgent(model, PdfParser(), "gpt-6.1-sol")
    first = await agent.extract(
        context,
        InMemoryDocumentPages(parsed),
        path,
        ReportingStandard.JORC,
        DocumentBudget(),
    )
    second = await agent.extract(
        context,
        InMemoryDocumentPages(parsed),
        path,
        ReportingStandard.JORC,
        DocumentBudget(),
    )
    assert first == second
    assert len(model.requests) == 2
    image_inputs = [
        item
        for item in model.requests[1].input
        if isinstance(item, ModelMessage) and isinstance(item.content, list)
    ]
    assert len(image_inputs) == 1
    outputs = [
        item.output
        for item in model.requests[1].input
        if isinstance(item, ModelFunctionCallOutput)
    ]
    assert outputs and all("data:image" not in output for output in outputs)


async def test_context_budget_stops_before_any_new_model_call():
    model = ScannedModel()
    parsed = ParsedDocument(
        pages=[ParsedPage(page_number=1, text="JORC", width=600, height=800)]
    )
    result = await DocumentAgent(model, PdfParser(), "gpt-6.1-sol").extract(
        MemoryContext(),
        InMemoryDocumentPages(parsed),
        Path("unused.pdf"),
        ReportingStandard.JORC,
        DocumentBudget(context_bytes=1024),
    )
    assert not result.complete
    assert "bytes" in result.limitations[0]
    assert model.requests == []


async def test_truncated_page_content_is_explicit():
    parsed = ParsedDocument(
        pages=[
            ParsedPage(page_number=1, text="JORC " + "a" * 30000, width=600, height=800)
        ]
    )
    result = await InMemoryDocumentPages(parsed).read(
        ReadPagesArguments(pages=[1]),
    )
    assert result.truncated


def test_adjacent_tonnage_unit_is_valid_without_accepting_unrelated_tokens():
    quote = "The Pilgangoora Mineral Resource is reported as 446Mt at 1.28% Li2O and 122 ppm Ta2O5."
    quantity = ResourceQuantity(value=446, unit=QuantityUnit.MEGATONNE, material="ore")
    agent = DocumentAgent(None, PdfParser(), "gpt-6.1-sol")
    agent.verify_quantity(quantity, quote, is_tonnage=True)
    with pytest.raises(DomainError):
        agent.verify_quantity(
            quantity, "The resource contains Li446O and 100 Mt of ore", is_tonnage=True
        )


class OversizedBatchModel:
    def __init__(self):
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        calls = [
            ModelToolCall(
                call_id=f"read-{index}",
                name=DocumentTool.READ,
                arguments='{"pages":[1]}',
            )
            for index in range(101)
        ]
        return ModelResponse(
            response_id="oversized-batch",
            tool_calls=calls,
            output=[
                ModelFunctionCall(
                    call_id=call.call_id,
                    name=call.name,
                    namespace=call.namespace,
                    arguments=call.arguments,
                )
                for call in calls
            ],
        )


async def test_oversized_round_is_rejected_before_checkpoint_keys_can_collide():
    model = OversizedBatchModel()
    context = MemoryContext()
    parsed = ParsedDocument(
        pages=[ParsedPage(page_number=1, text="JORC resources", width=600, height=800)]
    )
    agent = DocumentAgent(model, PdfParser(), "gpt-6.1-sol")
    budget = DocumentBudget(tool_calls=400)
    result = await agent.extract(
        context,
        InMemoryDocumentPages(parsed),
        Path("unused.pdf"),
        ReportingStandard.JORC,
        budget,
    )
    resumed = await agent.extract(
        context,
        InMemoryDocumentPages(parsed),
        Path("unused.pdf"),
        ReportingStandard.JORC,
        budget,
    )
    assert not result.complete
    assert "no tools" in result.limitations[0]
    assert result == resumed
    assert model.calls == 1
    assert context.steps == []


async def test_large_tool_result_stops_remaining_tools_in_the_same_round():
    class TwoTablesModel:
        async def complete(self, request):
            calls = [
                ModelToolCall(
                    call_id=f"table-{number}",
                    name=DocumentTool.TABLE,
                    arguments='{"page":1}',
                )
                for number in range(2)
            ]
            return ModelResponse(
                response_id="two-tables",
                tool_calls=calls,
                output=[
                    ModelFunctionCall(
                        call_id=call.call_id,
                        name=call.name,
                        namespace=call.namespace,
                        arguments=call.arguments,
                    )
                    for call in calls
                ],
            )

    class LargeTableParser:
        calls = 0

        async def table(self, path, page):
            self.calls += 1
            return TableResult(page_number=page, tables=[[["x" * 50000]]])

    parser = LargeTableParser()
    context = MemoryContext()
    pages = InMemoryDocumentPages(
        ParsedDocument(
            pages=[ParsedPage(page_number=1, text="JORC", width=600, height=800)]
        )
    )
    result = await DocumentAgent(TwoTablesModel(), parser, "gpt-6.1-sol").extract(
        context,
        pages,
        Path("unused.pdf"),
        ReportingStandard.JORC,
        DocumentBudget(context_bytes=40000),
    )
    assert not result.complete
    assert "after tool result" in result.limitations[0]
    assert parser.calls == 1
    assert len(context.steps) == 1
