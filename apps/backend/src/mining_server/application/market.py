from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
from mining_contracts.domain.core import ErrorCode
from mining_contracts.domain.market import (
    InstrumentCreate,
    InstrumentList,
    InstrumentUpdate,
    LookupMode,
    MarketRefreshResult,
    PriceCoverage,
    PriceInstrument,
    PriceResponse,
    TrendResponse,
)
from mining_contracts.domain.task_views import TaskView
from mining_contracts.domain.tasks import StepKind, TaskKind
from sqlalchemy.ext.asyncio import AsyncSession

from mining_server.application.tasks import HandlerRegistry, TaskContext, TaskService
from mining_server.domain.core import fail
from mining_server.domain.market import (
    MarketBatch,
    MarketLimits,
    MarketRefreshPayload,
    MarketRefreshRequest,
)
from mining_server.domain.tasks import TaskOutcome
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.market.adapters import MarketHttpAdapter
from mining_server.infrastructure.market.repository import MarketRepository
from mining_server.infrastructure.settings import Settings


class MarketService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        tasks: TaskService,
        repository_factory: type[MarketRepository] = MarketRepository,
    ):
        self.database = database
        self.settings = settings
        self.tasks = tasks
        self.repositories = repository_factory

    async def instruments(self) -> InstrumentList:
        async with self.database.sessions() as session:
            return InstrumentList(
                instruments=await self.repositories(session).instruments()
            )

    async def get_instrument(self, slug: str) -> PriceInstrument:
        async with self.database.sessions() as session:
            repository = self.repositories(session)
            row = await repository.instrument(slug)
            if row is None:
                raise fail(
                    ErrorCode.NOT_FOUND,
                    "Unknown price instrument; choose an explicit instrument slug",
                )
            return repository.instrument_view(row)

    async def create(self, request: InstrumentCreate) -> PriceInstrument:
        async with self.database.sessions() as session, session.begin():
            repository = self.repositories(session)
            if await repository.instrument(request.slug):
                raise fail(ErrorCode.CONFLICT, "Price instrument already exists")
            return await repository.add_instrument(request)

    async def update(self, slug: str, request: InstrumentUpdate) -> PriceInstrument:
        if request.slug != slug:
            raise fail(ErrorCode.INVALID_INPUT, "Instrument slug is immutable")
        async with self.database.sessions() as session, session.begin():
            repository = self.repositories(session)
            row = await repository.instrument(slug, lock=True)
            if row is None:
                raise fail(ErrorCode.NOT_FOUND, "Price instrument was not found")
            if row.revision != request.expected_revision:
                raise fail(ErrorCode.CONFLICT, "Price instrument revision changed")
            previous = repository.instrument_view(row)
            definition = InstrumentCreate(
                slug=request.slug,
                name=request.name,
                commodity=request.commodity,
                grade=request.grade,
                purity_basis=request.purity_basis,
                purity_min=request.purity_min,
                hydrate_form=request.hydrate_form,
                region=request.region,
                origin=request.origin,
                issuer=request.issuer,
                project=request.project,
                price_kind=request.price_kind,
                frequency=request.frequency,
                currency=request.currency,
                unit=request.unit,
                tax_basis=request.tax_basis,
                delivery_basis=request.delivery_basis,
                session=request.session,
                adapter=request.adapter,
                source_symbol=request.source_symbol,
                source_url=request.source_url,
                methodology_url=request.methodology_url,
                methodology_version=request.methodology_version,
                usage_notice=request.usage_notice,
                enabled=request.enabled,
            )
            if (
                previous.commodity,
                previous.grade,
                previous.purity_basis,
                previous.purity_min,
                previous.currency,
                previous.unit,
                previous.price_kind,
                previous.frequency,
                previous.delivery_basis,
                previous.tax_basis,
                previous.session,
                previous.adapter,
                previous.source_symbol,
            ) != (
                definition.commodity,
                definition.grade,
                definition.purity_basis,
                definition.purity_min,
                definition.currency,
                definition.unit,
                definition.price_kind,
                definition.frequency,
                definition.delivery_basis,
                definition.tax_basis,
                definition.session,
                definition.adapter,
                definition.source_symbol,
            ):
                raise fail(
                    ErrorCode.CONFLICT,
                    "Create a new instrument when quote semantics change",
                )
            row.revision += 1
            row.payload = definition.model_dump_json()
            return repository.instrument_view(row)

    async def refresh(
        self, slug: str, request: MarketRefreshRequest, key: str
    ) -> TaskView:
        instrument = await self.get_instrument(slug)
        if not instrument.enabled:
            raise fail(ErrorCode.CONFLICT, "Price instrument is disabled")
        return await self.tasks.submit(
            TaskKind.COLLECT_PRICES,
            MarketRefreshPayload(
                instrument=instrument, start=request.start, end=request.end
            ),
            key,
            input_revision=str(instrument.revision),
        )

    async def price(
        self,
        slug: str,
        requested_date: date,
        mode: LookupMode,
        known_at: datetime | None = None,
    ) -> PriceResponse:
        instrument = await self.get_instrument(slug)
        asof = known_at or datetime.now(UTC)
        if asof.tzinfo is None:
            raise fail(
                ErrorCode.INVALID_INPUT, "Knowledge timestamp must include a timezone"
            )
        start = (
            requested_date
            if mode == LookupMode.EXACT
            else MarketLimits().earliest_observation_date
        )
        async with self.database.sessions() as session:
            points = await self.repositories(session).points(
                instrument.id, start, requested_date, asof
            )
        if mode == LookupMode.LATEST_AVAILABLE:
            points = [
                point
                for point in points
                if (
                    point.published_at.date()
                    if point.published_at is not None
                    else point.publication_date or point.asof_at.date()
                )
                <= requested_date
            ]
        if not points:
            raise fail(
                ErrorCode.NO_QUOTE,
                "No observation is available for this instrument and time boundary",
            )
        return PriceResponse(
            instrument=instrument,
            point=points[-1],
            mode=mode,
            requested_date=requested_date,
        )

    async def trend(
        self,
        slug: str,
        days: int,
        end: date | None = None,
        known_at: datetime | None = None,
    ) -> TrendResponse:
        if days < 1 or days > MarketLimits().history_days:
            raise fail(ErrorCode.INVALID_INPUT, "Trend days must be between 1 and 3650")
        instrument = await self.get_instrument(slug)
        last = end or datetime.now(UTC).date()
        first = last - timedelta(days=days - 1)
        asof = known_at or datetime.now(UTC)
        if asof.tzinfo is None:
            raise fail(
                ErrorCode.INVALID_INPUT, "Knowledge timestamp must include a timezone"
            )
        async with self.database.sessions() as session:
            points = await self.repositories(session).points(
                instrument.id, first, last, asof
            )
        coverage = PriceCoverage(
            count=len(points),
            first_date=points[0].observed_date if points else None,
            last_date=points[-1].observed_date if points else None,
            frequency=instrument.frequency,
            complete_daily=False,
            missing_calendar_days=days - len({point.observed_date for point in points}),
            notice="Only actual observations are returned; calendar gaps include non-trading days and unknown coverage; no forward filling",
        )
        change = points[-1].value - points[0].value if len(points) >= 2 else None
        percent = (
            change / points[0].value * Decimal(100) if change is not None else None
        )
        return TrendResponse(
            instrument=instrument,
            points=points,
            coverage=coverage,
            absolute_change=change,
            percentage_change=percent,
        )

    async def collect(self, payload: MarketRefreshPayload) -> MarketBatch:
        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            trust_env=True,
            follow_redirects=False,
        ) as client:
            return await MarketHttpAdapter(client).collect(
                payload.instrument, payload.start, payload.end
            )

    async def schedule_due(self, moment: datetime | None = None) -> None:
        current = moment or datetime.now(UTC)
        if current.tzinfo is None:
            raise fail(
                ErrorCode.INVALID_INPUT, "Schedule timestamp must include a timezone"
            )
        interval = (await self.tasks.runtime_settings()).source_interval_seconds
        bucket = int(current.timestamp()) // interval
        today = (
            datetime.fromtimestamp(bucket * interval, UTC)
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .date()
        )
        for instrument in (await self.instruments()).instruments:
            if not instrument.enabled:
                continue
            async with self.database.sessions() as session, session.begin():
                repository = self.repositories(session)
                row = await repository.instrument(instrument.slug, lock=True)
                if row is None:
                    continue
                active = repository.instrument_view(row)
                if not active.enabled:
                    continue
                key = f"market:{active.id}:{active.revision}:{interval}:{bucket}"
                if await repository.scheduled_payload(key) is not None:
                    continue
                start = (
                    today - timedelta(days=MarketLimits().refresh_lookback_days - 1)
                    if await repository.has_observations(active.id)
                    else MarketLimits().earliest_observation_date
                )
                await self.tasks.submit_in_session(
                    session,
                    TaskKind.COLLECT_PRICES,
                    MarketRefreshPayload(instrument=active, start=start, end=today),
                    key,
                    input_revision=str(active.revision),
                )

    async def store(
        self, session: AsyncSession, payload: MarketRefreshPayload, batch: MarketBatch
    ) -> MarketRefreshResult:
        repository = self.repositories(session)
        stored = 0
        for point in batch.points:
            stored += int(await repository.save_point(point))
        dates = [point.observed_date for point in batch.points]
        return MarketRefreshResult(
            instrument_id=payload.instrument.id,
            observed=len(batch.points),
            stored=stored,
            first_date=min(dates) if dates else None,
            last_date=max(dates) if dates else None,
        )


def register_handlers(
    registry: HandlerRegistry, database: Database, settings: Settings
) -> None:
    service = MarketService(database, settings, TaskService(database, settings))

    async def collect(
        context: TaskContext, payload: MarketRefreshPayload
    ) -> TaskOutcome[MarketRefreshResult]:
        batch = await context.step(
            StepKind.PRICE_COLLECT,
            MarketBatch,
            lambda: service.collect(payload),
            next_step=StepKind.PRICE_STORE,
        )
        result = await context.transaction_step(
            StepKind.PRICE_STORE,
            MarketRefreshResult,
            lambda session: service.store(session, payload, batch),
        )
        return TaskOutcome(result=result)

    registry.register(TaskKind.COLLECT_PRICES, MarketRefreshPayload, collect)
    registry.register_due_sources(service.schedule_due)


async def seed_defaults(database: Database, settings: Settings) -> None:
    from mining_server.infrastructure.market.seeds import defaults

    async with database.sessions() as session, session.begin():
        repository = MarketRepository(session)
        for instrument in defaults():
            if await repository.instrument(instrument.slug) is None:
                await repository.add_instrument(instrument)
