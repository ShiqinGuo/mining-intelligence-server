import hashlib
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from mining_contracts.domain.core import ErrorCode
from mining_contracts.domain.market import LookupMode, PriceAdapter, PricePoint
from mining_contracts.domain.news import (
    Article,
    CollectionResult,
    EntityKind,
    NewsEntity,
    NewsSearchRequest,
    SourceState,
    SummaryFact,
)
from mining_contracts.domain.tasks import StepKind, TaskKind, TaskState
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from mining_server.application.market import MarketService, register_handlers
from mining_server.application.news import NewsService, analyze_article
from mining_server.application.tasks import HandlerRegistry, TaskService
from mining_server.domain.core import DomainError
from mining_server.domain.market import (
    MarketBatch,
    MarketRefreshPayload,
    MarketRefreshRequest,
)
from mining_server.domain.model import ModelRequest, ModelResponse
from mining_server.domain.news import (
    AnalysisPayload,
    ArticleAnalysisDraft,
    ArticleBatch,
    ArticleContent,
    DiscoveredArticle,
    SourceCreate,
    SourceStateRequest,
)
from mining_server.domain.tasks import (
    RuntimeSettingsUpdate,
    RuntimeSettingsValues,
    TaskEnvelope,
)
from mining_server.infrastructure.auth.models import ModelConnection
from mining_server.infrastructure.database import Base, Database
from mining_server.infrastructure.documents.http import PublicHttpClient
from mining_server.infrastructure.market.repository import MarketRepository
from mining_server.infrastructure.market.seeds import defaults as instruments
from mining_server.infrastructure.news.models import (
    ArticleAnalysisRecord,
    ArticleRecord,
)
from mining_server.infrastructure.news.parsers import discover, parse_feed_article
from mining_server.infrastructure.news.repository import NewsRepository
from mining_server.infrastructure.news.seeds import defaults as sources
from mining_server.infrastructure.settings import Settings
from mining_server.infrastructure.task_models import OutboxMessage, StepRun, TaskRun


@pytest.fixture
async def services():
    url = os.getenv("MINING_NEWS_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "MINING_NEWS_TEST_DATABASE_URL is required for PostgreSQL integration tests"
        )
    database = Database(url)
    schema = f"news_market_{uuid4().hex}"
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
        admin_token="test-admin-token-long-enough",
        service_token="test-service-token-long-enough",
        master_key=Fernet.generate_key().decode(),
        oauth_host_id=f"urn:uuid:{uuid4()}",
        lease_seconds=60,
    )
    tasks = TaskService(database, settings)
    news = NewsService(database, settings, tasks, PublicHttpClient(1))
    market = MarketService(database, settings, tasks)
    try:
        yield news, market, tasks
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await database.close()


def enabled_source() -> SourceCreate:
    definition = sources()[0]
    return SourceCreate(
        name=definition.name,
        publisher=definition.publisher,
        kind=definition.kind,
        url=definition.url,
        rules=definition.rules,
        interval_minutes=definition.interval_minutes,
        max_items=definition.max_items,
        backfill_days=definition.backfill_days,
        state=SourceState.ENABLED,
    )


def article(source_id) -> ArticleContent:
    content = "PLS announced expansion of the Pilgangoora processing plant. Production guidance remains unchanged."
    return ArticleContent(
        url="https://www.pls.com/news/expansion",
        title="PLS expansion",
        publisher="PLS",
        source_id=source_id,
        project="Pilgangoora",
        published_at=datetime.now(UTC),
        fetched_at=datetime.now(UTC),
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )


async def test_collection_atomically_saves_article_cursor_checkpoint_and_analysis_outbox(
    services,
):
    news, _, tasks = services
    source = await news.create_source(enabled_source())
    submitted = await news.submit_collection(source.id, f"run:{uuid4()}")
    context = await tasks.claim(
        TaskEnvelope(task_id=submitted.id, generation=0), uuid4()
    )
    item = article(source.id)
    batch = ArticleBatch(discovered=1, articles=[item], fetched_at=datetime.now(UTC))
    result = await context.transaction_step(
        StepKind.SOURCE_COMMIT,
        CollectionResult,
        lambda session: news.commit_collection(session, source, batch, context.task_id),
    )
    assert result.stored == 1
    replay = await context.transaction_step(
        StepKind.SOURCE_COMMIT,
        type(result),
        lambda session: news.commit_collection(session, source, batch, context.task_id),
    )
    assert replay == result
    async with news.database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ArticleRecord)) == 1
        )
        assert await session.scalar(select(func.count()).select_from(StepRun)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TaskRun)
                .where(TaskRun.kind == TaskKind.NEWS_ANALYSIS)
            )
            == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(OutboxMessage)) == 2
        )
    assert (await news.get_source(source.id)).cursor == item.published_at
    assert len((await news.search(NewsSearchRequest(query="PLS"))).articles) == 1


