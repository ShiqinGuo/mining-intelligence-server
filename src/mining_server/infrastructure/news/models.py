from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mining_server.domain.news import SourceState
from mining_server.infrastructure.database import Base


class SourceRecord(Base):
    __tablename__ = "sources"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    revision: Mapped[int] = mapped_column(Integer)
    state: Mapped[SourceState] = mapped_column(
        Enum(SourceState, native_enum=False), index=True
    )
    payload: Mapped[str] = mapped_column(Text)
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class SourceRevisionRecord(Base):
    __tablename__ = "source_revisions"
    __table_args__ = (UniqueConstraint("source_id", "revision"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)


class ArticleRecord(Base):
    __tablename__ = "articles"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id"), index=True)
    project: Mapped[str | None] = mapped_column(String(200), index=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)


class ArticleRevisionRecord(Base):
    __tablename__ = "article_revisions"
    __table_args__ = (UniqueConstraint("article_id", "content_hash"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    article_id: Mapped[UUID] = mapped_column(ForeignKey("articles.id"))
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)


class CollectionRunRecord(Base):
    __tablename__ = "collection_runs"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), index=True)
    task_id: Mapped[UUID] = mapped_column(index=True, unique=True)
    revision: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(Text)


class ArticleAnalysisRecord(Base):
    __tablename__ = "article_analyses"
    __table_args__ = (
        UniqueConstraint("article_id", "article_revision", "model", "prompt_version"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    article_id: Mapped[UUID] = mapped_column(ForeignKey("articles.id"), index=True)
    article_revision: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(100))
    payload: Mapped[str] = mapped_column(Text)
