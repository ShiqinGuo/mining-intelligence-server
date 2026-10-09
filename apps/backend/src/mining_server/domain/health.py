from datetime import datetime
from enum import StrEnum
from uuid import UUID

from mining_contracts.domain.core import Contract
from pydantic import field_validator


class HealthOrigin(StrEnum):
    API = "api"
    BEAT = "beat"


class HealthProbe(Contract):
    origin: HealthOrigin
    nonce: UUID
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Health timestamps require a timezone")
        return value


class HealthEvidence(HealthProbe):
    observed_at: datetime


class HealthResponse(Contract):
    ready: bool
    database: bool = False
    worker: bool = False
    beat: bool = False
    publisher: bool = False