async def test_repeated_feed_full_text_collection_keeps_one_immutable_article(services):
    news, _, tasks = services
    definition = next(
        item for item in sources() if item.name == "Mining.com.au PLS RSS"
    )
    source = await news.create_source(definition)
    raw = b'<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><title>PLS update</title><link>https://mining.com.au/pls-update/?utm_source=rss</link><pubDate>Wed, 07 Oct 2026 08:00:00 +0800</pubDate><content:encoded><![CDATA[<p>PLS announced a corporate funding update. This announcement contains no new mine resource estimate.</p>]]></content:encoded></item></channel></rss>'
    item = parse_feed_article(discover(raw, source)[0], source, datetime.now(UTC))
    batch = ArticleBatch(discovered=1, articles=[item], fetched_at=datetime.now(UTC))
    results: list[CollectionResult] = []
    for _ in range(2):
        submitted = await news.submit_collection(source.id, f"rss:{uuid4()}")
        context = await tasks.claim(
            TaskEnvelope(task_id=submitted.id, generation=0), uuid4()
        )
        results.append(
            await context.transaction_step(
                StepKind.SOURCE_COMMIT,
                CollectionResult,
                lambda session, context=context: news.commit_collection(
                    session, source, batch, context.task_id
                ),
            )
        )
    assert [result.stored for result in results] == [1, 0]
    assert (await news.get_source(source.id)).cursor == datetime(
        2026, 10, 7, tzinfo=UTC
    )
    async with news.database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ArticleRecord)) == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TaskRun)
                .where(TaskRun.kind == TaskKind.NEWS_ANALYSIS)
            )
            == 1
        )
        saved = await NewsRepository(session).article_revision(
            (await session.scalar(select(ArticleRecord))).id, 1
        )
    assert saved.content == item.content
    assert saved.content_hash == item.content_hash
    assert saved.published_at == item.published_at
    assert saved.project is None


async def test_source_revision_cas_and_disabled_source_reject_collection(services):
    news, _, _ = services
    source = await news.create_source(enabled_source())
    updated = await news.set_source_state(
        source.id, SourceStateRequest(state=SourceState.DISABLED, expected_revision=1)
    )
    assert updated.revision == 2
    with pytest.raises(DomainError) as caught:
        await news.set_source_state(
            source.id,
            SourceStateRequest(state=SourceState.ENABLED, expected_revision=1),
        )
    assert caught.value.code == ErrorCode.CONFLICT
    with pytest.raises(DomainError):
        await news.submit_collection(source.id, "disabled")


async def test_price_revisions_deduplicate_and_do_not_leak_future_knowledge(services):
    _, market, _ = services
    instrument = await market.create(instruments()[0])
    stamp = datetime(2026, 10, 8, 8, tzinfo=UTC)
    point = PricePoint(
        instrument_id=instrument.id,
        observed_date=date(2026, 9, 30),
        value=Decimal(120000),
        asof_at=stamp,
        fetched_at=stamp,
        source_url="https://www.mysteel.com/mmlc/",
        evidence="Observed public market history",
    )
    async with market.database.sessions() as session, session.begin():
        repository = MarketRepository(session)
        assert await repository.save_point(point)
        assert not await repository.save_point(
            PricePoint(
                instrument_id=point.instrument_id,
                observed_date=point.observed_date,
                published_at=point.published_at,
                publication_date=point.publication_date,
                asof_at=stamp + timedelta(hours=1),
                fetched_at=stamp + timedelta(hours=1),
                period_start=point.period_start,
                period_end=point.period_end,
                value=point.value,
                low=point.low,
                high=point.high,
                open=point.open,
                settlement=point.settlement,
                volume=point.volume,
                open_interest=point.open_interest,
                estimated=point.estimated,
                derived=point.derived,
                source_url=point.source_url,
                evidence=point.evidence,
            )
        )
        assert await repository.save_point(
            PricePoint(
                instrument_id=point.instrument_id,
                observed_date=point.observed_date,
                published_at=point.published_at,
                publication_date=point.publication_date,
                asof_at=stamp + timedelta(hours=2),
                fetched_at=point.fetched_at,
                period_start=point.period_start,
                period_end=point.period_end,
                value=Decimal(120250),
                low=point.low,
                high=point.high,
                open=point.open,
                settlement=point.settlement,
                volume=point.volume,
                open_interest=point.open_interest,
                estimated=point.estimated,
                derived=point.derived,
                source_url=point.source_url,
                evidence=point.evidence,
            )
        )
    early = await market.price(
        instrument.slug, date(2026, 9, 30), LookupMode.EXACT, stamp + timedelta(hours=1)
    )
    latest = await market.price(
        instrument.slug, date(2026, 9, 30), LookupMode.EXACT, stamp + timedelta(hours=3)
    )
    assert early.point.value == Decimal(120000)
    assert latest.point.value == Decimal(120250)
    with pytest.raises(DomainError):
        await market.price(
            instrument.slug,
            date(2026, 9, 30),
            LookupMode.EXACT,
            stamp - timedelta(hours=1),
        )
    trend = await market.trend(
        instrument.slug, 7, date(2026, 10, 6), stamp + timedelta(hours=3)
    )
    assert len(trend.points) == 1
    assert trend.absolute_change is None
    assert trend.coverage.missing_calendar_days == 6


