import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import ConfigDict, Field, HttpUrl, model_validator

from mining_contracts.domain.core import Contract


class Commodity(StrEnum):
    LITHIUM_CARBONATE = "lithium_carbonate"
    SPODUMENE = "spodumene"
    LITHIUM_HYDROXIDE = "lithium_hydroxide"


class PriceKind(StrEnum):
    SPOT_ASSESSMENT = "spot_assessment"
    FORWARD_PHYSICAL_SPOT = "forward_physical_spot_assessment"
    ISSUER_REALIZED = "issuer_realized_price"
    FUTURES_CONTRACT = "futures_contract"


class PriceFrequency(StrEnum):
    TRADING_DAY = "trading_day"
    SPARSE = "sparse"
    QUARTERLY = "quarterly"


class Currency(StrEnum):
    CNY = "CNY"
    USD = "USD"


class PriceUnit(StrEnum):
    TONNE = "tonne"
    KILOGRAM = "kilogram"


class TaxBasis(StrEnum):
    VAT_13_INCLUDED = "vat_13_included"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class DeliveryBasis(StrEnum):
    DELIVERED = "delivered"
    PICKUP = "pickup"
    CIF = "cif"
    FOB = "fob"
    EXCHANGE = "exchange"
    UNKNOWN = "unknown"


