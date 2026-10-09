from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Enum, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mining_server.domain.auth import ConnectionStatus, RefreshState
from mining_server.infrastructure.database import Base


class ModelConnection(Base):
    __tablename__ = "model_connections"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    slot: Mapped[int] = mapped_column(Integer, unique=True, default=1)
    status: Mapped[ConnectionStatus] = mapped_column(
        Enum(ConnectionStatus, name="connection_status")
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    subject: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(500))
    client_id: Mapped[str | None] = mapped_column(String(300))
    encrypted_credentials: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    installation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    installation_id: Mapped[UUID | None] = mapped_column()
    token_generation: Mapped[int] = mapped_column(Integer, default=0)
    refresh_state: Mapped[RefreshState] = mapped_column(
        Enum(RefreshState, name="refresh_state"), default=RefreshState.NONE
    )
    refresh_owner: Mapped[UUID | None] = mapped_column()
    refresh_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(200))
    catalog_json: Mapped[str | None] = mapped_column(Text)