async def test_registered_price_workflow_persists_quote_once(services, monkeypatch):
    _, market, tasks = services
    definition = next(
        item for item in instruments() if item.adapter == PriceAdapter.MYSTEEL_ARTICLE
    )
    instrument = await market.create(definition)
    observed = date(2026, 10, 8)
    point = PricePoint(
        instrument_id=instrument.id,
        observed_date=observed,
        value=Decimal(1695),
        low=Decimal(1670),
        high=Decimal(1720),
        derived=True,
        asof_at=datetime.now(UTC),
        fetched_at=datetime.now(UTC),
        source_url="https://xny.mysteel.com/a/26100820/151833A2AC7B0828.html",
        evidence="SC6 CIF China quoted range 1670-1720 USD/t",
    )
    calls: list[MarketRefreshPayload] = []

    async def collect(self, payload: MarketRefreshPayload) -> MarketBatch:
        calls.append(payload)
        return MarketBatch(points=[point])

    monkeypatch.setattr(MarketService, "collect", collect)
    registry = HandlerRegistry()
    register_handlers(registry, tasks.database, tasks.settings)
    task = await market.refresh(
        instrument.slug,
        MarketRefreshRequest(start=observed, end=observed),
        f"price-handler:{uuid4()}",
    )
    envelope = TaskEnvelope(task_id=task.id, generation=task.generation)
    await registry.execute(tasks, envelope)
    await registry.execute(tasks, envelope)
    assert (await tasks.get(task.id)).state == TaskState.SUCCEEDED
    assert len(calls) == 1
    result = await market.price(instrument.slug, observed, LookupMode.EXACT)
    assert result.point.value == point.value
    assert result.point.low == point.low
    assert result.point.high == point.high
    assert result.point.derived


class ScriptedModel:
    def __init__(self, evidence: str):
        self.evidence = evidence
        self.calls = 0
        self.requested_models: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requested_models.append(request.model)
        return ModelResponse(
            response_id="scripted-response",
            text=ArticleAnalysisDraft(
                relevant=True,
                entities=[
                    NewsEntity(
                        name="PLS", kind=EntityKind.ORGANIZATION, evidence=self.evidence
                    )
                ],
                summary=[
                    SummaryFact(
                        statement="PLS announced an expansion", evidence=self.evidence
                    )
                ],
            ).model_dump_json(),
            output=[],
        )


async def test_news_analysis_uses_immutable_revision_and_reuses_saved_model_response(
    services,
):
    news, _, tasks = services
    source = await news.create_source(enabled_source())
    async with news.database.sessions() as session, session.begin():
        saved, _ = await NewsRepository(session).store_article(
            article(source.id), datetime.now(UTC)
        )
        submitted = await news.submit_analysis(session, saved)
    context = await tasks.claim(
        TaskEnvelope(task_id=submitted.id, generation=0), uuid4()
    )
    payload = AnalysisPayload(
        article_id=saved.id,
        article_revision=1,
        content_hash=saved.content_hash,
        model="gpt-6.1-sol",
    )
    model = ScriptedModel(
        "PLS announced expansion of the Pilgangoora processing plant."
    )
    first = await analyze_article(context, payload, news, model)
    second = await analyze_article(context, payload, news, model)
    assert first == second
    assert model.calls == 1
    async with news.database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ArticleAnalysisRecord)
            )
            == 1
        )
    assert (await news.search(NewsSearchRequest(query="PLS"))).articles[
        0
    ].analysis.response_id == "scripted-response"