class PriceSession(StrEnum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    DAILY = "daily"
    PERIOD = "period"


class PriceAdapter(StrEnum):
    MYSTEEL_JSON = "mysteel_json"
    SINA_DAILY = "sina_daily"
    MYSTEEL_ARTICLE = "mysteel_article"
    PLS_REPORT = "pls_report"


class MysteelIndex(StrEnum):
    BATTERY_MORNING = "ID01551919"
    TECHNICAL_MORNING = "ID01551906"
    BATTERY_AFTERNOON = "ID01720085"


class LookupMode(StrEnum):
    EXACT = "exact"
    LATEST_AVAILABLE = "latest_available"


class InstrumentCreate(Contract):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{2,150}$")
    name: str = Field(min_length=1)
    commodity: Commodity
    grade: str
    purity_basis: str
    purity_min: Decimal = Field(ge=0, le=100)
    hydrate_form: str | None = None
    region: str
    origin: str | None = None
    issuer: str | None = None
    project: str | None = None
    price_kind: PriceKind
    frequency: PriceFrequency
    currency: Currency
    unit: PriceUnit
    tax_basis: TaxBasis
    delivery_basis: DeliveryBasis
    session: PriceSession
    adapter: PriceAdapter
    source_symbol: str
    source_url: HttpUrl
    methodology_url: HttpUrl | None = None
    methodology_version: str | None = None
    usage_notice: str
    enabled: bool = True

    @model_validator(mode="after")
    def validate_adapter(self) -> "InstrumentCreate":
        if self.adapter == PriceAdapter.SINA_DAILY and not re.fullmatch(
            r"LC\d{4}", self.source_symbol
        ):
            raise ValueError("Sina requires a specific LCYYMM contract")
        if self.adapter == PriceAdapter.MYSTEEL_JSON and not re.fullmatch(
            r"ID\d{8}", self.source_symbol
        ):
            raise ValueError("Mysteel requires a published index identifier")
        if self.adapter in {PriceAdapter.MYSTEEL_JSON, PriceAdapter.SINA_DAILY} and (
            self.commodity != Commodity.LITHIUM_CARBONATE
            or self.currency != Currency.CNY
            or self.unit != PriceUnit.TONNE
        ):
            raise ValueError(
                "Carbonate adapters require lithium carbonate quoted in CNY per tonne"
            )
        if self.adapter == PriceAdapter.MYSTEEL_JSON:
            if (
                self.price_kind != PriceKind.SPOT_ASSESSMENT
                or self.tax_basis != TaxBasis.VAT_13_INCLUDED
            ):
                raise ValueError(
                    "Mysteel carbonate instruments require VAT-inclusive spot assessment semantics"
                )
            match MysteelIndex(self.source_symbol):
                case MysteelIndex.BATTERY_MORNING:
                    purity, session, delivery = (
                        Decimal("99.5"),
                        PriceSession.MORNING,
                        DeliveryBasis.DELIVERED,
                    )
                case MysteelIndex.TECHNICAL_MORNING:
                    purity, session, delivery = (
                        Decimal("99.2"),
                        PriceSession.MORNING,
                        DeliveryBasis.PICKUP,
                    )
                case MysteelIndex.BATTERY_AFTERNOON:
                    purity, session, delivery = (
                        Decimal("99.5"),
                        PriceSession.AFTERNOON,
                        DeliveryBasis.DELIVERED,
                    )
                case _:
                    raise ValueError(
                        "Mysteel index methodology has not been verified for this identifier"
                    )
            if (
                self.purity_min,
                self.session,
                self.delivery_basis,
                self.purity_basis,
            ) != (purity, session, delivery, "Li2CO3"):
                raise ValueError(
                    "Instrument specification conflicts with the verified Mysteel identifier"
                )
        if (
            self.adapter == PriceAdapter.SINA_DAILY
            and self.price_kind != PriceKind.FUTURES_CONTRACT
        ):
            raise ValueError("Sina contract quotes cannot be labeled as physical spot")
        if self.adapter == PriceAdapter.MYSTEEL_ARTICLE and (
            self.price_kind != PriceKind.FORWARD_PHYSICAL_SPOT
            or self.frequency != PriceFrequency.SPARSE
        ):
            raise ValueError(
                "Public article observations require sparse forward physical spot semantics"
            )
        if self.adapter == PriceAdapter.PLS_REPORT and (
            self.price_kind != PriceKind.ISSUER_REALIZED
            or self.frequency != PriceFrequency.QUARTERLY
        ):
            raise ValueError(
                "PLS report observations require issuer realised quarterly semantics"
            )
        return self


class PriceInstrument(InstrumentCreate):
    id: UUID
    revision: int = Field(ge=1)


class InstrumentUpdate(InstrumentCreate):
    expected_revision: int = Field(ge=1)


class PricePoint(Contract):
    model_config = ConfigDict(allow_inf_nan=False, extra="ignore")
    instrument_id: UUID
    observed_date: date
    published_at: datetime | None = None
    publication_date: date | None = None
    asof_at: datetime
    fetched_at: datetime
    period_start: date | None = None
    period_end: date | None = None
    value: Decimal = Field(gt=0)
    low: Decimal | None = Field(default=None, gt=0)
    high: Decimal | None = Field(default=None, gt=0)
    open: Decimal | None = Field(default=None, gt=0)
    settlement: Decimal | None = Field(default=None, gt=0)
    volume: int | None = Field(default=None, ge=0)
    open_interest: int | None = Field(default=None, ge=0)
    estimated: bool = False
    derived: bool = False
    source_url: HttpUrl
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_point(self) -> "PricePoint":
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError("Price range is reversed")
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_start > self.period_end
        ):
            raise ValueError("Statistical period is reversed")
        if self.asof_at.tzinfo is None or self.fetched_at.tzinfo is None:
            raise ValueError("Observation timestamps must include a timezone")
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("Publication timestamp must include a timezone")
        return self


class PriceCoverage(Contract):
    count: int
    first_date: date | None
    last_date: date | None
    frequency: PriceFrequency
    complete_daily: bool
    missing_calendar_days: int
    notice: str


class PriceResponse(Contract):
    instrument: PriceInstrument
    point: PricePoint
    mode: LookupMode
    requested_date: date


class TrendResponse(Contract):
    instrument: PriceInstrument
    points: list[PricePoint]
    coverage: PriceCoverage
    absolute_change: Decimal | None = None
    percentage_change: Decimal | None = None


class InstrumentList(Contract):
    instruments: list[PriceInstrument]


class MarketRefreshResult(Contract):
    instrument_id: UUID
    observed: int
    stored: int
    first_date: date | None
    last_date: date | None
