from datetime import date
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from mining_contracts.domain.core import Contract
from mining_contracts.domain.documents import (
    ExtractionRequest,
    PageEvidence,
    ReportingStandard,
    ResourceCategory,
    ResourceQuantity,
    ResourceRecord,
    ResourceScope,
    parse_resource_mass_material,
)
from mining_contracts.domain.tasks import TaskState
from pydantic import Field, HttpUrl, field_validator, model_validator


class DocumentPageLimits(Contract):
    candidate_pages: int = Field(default=150, ge=1, le=150)
    read_chars: int = Field(default=24000, ge=1, le=24000)
    search_chars: int = Field(default=16000, ge=1, le=16000)
    read_pages: int = Field(default=8, ge=1, le=8)
    tools_per_round: int = Field(default=100, ge=1, le=100)
    max_pages: int = Field(default=1000, ge=1, le=1000)


class DocumentTransferLimits(Contract):
    chunk_bytes: int = Field(default=65536, ge=1, le=1024 * 1024)
    upload_chunk_bytes: int = Field(default=1024 * 1024, ge=1, le=1024 * 1024)
    header_bytes: int = Field(default=1024, ge=5, le=1024)
    disk_reserve_bytes: int = Field(default=64 * 1024 * 1024, ge=0)
    redirect_hops: int = Field(default=6, ge=1, le=6)


class ResourcePagePriority(IntEnum):
    CATEGORY_TABLE = 0
    CATEGORIES = 1
    ESTIMATE = 2
    RESOURCE = 3


class DocumentCheckpointLayout(IntEnum):
    VERSION_ONE_TOOL_STRIDE = 100


class DocumentIngestPayload(Contract):
    document_id: UUID | None = None
    pdf_url: HttpUrl | None = None

    @model_validator(mode="after")
    def one_input(self):
        if (self.document_id is None) == (self.pdf_url is None):
            raise ValueError("Exactly one document input is required")
        return self


class ExtractionPayload(Contract):
    extraction_id: UUID
    request: ExtractionRequest


class ParsedPage(Contract):
    page_number: int = Field(ge=1, le=1000)
    text: str
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class ParsedDocument(Contract):
    pages: list[ParsedPage] = Field(min_length=1, max_length=1000)


class PublicBytesResponse(Contract):
    data: bytes
    final_url: str
    content_type: str | None


class StoredDocumentContent(Contract):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=250 * 1024 * 1024)
    source_url: str | None = None


class DocumentSearchResult(Contract):
    pages: list[ParsedPage]
    truncated: bool


class DocumentCandidates(Contract):
    pages: list[int] = Field(max_length=150)
    truncated: bool


class SavedPageBatch(Contract):
    first_page: int = Field(ge=1, le=1000)
    last_page: int = Field(ge=1, le=1000)
    page_count: int = Field(ge=1, le=20)


class TableResult(Contract):
    page_number: int
    tables: list[list[list[str | None]]]


class RenderedPage(Contract):
    page_number: int
    image_data_url: str


class RenderedPageReceipt(Contract):
    page_number: int = Field(ge=1, le=1000)
    rendered: bool = True


class SearchPagesArguments(Contract):
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=8, ge=1, le=20)


class ReadPagesArguments(Contract):
    pages: list[int] = Field(min_length=1, max_length=8)


class ReadTableArguments(Contract):
    page: int = Field(ge=1, le=1000)


class RenderPageArguments(Contract):
    page: int = Field(ge=1, le=1000)


class FinishExtractionArguments(Contract):
    reporting_standard: ReportingStandard
    standard_evidence: list[PageEvidence] = Field(default_factory=list, max_length=10)
    records: list[ResourceRecord] = Field(default_factory=list, max_length=1000)
    complete: bool = Field(
        description="True when the report's adopted current base-case mineral resource statements are covered. Historical comparisons and alternative cutoff scenarios are outside the required coverage. Preserve unresolved source conflicts as null fields with limitations; mark incomplete when required resource statements remain unexamined or unsupported."
    )
    limitations: list[str] = Field(default_factory=list, max_length=100)


class DocumentPageAccess(Protocol):
    page_count: int

    async def search(self, arguments: SearchPagesArguments) -> DocumentSearchResult: ...

    async def read(self, arguments: ReadPagesArguments) -> DocumentSearchResult: ...

    async def candidates(self) -> DocumentCandidates: ...

    async def has_standard(self, standard: ReportingStandard) -> bool: ...

    async def has_quote(self, evidence: PageEvidence) -> bool: ...


class GoldenResourceExpectation(Contract):
    project_fragment: str = Field(min_length=1)
    deposit_fragment: str | None = None
    category: ResourceCategory
    scope: ResourceScope | None = None
    report_date: date
    effective_date: date
    tonnage: ResourceQuantity
    grade: list[ResourceQuantity] = Field(min_length=1)
    contained_materials: list[ResourceQuantity] = Field(default_factory=list)
    cutoff: list[ResourceQuantity] = Field(min_length=1)
    source_pages: list[int] = Field(min_length=1)

    @field_validator("tonnage")
    @classmethod
    def resource_mass(cls, value: ResourceQuantity) -> ResourceQuantity:
        parse_resource_mass_material(value.material)
        return value


class GoldenReport(Contract):
    filename: str = Field(pattern=r"^[a-z0-9-]+\.pdf$")
    pdf_url: HttpUrl
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1, le=1000)
    reporting_standard: ReportingStandard
    expectations: list[GoldenResourceExpectation] = Field(min_length=1)
    absent_stockpile_inferred: bool = False


class GoldenReports(Contract):
    reports: list[GoldenReport] = Field(min_length=1)


class GoldenVerification(Contract):
    filename: str
    passed: bool
    failures: list[str] = Field(default_factory=list)
    matched_records: int = Field(ge=0)


class DocumentAcceptanceMode(StrEnum):
    LIVE = "live"
    VERIFY = "verify"


class DocumentAcceptanceOutcome(Contract):
    filename: str
    task_id: UUID | None = None
    extraction_id: UUID | None = None
    state: TaskState | None = None
    verification: GoldenVerification | None = None


class DocumentAcceptanceRequest(Contract):
    mode: DocumentAcceptanceMode
    golden: Path
    fixture_dir: Path
    output_dir: Path
    result_dir: Path | None = None
    backend_url: HttpUrl = HttpUrl("http://127.0.0.1:28110")
    authorize_model_calls: bool = False
    run_id: str | None = Field(default=None, min_length=1, max_length=200)
    timeout_seconds: int = Field(default=1200, ge=1, le=7200)

    @model_validator(mode="after")
    def execution_boundary(self):
        if self.mode == DocumentAcceptanceMode.LIVE and (
            not self.authorize_model_calls or self.run_id is None
        ):
            raise ValueError(
                "Live acceptance requires explicit model-call authorization and a stable run ID"
            )
        if self.mode == DocumentAcceptanceMode.VERIFY and self.result_dir is None:
            raise ValueError("Verify mode requires a result directory")
        return self