async def test_news_analysis_rejects_fabricated_evidence(services):
    news, _, tasks = services
    source = await news.create_source(enabled_source())
    async with news.database.sessions() as session, session.begin():
        saved, _ = await NewsRepository(session).store_article(
            article(source.id), datetime.now(UTC)
        )
        submitted = await news.submit_analysis(session, saved)
    context = await tasks.claim(
        TaskEnvelope(task_id=submitted.id, generation=0), uuid4()
    )
    payload = AnalysisPayload(
        article_id=saved.id,
        article_revision=1,
        content_hash=saved.content_hash,
        model="gpt-6.1-sol",
    )
    with pytest.raises(DomainError) as caught:
        await analyze_article(
            context, payload, news, ScriptedModel("PLS discovered ten billion tonnes.")
        )
    assert caught.value.code == ErrorCode.UPSTREAM_FAILURE
    async with news.database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ArticleAnalysisRecord)
            )
            == 0
        )


async def test_unconnected_analysis_waits_for_auth_without_undoing_article_collection(
    services,
):
    news, _, tasks = services
    source = await news.create_source(sources()[1])
    async with news.database.sessions() as session, session.begin():
        saved, _ = await NewsRepository(session).store_article(
            article(source.id), datetime.now(UTC)
        )
        submitted = await news.submit_analysis(session, saved)
    context = await tasks.claim(
        TaskEnvelope(task_id=submitted.id, generation=0), uuid4()
    )
    payload = AnalysisPayload(
        article_id=saved.id,
        article_revision=1,
        content_hash=saved.content_hash,
        model="gpt-6.1-sol",
    )
    with pytest.raises(DomainError) as caught:
        await analyze_article(context, payload, news)
    assert caught.value.code == ErrorCode.WAITING_AUTH
    await tasks.fail_task(context, caught.value)
    assert (await tasks.get(submitted.id)).state == TaskState.WAITING_AUTH
    async with news.database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ArticleRecord)) == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(ModelConnection)) == 0
        )


async def test_market_schedule_refreshes_across_configured_time_buckets(services):
    _, market, tasks = services
    instrument = await market.create(instruments()[0])
    await tasks.update_runtime_settings(
        RuntimeSettingsUpdate(
            expected_revision=1,
            settings=RuntimeSettingsValues(source_interval_seconds=1800),
        )
    )
    midnight_bucket = datetime(2026, 10, 7, 16, 10, tzinfo=UTC)
    await market.schedule_due(midnight_bucket)
    async with market.database.sessions() as session:
        first_task = await session.scalar(
            select(TaskRun).where(TaskRun.kind == TaskKind.COLLECT_PRICES)
        )
        first = MarketRefreshPayload.model_validate_json(first_task.payload_json)
        assert first.start == date(1900, 1, 1)
        assert first.end == date(2026, 10, 8)
    point = PricePoint(
        instrument_id=instrument.id,
        observed_date=date(2026, 10, 7),
        value=Decimal(120000),
        asof_at=midnight_bucket,
        fetched_at=midnight_bucket,
        source_url="https://www.mysteel.com/mmlc/",
        evidence="Existing observation for incremental scheduling",
    )
    async with market.database.sessions() as session, session.begin():
        await MarketRepository(session).save_point(point)
    await market.schedule_due(midnight_bucket + timedelta(minutes=10))
    await market.schedule_due(midnight_bucket + timedelta(minutes=20))
    async with market.database.sessions() as session:
        rows = (
            await session.scalars(
                select(TaskRun)
                .where(TaskRun.kind == TaskKind.COLLECT_PRICES)
                .order_by(TaskRun.created_at)
            )
        ).all()
        assert len(rows) == 2
        second = MarketRefreshPayload.model_validate_json(rows[1].payload_json)
        assert rows[0].idempotency_key != rows[1].idempotency_key
        assert second.start == date(2026, 10, 2)
        assert second.end == date(2026, 10, 8)


