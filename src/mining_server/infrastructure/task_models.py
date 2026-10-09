from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from mining_server.domain.core import ErrorCode
from mining_server.domain.tasks import InvocationState, TaskKind, TaskState
from mining_server.infrastructure.database import Base


class TaskRun(Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        Index("ix_task_runs_recovery", "state", "lease_until", "retry_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[TaskKind] = mapped_column(Enum(TaskKind, name="task_kind"))
    state: Mapped[TaskState] = mapped_column(Enum(TaskState, name="task_state"))
    payload_json: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64))
    settings_json: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    workflow_version: Mapped[str] = mapped_column(String(100))
    input_revision: Mapped[str] = mapped_column(String(100))
    generation: Mapped[int] = mapped_column(default=0)
    owner: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint_seq: Mapped[int] = mapped_column(default=0)
    next_step: Mapped[str | None] = mapped_column(String(200))
    result_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[ErrorCode | None] = mapped_column(
        Enum(ErrorCode, name="error_code")
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TaskRetryBudget(Base):
    __tablename__ = "task_retry_budgets"

    task_id: Mapped[UUID] = mapped_column(ForeignKey("task_runs.id"), primary_key=True)
    failure_retry_count: Mapped[int] = mapped_column(default=0, server_default="0")


class StepRun(Base):
    __tablename__ = "step_runs"
    __table_args__ = (UniqueConstraint("task_id", "name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("task_runs.id"))
    name: Mapped[str] = mapped_column(String(200))
    result_json: Mapped[str] = mapped_column(Text)
    generation: Mapped[int]
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Checkpoint(Base):
    __tablename__ = "checkpoints"
    __table_args__ = (UniqueConstraint("task_id", "sequence"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("task_runs.id"))
    sequence: Mapped[int]
    workflow_version: Mapped[str] = mapped_column(String(100))
    input_revision: Mapped[str] = mapped_column(String(100))
    completed_step: Mapped[str] = mapped_column(String(200))
    next_step: Mapped[str | None] = mapped_column(String(200))
    context_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelInvocation(Base):
    __tablename__ = "model_invocations"
    __table_args__ = (UniqueConstraint("task_id", "name", "attempt"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("task_runs.id"))
    name: Mapped[str] = mapped_column(String(200))
    attempt: Mapped[int] = mapped_column(default=1)
    state: Mapped[InvocationState] = mapped_column(
        Enum(InvocationState, name="invocation_state")
    )
    generation: Mapped[int]
    response_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        Index(
            "ix_outbox_messages_pending", "published_at", "claimed_until", "created_at"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("task_runs.id"))
    generation: Mapped[int]
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[UUID | None]
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RuntimeConfig(Base):
    __tablename__ = "runtime_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int]
    config_json: Mapped[str] = mapped_column(Text)
