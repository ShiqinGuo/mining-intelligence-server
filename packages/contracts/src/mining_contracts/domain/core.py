from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    UPSTREAM_FAILURE = "upstream_failure"
    NO_QUOTE = "no_quote"
    COVERAGE_INSUFFICIENT = "coverage_insufficient"
    STANDARD_MISMATCH = "standard_mismatch"
    WAITING_AUTH = "waiting_auth"
    WAITING_QUOTA = "waiting_quota"
    BUSY = "busy"
    UNKNOWN_RESULT = "unknown_result"
    LEASE_LOST = "lease_lost"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


class ErrorDetails(Contract):
    definitive_rejection: bool = False
    field: str | None = None
    task_id: UUID | None = None
    retryable: bool = False
    retry_after: int | None = Field(default=None, ge=1)
    reason: str | None = None


class ErrorResponse(Contract):
    code: ErrorCode
    message: str
    request_id: str
    details: ErrorDetails = ErrorDetails()
