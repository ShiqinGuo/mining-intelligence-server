from datetime import date
from decimal import Decimal
from enum import StrEnum

from mining_contracts.domain.core import Contract
from mining_contracts.domain.market import PriceInstrument, PricePoint
from pydantic import ConfigDict, Field, model_validator


class MarketDateRange(Contract):
    start: date
    end: date

    @model_validator(mode="after")
    def validate_dates(self) -> "MarketDateRange":
        if self.start > self.end:
            raise ValueError("Date range is reversed")
        return self


class MarketRefreshPayload(MarketDateRange):
    instrument: PriceInstrument


class MarketRefreshRequest(MarketDateRange):
    pass


class MarketBatch(Contract):
    points: list[PricePoint]


class MarketLimits(Contract):
    history_days: int = Field(default=3650, ge=1)
    earliest_observation_date: date = date(1900, 1, 1)
    refresh_lookback_days: int = Field(default=7, ge=1)
    response_bytes: int = Field(default=2_000_000, ge=1)
    stream_chunk_bytes: int = Field(default=65536, ge=1)
    report_bytes: int = Field(default=5_000_000, ge=1)
    public_timeout_seconds: float = Field(default=60, gt=0)
    discovered_article_limit: int = Field(default=3, ge=1)
    evidence_prefix_characters: int = Field(default=80, ge=0)
    evidence_suffix_characters: int = Field(default=120, ge=0)


class ReportQuarter(StrEnum):
    MARCH = "Mar"
    JUNE = "Jun"
    SEPTEMBER = "Sep"


class ReportPublicationMonth(StrEnum):
    JULY = "July"
    OCTOBER = "October"


class MysteelStatus(StrEnum):
    SUCCESS = "200"


class MysteelUnit(StrEnum):
    YUAN_PER_TONNE = "元/吨"


class MysteelSeries(Contract):
    model_config = ConfigDict(allow_inf_nan=False, extra="ignore")
    indexCode: str
    yAxis: list[Decimal]


class MysteelHistory(Contract):
    model_config = ConfigDict(extra="ignore")
    xAxis: list[date]
    unitName: MysteelUnit
    datas: list[MysteelSeries]
    indexName: str
    indexCode: str


class MysteelEnvelope(Contract):
    model_config = ConfigDict(extra="ignore")
    status: MysteelStatus
    message: str
    response: str
    isValid: bool


class SinaDay(Contract):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    d: date
    o: Decimal = Field(gt=0)
    h: Decimal = Field(gt=0)
    l: Decimal = Field(gt=0)
    c: Decimal = Field(gt=0)
    v: int = Field(ge=0)
    p: int = Field(ge=0)
    s: Decimal = Field(gt=0)
