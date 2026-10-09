import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from alembic import op
from pydantic import BaseModel, ConfigDict, TypeAdapter
from sqlalchemy import Text, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from mining_server.domain.model import ModelOutput, ModelResponse, ModelToolCall
from mining_server.domain.task_payloads import parse_task_payload
from mining_server.domain.tasks import StepKind
from mining_server.infrastructure.task_models import (
    Checkpoint,
    ModelInvocation,
    StepRun,
    TaskRun,
)
from mining_server.infrastructure.task_repository import TaskRepository

revision: str = "136e253238b6"
down_revision: str | None = "8ab58a5681b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


@dataclass(frozen=True)
class BackfillLimits:
    batch_rows: int = 100


class LegacyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: ModelOutput


class LegacyModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_id: str
    text: str = ""
    tool_calls: list[ModelToolCall]
    output: list[LegacyOutput]
    input_tokens: int | None = None
    output_tokens: int | None = None


def model_response(encoded: str) -> ModelResponse:
    parsed = TypeAdapter(ModelResponse | LegacyModelResponse).validate_json(encoded)
    match parsed:
        case ModelResponse():
            return parsed
        case LegacyModelResponse():
            return ModelResponse(
                response_id=parsed.response_id,
                text=parsed.text,
                tool_calls=parsed.tool_calls,
                output=[item.value for item in parsed.output],
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
            )


def legacy_model_response(encoded: str) -> str:
    parsed = ModelResponse.model_validate_json(encoded)
    return LegacyModelResponse(
        response_id=parsed.response_id,
        text=parsed.text,
        tool_calls=parsed.tool_calls,
        output=[LegacyOutput(value=item) for item in parsed.output],
        input_tokens=parsed.input_tokens,
        output_tokens=parsed.output_tokens,
    ).model_dump_json()


def payloads(session: Session, legacy: bool) -> None:
    for task in session.scalars(
        select(TaskRun).execution_options(yield_per=BackfillLimits().batch_rows)
    ):
        parsed = parse_task_payload(task.kind, task.payload_json)
        encoded = (
            json.dumps(
                parsed.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            )
            if legacy
            else parsed.model_dump_json()
        )
        task.payload_json = encoded
        task.payload_hash = hashlib.sha256(
            f"{task.kind}:{task.workflow_version}:{task.input_revision}:{encoded}".encode()
        ).hexdigest()
        result = TaskRepository.result(task)
        if result is not None:
            task.result_json = result.model_dump_json()
        session.flush()


def responses(session: Session, legacy: bool) -> None:
    for invocation in session.scalars(
        select(ModelInvocation)
        .where(ModelInvocation.response_json.is_not(None))
        .execution_options(yield_per=BackfillLimits().batch_rows)
    ):
        invocation.response_json = (
            legacy_model_response(invocation.response_json)
            if legacy
            else model_response(invocation.response_json).model_dump_json()
        )
        session.flush()
    for step in session.scalars(
        select(StepRun)
        .where(
            or_(
                StepRun.name.startswith(
                    StepKind.MODEL_DECISION.value + ":", autoescape=True
                ),
                StepRun.name.startswith(
                    StepKind.NEWS_ANALYSIS.value + ":", autoescape=True
                ),
            )
        )
        .execution_options(yield_per=BackfillLimits().batch_rows)
    ):
        step.result_json = (
            legacy_model_response(step.result_json)
            if legacy
            else model_response(step.result_json).model_dump_json()
        )
        session.flush()
    for checkpoint in session.scalars(
        select(Checkpoint)
        .where(
            or_(
                Checkpoint.completed_step.startswith(
                    StepKind.MODEL_DECISION.value + ":", autoescape=True
                ),
                Checkpoint.completed_step.startswith(
                    StepKind.NEWS_ANALYSIS.value + ":", autoescape=True
                ),
            )
        )
        .execution_options(yield_per=BackfillLimits().batch_rows)
    ):
        checkpoint.context_json = (
            legacy_model_response(checkpoint.context_json)
            if legacy
            else model_response(checkpoint.context_json).model_dump_json()
        )
        session.flush()
    session.flush()


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        update(StepRun)
        .where(func.jsonb_exists(cast(StepRun.result_json, JSONB), "value"))
        .values(result_json=cast(cast(StepRun.result_json, JSONB)["value"], Text))
    )
    connection.execute(
        update(Checkpoint)
        .where(func.jsonb_exists(cast(Checkpoint.context_json, JSONB), "value"))
        .values(context_json=cast(cast(Checkpoint.context_json, JSONB)["value"], Text))
    )
    connection.execute(
        update(TaskRun)
        .where(
            TaskRun.result_json.is_not(None),
            func.jsonb_exists(cast(TaskRun.result_json, JSONB), "value"),
        )
        .values(result_json=cast(cast(TaskRun.result_json, JSONB)["value"], Text))
    )
    connection.execute(
        update(StepRun)
        .where(
            StepRun.name.startswith(
                StepKind.DOCUMENT_TOOL.value + ":", autoescape=True
            ),
            func.jsonb_exists(cast(StepRun.result_json, JSONB), "value"),
        )
        .values(result_json=cast(cast(StepRun.result_json, JSONB)["value"], Text))
    )
    connection.execute(
        update(Checkpoint)
        .where(
            Checkpoint.completed_step.startswith(
                StepKind.DOCUMENT_TOOL.value + ":", autoescape=True
            ),
            func.jsonb_exists(cast(Checkpoint.context_json, JSONB), "value"),
        )
        .values(context_json=cast(cast(Checkpoint.context_json, JSONB)["value"], Text))
    )
    with Session(connection) as session:
        payloads(session, legacy=False)
        responses(session, legacy=False)


def downgrade() -> None:
    connection = op.get_bind()
    with Session(connection) as session:
        payloads(session, legacy=True)
        responses(session, legacy=True)
    connection.execute(
        update(StepRun)
        .where(
            StepRun.name.startswith(StepKind.DOCUMENT_TOOL.value + ":", autoescape=True)
        )
        .values(
            result_json=cast(
                func.jsonb_build_object("value", cast(StepRun.result_json, JSONB)), Text
            )
        )
    )
    connection.execute(
        update(Checkpoint)
        .where(
            Checkpoint.completed_step.startswith(
                StepKind.DOCUMENT_TOOL.value + ":", autoescape=True
            )
        )
        .values(
            context_json=cast(
                func.jsonb_build_object("value", cast(Checkpoint.context_json, JSONB)),
                Text,
            )
        )
    )
    connection.execute(
        update(StepRun).values(
            result_json=cast(
                func.jsonb_build_object("value", cast(StepRun.result_json, JSONB)), Text
            )
        )
    )
    connection.execute(
        update(Checkpoint).values(
            context_json=cast(
                func.jsonb_build_object("value", cast(Checkpoint.context_json, JSONB)),
                Text,
            )
        )
    )
    connection.execute(
        update(TaskRun)
        .where(TaskRun.result_json.is_not(None))
        .values(
            result_json=cast(
                func.jsonb_build_object("value", cast(TaskRun.result_json, JSONB)), Text
            )
        )
    )