async def test_document_source_queues_pdf_and_news_tasks_with_cursor_atomically(
    services,
):
    news, _, tasks = services
    definition = next(
        source for source in sources() if source.name == "PLS ASX announcements"
    )
    source = await news.create_source(definition)
    submitted = await news.submit_collection(source.id, f"asx:{uuid4()}")
    context = await tasks.claim(
        TaskEnvelope(task_id=submitted.id, generation=0), uuid4()
    )
    published = datetime(2026, 9, 23, 1, 24, tzinfo=UTC)
    item = DiscoveredArticle(
        url="https://announcements.asx.com.au/asxpdf/20260923/pdf/074f015p42q95d.pdf",
        title="September quarter advisory",
        published_at=published,
    )
    batch = ArticleBatch(
        discovered=1, articles=[], documents=[item], fetched_at=datetime.now(UTC)
    )
    result = await context.transaction_step(
        StepKind.SOURCE_COMMIT,
        CollectionResult,
        lambda session: news.commit_collection(session, source, batch, context.task_id),
    )
    assert len(result.document_task_ids) == len(result.article_task_ids) == 1
    assert (await news.get_source(source.id)).cursor == published
    async with news.database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TaskRun)
                .where(TaskRun.kind == TaskKind.DOCUMENT_INGEST)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TaskRun)
                .where(TaskRun.kind == TaskKind.FETCH_ARTICLE)
            )
            == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(OutboxMessage)) == 3
        )
    updated = await news.get_source(source.id)
    repeated = await news.submit_collection(source.id, f"asx-repeat:{uuid4()}")
    repeated_context = await tasks.claim(
        TaskEnvelope(task_id=repeated.id, generation=0), uuid4()
    )
    repeated_result = await repeated_context.transaction_step(
        StepKind.SOURCE_COMMIT,
        CollectionResult,
        lambda session: news.commit_collection(
            session, updated, batch, repeated_context.task_id
        ),
    )
    assert repeated_result.document_task_ids == result.document_task_ids
    assert repeated_result.article_task_ids == result.article_task_ids


async def test_news_model_selection_uses_submission_snapshot_after_http_configuration_change(
    services,
):
    news, _, tasks = services
    await tasks.update_runtime_settings(
        RuntimeSettingsUpdate(
            expected_revision=1,
            settings=RuntimeSettingsValues(model_name="configured-model"),
        )
    )
    source = await news.create_source(sources()[0])
    async with news.database.sessions() as session, session.begin():
        saved, _ = await NewsRepository(session).store_article(
            article(source.id), datetime.now(UTC)
        )
        submitted = await news.submit_analysis(session, saved)
    await tasks.update_runtime_settings(
        RuntimeSettingsUpdate(
            expected_revision=2,
            settings=RuntimeSettingsValues(model_name="later-model"),
        )
    )
    context = await tasks.claim(
        TaskEnvelope(task_id=submitted.id, generation=0), uuid4()
    )
    payload = AnalysisPayload.model_validate_json(context.payload_json)
    assert payload.model == "configured-model"
    assert context.runtime_settings.model_name == "configured-model"
    model = ScriptedModel(
        "PLS announced expansion of the Pilgangoora processing plant."
    )
    outcome = await analyze_article(context, payload, news, model)
    assert model.requested_models == ["configured-model"]
    assert outcome.result.model == "configured-model"


async def test_search_bounds_large_article_excerpt_and_fetch_preserves_full_content(
    services,
):
    news, _, _ = services
    content = "PLS securities announcement. " + "Security record. " * 75000
    base = article(None)
    original = ArticleContent(
        url=base.url,
        title=base.title,
        published_at=base.published_at,
        summary=base.summary,
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        fetched_at=base.fetched_at,
        publisher=base.publisher,
        source_id=base.source_id,
        project=base.project,
    )
    async with news.database.sessions() as session, session.begin():
        saved, _ = await NewsRepository(session).store_article(
            original, datetime.now(UTC)
        )
    result = await news.search(NewsSearchRequest(query="PLS"))
    summary = result.articles[0]
    assert summary.id == saved.id
    assert summary.content_hash == saved.content_hash
    assert summary.revision == saved.revision
    assert summary.excerpt == content[:1200]
    assert summary.truncated is True
    assert len(result.model_dump_json()) < 4000
    fetched = await news.fetch(str(saved.url), f"fetch:{uuid4()}")
    assert isinstance(fetched, Article)
    assert fetched.content == content
    assert fetched.content_hash == saved.content_hash
