from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mining_server.infrastructure.database import Base


class InstrumentRecord(Base):
    __tablename__ = "price_instruments"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(160), unique=True)
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)


class ObservationRecord(Base):
    __tablename__ = "price_observations"
    __table_args__ = (
        UniqueConstraint("instrument_id", "observed_date", "evidence_hash"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("price_instruments.id"), index=True
    )
    observed_date: Mapped[date] = mapped_column(Date, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    asof_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    value: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
