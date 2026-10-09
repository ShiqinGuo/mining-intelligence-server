import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import import_module
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from mining_contracts.domain.documents import (
    DocumentResponse,
    DocumentStatus,
    ExtractionRequest,
)
from mining_contracts.domain.market import PriceInstrument
from mining_contracts.domain.tasks import StepKey, StepKind, TaskKind, TaskState
from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from mining_server.application.tasks import TaskService
from mining_server.domain.documents import (
    DocumentIngestPayload,
    DocumentSearchResult,
    ExtractionPayload,
    ParsedPage,
)
from mining_server.domain.market import MarketRefreshPayload
from mining_server.domain.model import (
    ModelFunctionCall,
    ModelMessage,
    ModelOutputText,
    ModelResponse,
    ModelRole,
    ModelToolCall,
)
from mining_server.domain.news import (
    AnalysisPayload,
    ArticleFetchPayload,
    CollectionPayload,
    Source,
)
from mining_server.domain.task_payloads import TaskPayload, parse_task_payload
from mining_server.domain.tasks import InvocationState, RuntimeSettingsValues
from mining_server.infrastructure.database import Base, Database
from mining_server.infrastructure.market.seeds import defaults as instruments
from mining_server.infrastructure.news.seeds import defaults as sources
from mining_server.infrastructure.settings import Settings
from mining_server.infrastructure.task_models import (
    Checkpoint,
    ModelInvocation,
    StepRun,
    TaskRun,
)

migration = import_module(
    "migrations.versions.20261008_136e253238b6_typed_task_payloads"
)


class Envelope[T: BaseModel](BaseModel):
    value: T


@dataclass(frozen=True)
class Case:
    kind: TaskKind
    payload: TaskPayload


@dataclass
class MigrationDatabase:
    database: Database
    service: TaskService
    schema: str


@pytest.fixture
async def migration_database():
    if "MINING_TYPED_MIGRATION_TEST_DATABASE_URL" not in os.environ:
        pytest.skip("Dedicated mining_typed_migration_test PostgreSQL URL is required")
    url = os.environ["MINING_TYPED_MIGRATION_TEST_DATABASE_URL"]
    if not url.endswith("/mining_typed_migration_test"):
        raise ValueError("Migration tests require the dedicated database")
    database = Database(url)
    schema = "typed_migration_" + uuid4().hex
    async with database.engine.begin() as connection:
        await connection.execute(CreateSchema(schema))
        await connection.execution_options(schema_translate_map={None: schema})
        await connection.run_sync(Base.metadata.create_all)
    database.sessions = async_sessionmaker(
        database.engine.execution_options(schema_translate_map={None: schema}),
        expire_on_commit=False,
    )
    settings = Settings(
        database_url=url,
        admin_token="migration-admin-token-long-enough",
        service_token="migration-service-token-long-enough",
        master_key="MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE=",
        oauth_host_id="urn:uuid:" + str(uuid4()),
    )
    try:
        yield MigrationDatabase(database, TaskService(database, settings), schema)
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await database.close()


def cases(document_id: UUID) -> list[Case]:
    seed = sources()[0]
    source = Source(
        id=uuid4(),
        revision=1,
        next_due_at=datetime.now(UTC),
        name=seed.name + " caf\u00e9",
        publisher=seed.publisher,
        kind=seed.kind,
        url=seed.url,
        rules=seed.rules,
        interval_minutes=seed.interval_minutes,
        max_items=seed.max_items,
        backfill_days=seed.backfill_days,
        state=seed.state,
    )
    seed_instrument = instruments()[0]
    instrument = PriceInstrument(
        id=uuid4(),
        revision=1,
        slug=seed_instrument.slug,
        name=seed_instrument.name,
        commodity=seed_instrument.commodity,
        grade=seed_instrument.grade,
        purity_basis=seed_instrument.purity_basis,
        purity_min=seed_instrument.purity_min,
        hydrate_form=seed_instrument.hydrate_form,
        region=seed_instrument.region,
        origin=seed_instrument.origin,
        issuer=seed_instrument.issuer,
        project=seed_instrument.project,
        price_kind=seed_instrument.price_kind,
        frequency=seed_instrument.frequency,
        currency=seed_instrument.currency,
        unit=seed_instrument.unit,
        tax_basis=seed_instrument.tax_basis,
        delivery_basis=seed_instrument.delivery_basis,
        session=seed_instrument.session,
        adapter=seed_instrument.adapter,
        source_symbol=seed_instrument.source_symbol,
        source_url=seed_instrument.source_url,
        methodology_url=seed_instrument.methodology_url,
        methodology_version=seed_instrument.methodology_version,
        usage_notice=seed_instrument.usage_notice,
        enabled=seed_instrument.enabled,
    )
    return [
        Case(TaskKind.DOCUMENT_INGEST, DocumentIngestPayload(document_id=document_id)),
        Case(
            TaskKind.RESOURCE_EXTRACTION,
            ExtractionPayload(
                extraction_id=uuid4(),
                request=ExtractionRequest(document_id=document_id),
            ),
        ),
        Case(
            TaskKind.FETCH_ARTICLE,
            ArticleFetchPayload(url="https://example.com/report", source=source),
        ),
        Case(TaskKind.COLLECT_SOURCE, CollectionPayload(source=source)),
        Case(
            TaskKind.COLLECT_PRICES,
            MarketRefreshPayload(
                instrument=instrument, start=date(2026, 10, 1), end=date(2026, 10, 8)
            ),
        ),
        Case(
            TaskKind.NEWS_ANALYSIS,
            AnalysisPayload(
                article_id=uuid4(),
                article_revision=1,
                content_hash="hash",
                model="gpt-6.1-sol",
            ),
        ),
    ]


