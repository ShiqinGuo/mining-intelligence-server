from datetime import datetime
from uuid import UUID, uuid4

from mining_contracts.domain.documents import DocumentStatus
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from mining_server.infrastructure.database import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"), default=DocumentStatus.STORED
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentPage(Base):
    __tablename__ = "document_pages"

    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id"), primary_key=True
    )
    page_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_json: Mapped[str] = mapped_column(Text)


class ResourceExtraction(Base):
    __tablename__ = "resource_extractions"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("task_runs.id"), unique=True)
    document_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id"))
    request_json: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
