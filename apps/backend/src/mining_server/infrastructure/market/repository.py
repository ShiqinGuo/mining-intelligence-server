import hashlib
from datetime import date, datetime
from uuid import UUID, uuid4

from mining_contracts.domain.market import InstrumentCreate, PriceInstrument, PricePoint
from mining_contracts.domain.tasks import TaskKind
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mining_server.domain.market import MarketRefreshPayload
from mining_server.infrastructure.market.models import (
    InstrumentRecord,
    ObservationRecord,
)
from mining_server.infrastructure.task_models import TaskRun


class MarketRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def instrument_view(self, row: InstrumentRecord) -> PriceInstrument:
        value = InstrumentCreate.model_validate_json(row.payload)
        return PriceInstrument(
            slug=value.slug,
            name=value.name,
            commodity=value.commodity,
            grade=value.grade,
            purity_basis=value.purity_basis,
            purity_min=value.purity_min,
            hydrate_form=value.hydrate_form,
            region=value.region,
            origin=value.origin,
            issuer=value.issuer,
            project=value.project,
            price_kind=value.price_kind,
            frequency=value.frequency,
            currency=value.currency,
            unit=value.unit,
            tax_basis=value.tax_basis,
            delivery_basis=value.delivery_basis,
            session=value.session,
            adapter=value.adapter,
            source_symbol=value.source_symbol,
            source_url=value.source_url,
            methodology_url=value.methodology_url,
            methodology_version=value.methodology_version,
            usage_notice=value.usage_notice,
            enabled=value.enabled,
            id=row.id,
            revision=row.revision,
        )

    async def instruments(self) -> list[PriceInstrument]:
        rows = (
            await self.session.scalars(
                select(InstrumentRecord).order_by(InstrumentRecord.slug)
            )
        ).all()
        return [self.instrument_view(row) for row in rows]

    async def instrument(
        self, slug: str, lock: bool = False
    ) -> InstrumentRecord | None:
        statement = select(InstrumentRecord).where(InstrumentRecord.slug == slug)
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def add_instrument(self, value: InstrumentCreate) -> PriceInstrument:
        row = InstrumentRecord(
            id=uuid4(), slug=value.slug, revision=1, payload=value.model_dump_json()
        )
        self.session.add(row)
        await self.session.flush()
        return self.instrument_view(row)

    async def save_point(self, point: PricePoint) -> bool:
        identity = point.model_dump_json(exclude={"asof_at", "fetched_at"})
        evidence_hash = hashlib.sha256(identity.encode()).hexdigest()
        statement = (
            insert(ObservationRecord)
            .values(
                id=uuid4(),
                instrument_id=point.instrument_id,
                observed_date=point.observed_date,
                published_at=point.published_at,
                asof_at=point.asof_at,
                value=point.value,
                evidence_hash=evidence_hash,
                payload=point.model_dump_json(),
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ObservationRecord.instrument_id,
                    ObservationRecord.observed_date,
                    ObservationRecord.evidence_hash,
                ]
            )
            .returning(ObservationRecord.id)
        )
        return await self.session.scalar(statement) is not None

    async def has_observations(self, instrument_id: UUID) -> bool:
        return (
            await self.session.scalar(
                select(ObservationRecord.id)
                .where(ObservationRecord.instrument_id == instrument_id)
                .limit(1)
            )
            is not None
        )

    async def scheduled_payload(
        self, idempotency_key: str
    ) -> MarketRefreshPayload | None:
        raw = await self.session.scalar(
            select(TaskRun.payload_json).where(
                TaskRun.kind == TaskKind.COLLECT_PRICES,
                TaskRun.idempotency_key == idempotency_key,
            )
        )
        return (
            MarketRefreshPayload.model_validate_json(raw) if raw is not None else None
        )

    async def points(
        self, instrument_id: UUID, start: date, end: date, known_at: datetime
    ) -> list[PricePoint]:
        rows = (
            await self.session.scalars(
                select(ObservationRecord)
                .where(
                    ObservationRecord.instrument_id == instrument_id,
                    ObservationRecord.observed_date >= start,
                    ObservationRecord.observed_date <= end,
                    ObservationRecord.asof_at <= known_at,
                )
                .order_by(
                    ObservationRecord.observed_date.asc(),
                    ObservationRecord.asof_at.desc(),
                )
            )
        ).all()
        seen = set()
        output = []
        for row in rows:
            if row.observed_date in seen:
                continue
            seen.add(row.observed_date)
            output.append(PricePoint.model_validate_json(row.payload))
        return output