def apply_upgrade(connection: Connection) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()


def apply_downgrade(connection: Connection) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()


async def migrate(database: MigrationDatabase, upgrade: bool) -> None:
    async with database.database.engine.begin() as connection:
        await connection.execution_options(schema_translate_map={None: database.schema})
        await connection.run_sync(apply_upgrade if upgrade else apply_downgrade)


async def test_legacy_payloads_results_model_outputs_upgrade_and_round_trip(
    migration_database,
):
    environment = migration_database
    document_id = uuid4()
    result = DocumentResponse(
        id=document_id,
        sha256="a" * 64,
        size_bytes=100,
        page_count=1,
        status=DocumentStatus.READY,
        created_at=datetime.now(UTC),
    )
    page_result = DocumentSearchResult(
        pages=[
            ParsedPage(
                page_number=1, text="JORC Indicated 100 Mt", width=600, height=800
            )
        ],
        truncated=False,
    )
    call = ModelFunctionCall(
        call_id="call-one", name="read_pages", arguments='{"pages":[1]}'
    )
    response = ModelResponse(
        response_id="response-one",
        text="",
        tool_calls=[
            ModelToolCall(
                call_id=call.call_id, name=call.name, arguments=call.arguments
            )
        ],
        output=[
            ModelMessage(
                role=ModelRole.ASSISTANT,
                content=[ModelOutputText(text="Reading evidence")],
            ),
            call,
        ],
    )
    legacy_response = migration.LegacyModelResponse(
        response_id=response.response_id,
        text=response.text,
        tool_calls=response.tool_calls,
        output=[migration.LegacyOutput(value=item) for item in response.output],
    ).model_dump_json()
    task_ids: list[UUID] = []
    owner = uuid4()
    prepared = cases(document_id)
    async with environment.database.sessions() as session, session.begin():
        for case in prepared:
            encoded = json.dumps(
                case.payload.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            identifier = uuid4()
            task_ids.append(identifier)
            session.add(
                TaskRun(
                    id=identifier,
                    kind=case.kind,
                    state=TaskState.UNKNOWN,
                    payload_json=encoded,
                    payload_hash=hashlib.sha256(
                        f"{case.kind}:1:1:{encoded}".encode()
                    ).hexdigest(),
                    settings_json=RuntimeSettingsValues().model_dump_json(),
                    idempotency_key="migration:" + str(identifier),
                    workflow_version="1",
                    input_revision="1",
                    generation=7,
                    owner=owner,
                    checkpoint_seq=2,
                    result_json=Envelope(value=result).model_dump_json()
                    if case.kind == TaskKind.DOCUMENT_INGEST
                    else None,
                )
            )
        await session.flush()
        tool_key = StepKey(kind=StepKind.DOCUMENT_TOOL).storage_key()
        model_key = StepKey(kind=StepKind.MODEL_DECISION).storage_key()
        session.add(
            StepRun(
                task_id=task_ids[0],
                name=tool_key,
                generation=7,
                result_json=Envelope(
                    value=Envelope(value=page_result)
                ).model_dump_json(),
            )
        )
        session.add(
            Checkpoint(
                task_id=task_ids[0],
                sequence=1,
                workflow_version="1",
                input_revision="1",
                completed_step=tool_key,
                context_json=Envelope(
                    value=Envelope(value=page_result)
                ).model_dump_json(),
            )
        )
        session.add(
            StepRun(
                task_id=task_ids[0],
                name=model_key,
                generation=7,
                result_json=Envelope(
                    value=migration.LegacyModelResponse.model_validate_json(
                        legacy_response
                    )
                ).model_dump_json(),
            )
        )
        session.add(
            Checkpoint(
                task_id=task_ids[0],
                sequence=2,
                workflow_version="1",
                input_revision="1",
                completed_step=model_key,
                context_json=Envelope(
                    value=migration.LegacyModelResponse.model_validate_json(
                        legacy_response
                    )
                ).model_dump_json(),
            )
        )
        session.add(
            ModelInvocation(
                task_id=task_ids[0],
                name=model_key,
                attempt=1,
                generation=7,
                state=InvocationState.COMPLETE,
                response_json=legacy_response,
            )
        )
        session.add(
            ModelInvocation(
                task_id=task_ids[0],
                name=StepKey(kind=StepKind.MODEL_DECISION, round=1).storage_key(),
                attempt=1,
                generation=7,
                state=InvocationState.UNKNOWN,
                response_json=None,
            )
        )
    await migrate(environment, upgrade=True)
    for identifier, case in zip(task_ids, prepared, strict=True):
        reused = await environment.service.submit(
            case.kind, case.payload, "migration:" + str(identifier)
        )
        assert reused.id == identifier
        assert reused.state == TaskState.UNKNOWN
        assert reused.generation == 7
        async with environment.database.sessions() as session:
            stored = await session.get(TaskRun, identifier)
            assert stored.owner == owner
            assert stored.checkpoint_seq == 2
            assert parse_task_payload(stored.kind, stored.payload_json) == case.payload
    assert (await environment.service.get(task_ids[0])).result == result
    async with environment.database.sessions() as session:
        steps = (
            await session.scalars(select(StepRun).where(StepRun.task_id == task_ids[0]))
        ).all()
        for step in steps:
            match StepKey.from_storage_key(step.name).kind:
                case StepKind.DOCUMENT_TOOL:
                    assert (
                        DocumentSearchResult.model_validate_json(step.result_json)
                        == page_result
                    )
                case StepKind.MODEL_DECISION:
                    assert (
                        ModelResponse.model_validate_json(step.result_json) == response
                    )
        invocations = (
            await session.scalars(
                select(ModelInvocation).where(ModelInvocation.task_id == task_ids[0])
            )
        ).all()
        assert any(
            item.state == InvocationState.UNKNOWN and item.response_json is None
            for item in invocations
        )
        assert any(
            item.state == InvocationState.COMPLETE
            and ModelResponse.model_validate_json(item.response_json) == response
            for item in invocations
        )
    await migrate(environment, upgrade=False)
    async with environment.database.sessions() as session:
        task = await session.get(TaskRun, task_ids[0])
        assert (
            Envelope[DocumentResponse].model_validate_json(task.result_json).value
            == result
        )
        tool_step = await session.scalar(
            select(StepRun).where(
                StepRun.task_id == task_ids[0], StepRun.name == tool_key
            )
        )
        assert (
            Envelope[Envelope[DocumentSearchResult]]
            .model_validate_json(tool_step.result_json)
            .value.value
            == page_result
        )
        invocation = await session.scalar(
            select(ModelInvocation).where(
                ModelInvocation.task_id == task_ids[0],
                ModelInvocation.name == model_key,
            )
        )
        legacy = migration.LegacyModelResponse.model_validate_json(
            invocation.response_json
        )
        assert [item.value for item in legacy.output] == response.output
    await migrate(environment, upgrade=True)
    assert (await environment.service.get(task_ids[0])).result == result


async def test_invalid_payload_aborts_all_backfill_changes(migration_database):
    environment = migration_database
    identifier = uuid4()
    result = DocumentResponse(
        id=uuid4(),
        sha256="b" * 64,
        size_bytes=1,
        status=DocumentStatus.STORED,
        created_at=datetime.now(UTC),
    )
    legacy = Envelope(value=result).model_dump_json()
    async with environment.database.sessions() as session, session.begin():
        session.add(
            TaskRun(
                id=identifier,
                kind=TaskKind.DOCUMENT_INGEST,
                state=TaskState.UNKNOWN,
                payload_json='{"document_id":null,"pdf_url":null}',
                payload_hash="c" * 64,
                settings_json=RuntimeSettingsValues().model_dump_json(),
                idempotency_key="invalid:" + str(identifier),
                workflow_version="1",
                input_revision="1",
                generation=7,
                result_json=legacy,
            )
        )
    with pytest.raises(ValidationError):
        await migrate(environment, upgrade=True)
    async with environment.database.sessions() as session:
        task = await session.get(TaskRun, identifier)
        assert task.result_json == legacy
        assert task.payload_hash == "c" * 64
        assert task.state == TaskState.UNKNOWN
        assert task.generation == 7
