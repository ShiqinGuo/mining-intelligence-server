from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import Field, HttpUrl, field_validator, model_validator

from mining_contracts.domain.core import Contract
from mining_contracts.domain.tasks import TaskState


class DocumentStatus(StrEnum):
    STORED = "stored"
    READY = "ready"
    FAILED = "failed"


class ReportingStandard(StrEnum):
    AUTO = "auto"
    NI_43_101 = "ni_43_101"
    JORC = "jorc"
    UNKNOWN = "unknown"


class ResourceCategory(StrEnum):
    MEASURED = "measured"
    INDICATED = "indicated"
    INFERRED = "inferred"
    MEASURED_INDICATED = "measured_indicated"


class ResourceScope(StrEnum):
    IN_SITU = "in_situ"
    STOCKPILE = "stockpile"
    TOTAL = "total"
    UNSPECIFIED = "unspecified"


class QuantityUnit(StrEnum):
    TONNE = "t"
    KILOTONNE = "kt"
    MEGATONNE = "Mt"
    OUNCE = "oz"
    KILOOUNCE = "koz"
    MILLION_OUNCE = "Moz"
    POUND = "lb"
    MILLION_POUND = "Mlb"
    PERCENT = "%"
    PPM = "ppm"
    GRAM_PER_TONNE = "g/t"


class EvidenceKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"


class ResourceQuantity(Contract):
    value: Decimal = Field(ge=0, allow_inf_nan=False)
    unit: QuantityUnit
    material: str = Field(min_length=1, max_length=100)


class ResourceMassMaterial(StrEnum):
    ORE = "ore"
    MINERALIZED_MATERIAL = "mineralized material"


def parse_resource_mass_material(material: str) -> ResourceMassMaterial:
    return ResourceMassMaterial(" ".join(material.split()).casefold())


class PageEvidence(Contract):
    pdf_page: int = Field(ge=1, le=1000)
    printed_page: str | None = Field(default=None, max_length=40)
    table_id: str | None = Field(default=None, max_length=100)
    quote: str = Field(min_length=1, max_length=16000)
    kind: EvidenceKind = EvidenceKind.TEXT


class ResourceRecord(Contract):
    project: str = Field(min_length=1, max_length=300)
    reporting_standard: ReportingStandard
    category: ResourceCategory
    report_date: date | None = None
    effective_date: date | None = None
    deposit: str | None = Field(default=None, max_length=300)
    scope: ResourceScope = ResourceScope.UNSPECIFIED
    tonnage: ResourceQuantity | None = Field(
        default=None,
        description="Total resource material mass. Material must be ore or mineralized material; contained metals or compounds belong in contained_materials.",
    )
    grade: list[ResourceQuantity] = Field(default_factory=list, max_length=20)
    contained_materials: list[ResourceQuantity] = Field(
        default_factory=list, max_length=20
    )
    cutoff: list[ResourceQuantity] = Field(default_factory=list, max_length=20)
    ownership_basis: str | None = Field(default=None, max_length=500)
    inclusive_of_reserves: bool | None = None
    evidence: list[PageEvidence] = Field(min_length=1, max_length=30)

    @field_validator("tonnage")
    @classmethod
    def resource_mass(cls, value: ResourceQuantity | None) -> ResourceQuantity | None:
        if value is not None:
            parse_resource_mass_material(value.material)
        return value

    @model_validator(mode="after")
    def actual_standard(self):
        if self.reporting_standard == ReportingStandard.AUTO:
            raise ValueError("A record must state an actual reporting standard")
        return self


class DocumentBudget(Contract):
    model_rounds: int = Field(default=20, ge=1, le=100)
    tool_calls: int = Field(default=80, ge=1, le=400)
    timeout_seconds: int = Field(default=600, ge=30, le=3600)
    visible_pages: int = Field(default=30, ge=1, le=200)
    context_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)


class DocumentUrlRequest(Contract):
    pdf_url: HttpUrl


class ExtractionRequest(Contract):
    pdf_url: HttpUrl | None = None
    document_id: UUID | None = None
    standard: ReportingStandard = ReportingStandard.AUTO
    budget: DocumentBudget = DocumentBudget()

    @model_validator(mode="after")
    def one_input(self):
        if (self.pdf_url is None) == (self.document_id is None):
            raise ValueError("Exactly one of pdf_url and document_id is required")
        if self.standard == ReportingStandard.UNKNOWN:
            raise ValueError("Unknown is not a requested reporting standard")
        return self


class DocumentResponse(Contract):
    id: UUID
    task_id: UUID | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=250 * 1024 * 1024)
    page_count: int | None = Field(default=None, ge=1, le=1000)
    status: DocumentStatus
    source_url: str | None = None
    created_at: datetime


class ResourceExtractionResult(Contract):
    extraction_id: UUID | None = None
    document_id: UUID
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reporting_standard: ReportingStandard
    standard_evidence: list[PageEvidence] = Field(default_factory=list, max_length=10)
    records: list[ResourceRecord] = Field(default_factory=list, max_length=1000)
    complete: bool
    limitations: list[str] = Field(default_factory=list, max_length=100)


class ExtractionSubmission(Contract):
    extraction_id: UUID
    task_id: UUID | None
    result: ResourceExtractionResult | None = None


class ExtractionResponse(Contract):
    id: UUID
    task_id: UUID | None
    document_id: UUID | None
    state: TaskState
    result_ready: bool
    error_message: str | None = None
