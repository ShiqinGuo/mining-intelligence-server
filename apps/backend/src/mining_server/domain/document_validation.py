from enum import StrEnum

from mining_contracts.domain.core import Contract, ErrorCode
from pydantic import Field, model_validator


class FinishValidationDisposition(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class FinishValidationTarget(StrEnum):
    REPORTING_STANDARD = "reporting_standard"
    STANDARD_EVIDENCE = "standard_evidence"
    RECORD_STANDARD = "record_standard"
    RECORD_EVIDENCE = "record_evidence"
    RESOURCE_CATEGORY = "resource_category"
    RESOURCE_SCOPE = "resource_scope"
    RESOURCE_QUANTITY = "resource_quantity"


class FinishValidationReason(StrEnum):
    ACTUAL_STANDARD_REQUIRED = "actual_standard_required"
    RECORD_STANDARD_MISMATCH = "record_standard_mismatch"
    PAGE_MISSING = "page_missing"
    QUOTE_NOT_CONTIGUOUS = "quote_not_contiguous"
    IMAGE_NOT_RENDERED = "image_not_rendered"
    CATEGORY_MISSING = "category_missing"
    NUMBER_MISSING = "number_missing"
    INVALID_TONNAGE_MATERIAL = "invalid_tonnage_material"
    UNIT_OR_MATERIAL_MISSING = "unit_or_material_missing"
    SCOPE_UNRESOLVED = "scope_unresolved"


class ResourceQuantityGroup(StrEnum):
    TONNAGE = "tonnage"
    GRADE = "grade"
    CONTAINED_MATERIALS = "contained_materials"
    CUTOFF = "cutoff"


class FinishValidationLocation(Contract):
    target: FinishValidationTarget
    record_index: int | None = Field(default=None, ge=0)
    evidence_index: int | None = Field(default=None, ge=0)
    quantity_group: ResourceQuantityGroup | None = None
    quantity_index: int | None = Field(default=None, ge=0)


class FinishValidationIssue(Contract):
    code: ErrorCode
    reason: FinishValidationReason
    message: str = Field(min_length=1)
    location: FinishValidationLocation
    pdf_page: int | None = Field(default=None, ge=1)


class FinishValidationResult(Contract):
    disposition: FinishValidationDisposition
    issues: list[FinishValidationIssue] = Field(default_factory=list, max_length=256)
    truncated: bool = False
    guidance: str = "Record, evidence and quantity indices are zero-based; PDF pages are one-based. Each text quote must be a contiguous original page fragment. Split headings, rows and footnotes into separate evidence entries instead of concatenating omitted rows or reordering labels. Read the cited source pages or tables, or render source pages for image evidence. Do not alter numerical fields merely to satisfy validation; any revised candidate must use values supported by the original report. Distinguish in_situ, stockpile and total scopes using document evidence. Deposit or block-model resource estimates are in_situ only when the document supports that interpretation; keep stockpile and combined totals separate. If scope cannot be established, return complete=false with explicit limitations instead of guessing."

    @model_validator(mode="after")
    def require_consistent_disposition(self):
        match self.disposition:
            case FinishValidationDisposition.ACCEPTED:
                if self.issues:
                    raise ValueError("Accepted finish validation cannot contain issues")
            case FinishValidationDisposition.REJECTED:
                if not self.issues:
                    raise ValueError("Rejected finish validation requires an issue")
        return self


class FinishValidationLimits(Contract):
    issue_limit: int = Field(default=64, ge=1, le=256)
