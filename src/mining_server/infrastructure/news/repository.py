from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mining_server.domain.news import (
    Article,
    ArticleAnalysis,
    ArticleContent,
    CollectionResult,
    CollectionRun,
    NewsLimits,
    Source,
    SourceCreate,
)
from mining_server.domain.task_views import TaskView
from mining_server.infrastructure.news.models import (
    ArticleAnalysisRecord,
    ArticleRecord,
    ArticleRevisionRecord,
    CollectionRunRecord,
    SourceRecord,
    SourceRevisionRecord,
)
from mining_server.infrastructure.task_models import TaskRun
from mining_server.infrastructure.task_repository import TaskRepository


class NewsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def source_view(self, row: SourceRecord) -> Source:
        definition = SourceCreate.model_validate_json(row.payload)
        return Source(
            name=definition.name,
            publisher=definition.publisher,
            kind=definition.kind,
            url=definition.url,
            rules=definition.rules,
            interval_minutes=definition.interval_minutes,
            max_items=definition.max_items,
            backfill_days=definition.backfill_days,
            state=definition.state,
            id=row.id,
            revision=row.revision,
            last_success_at=row.last_success_at,
            next_due_at=row.next_due_at,
            cursor=row.cursor,
            last_error=row.last_error,
        )

    async def sources(self) -> list[Source]:
        rows = (
            await self.session.scalars(select(SourceRecord).order_by(SourceRecord.name))
        ).all()
        return [self.source_view(row) for row in rows]

    async def source(self, source_id: UUID, lock: bool = False) -> SourceRecord | None:
        statement = select(SourceRecord).where(SourceRecord.id == source_id)
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def add_source(self, value: SourceCreate, now: datetime) -> Source:
        row = SourceRecord(
            id=uuid4(),
            name=value.name,
            revision=1,
            state=value.state,
            payload=value.model_dump_json(),
            next_due_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        self.session.add(
            SourceRevisionRecord(source_id=row.id, revision=1, payload=row.payload)
        )
        return self.source_view(row)

    async def save_source(self, row: SourceRecord, definition: SourceCreate) -> Source:
        row.revision += 1
        row.name = definition.name
        row.state = definition.state
        row.payload = definition.model_dump_json()
        self.session.add(
            SourceRevisionRecord(
                source_id=row.id, revision=row.revision, payload=row.payload
            )
        )
        await self.session.flush()
        return self.source_view(row)

    async def by_url(self, url: str) -> Article | None:
        row = await self.session.scalar(
            select(ArticleRecord).where(ArticleRecord.url == url)
        )
        return Article.model_validate_json(row.payload) if row else None

    async def article_revision(self, article_id: UUID, revision: int) -> Article | None:
        row = await self.session.scalar(
            select(ArticleRevisionRecord).where(
                ArticleRevisionRecord.article_id == article_id,
                ArticleRevisionRecord.revision == revision,
            )
        )
        return Article.model_validate_json(row.payload) if row else None

    async def existing_task(self, key: str) -> TaskView | None:
        row = await self.session.scalar(
            select(TaskRun).where(TaskRun.idempotency_key == key)
        )
        return await TaskRepository(self.session).view(row) if row else None

    async def store_analysis(self, analysis: ArticleAnalysis) -> ArticleAnalysis:
        await self.session.execute(
            insert(ArticleAnalysisRecord)
            .values(
                id=uuid4(),
                article_id=analysis.article_id,
                article_revision=analysis.article_revision,
                model=analysis.model,
                prompt_version=analysis.prompt_version,
                payload=analysis.model_dump_json(),
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ArticleAnalysisRecord.article_id,
                    ArticleAnalysisRecord.article_revision,
                    ArticleAnalysisRecord.model,
                    ArticleAnalysisRecord.prompt_version,
                ]
            )
        )
        row = await self.session.scalar(
            select(ArticleRecord)
            .where(ArticleRecord.id == analysis.article_id)
            .with_for_update()
        )
        if row and row.revision == analysis.article_revision:
            article = Article.model_validate_json(row.payload)
            row.payload = Article(
                url=article.url,
                title=article.title,
                published_at=article.published_at,
                summary=article.summary,
                content=article.content,
                content_hash=article.content_hash,
                fetched_at=article.fetched_at,
                publisher=article.publisher,
                source_id=article.source_id,
                project=article.project,
                id=article.id,
                revision=article.revision,
                discovered_at=article.discovered_at,
                analysis_task_id=article.analysis_task_id,
                analysis=analysis,
            ).model_dump_json()
        return analysis

    async def store_article(
        self, value: ArticleContent, now: datetime
    ) -> tuple[Article, bool]:
        article_id = uuid4()
        article = Article(
            url=value.url,
            title=value.title,
            published_at=value.published_at,
            summary=value.summary,
            content=value.content,
            content_hash=value.content_hash,
            fetched_at=value.fetched_at,
            publisher=value.publisher,
            source_id=value.source_id,
            project=value.project,
            id=article_id,
            revision=1,
            discovered_at=now,
        )
        statement = (
            insert(ArticleRecord)
            .values(
                id=article_id,
                url=str(value.url),
                title=value.title,
                source_id=value.source_id,
                project=value.project,
                published_at=value.published_at,
                discovered_at=now,
                revision=1,
                content_hash=value.content_hash,
                payload=article.model_dump_json(),
            )
            .on_conflict_do_nothing(index_elements=[ArticleRecord.url])
            .returning(ArticleRecord.id)
        )
        inserted = await self.session.scalar(statement)
        if inserted:
            self.session.add(
                ArticleRevisionRecord(
                    article_id=article_id,
                    revision=1,
                    content_hash=value.content_hash,
                    payload=article.model_dump_json(),
                )
            )
            return article, True
        row = await self.session.scalar(
            select(ArticleRecord)
            .where(ArticleRecord.url == str(value.url))
            .with_for_update()
        )
        if row.content_hash == value.content_hash:
            return Article.model_validate_json(row.payload), False
        existing_revision = await self.session.scalar(
            select(ArticleRevisionRecord).where(
                ArticleRevisionRecord.article_id == row.id,
                ArticleRevisionRecord.content_hash == value.content_hash,
            )
        )
        if existing_revision:
            return Article.model_validate_json(row.payload), False
        updated = Article(
            url=value.url,
            title=value.title,
            published_at=value.published_at,
            summary=value.summary,
            content=value.content,
            content_hash=value.content_hash,
            fetched_at=value.fetched_at,
            publisher=value.publisher,
            source_id=value.source_id,
            project=value.project,
            id=row.id,
            revision=row.revision + 1,
            discovered_at=row.discovered_at,
        )
        row.title = value.title
        row.published_at = value.published_at
        row.content_hash = value.content_hash
        row.revision = updated.revision
        row.payload = updated.model_dump_json()
        self.session.add(
            ArticleRevisionRecord(
                article_id=row.id,
                revision=row.revision,
                content_hash=value.content_hash,
                payload=row.payload,
            )
        )
        return updated, True

    async def search(
        self, query: str, since: datetime, project: str | None, limit: int
    ) -> list[Article]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = select(ArticleRecord).where(
            or_(
                ArticleRecord.title.ilike(f"%{escaped}%", escape="\\"),
                ArticleRecord.payload.ilike(f"%{escaped}%", escape="\\"),
            ),
            or_(
                ArticleRecord.published_at >= since,
                (ArticleRecord.published_at.is_(None))
                & (ArticleRecord.discovered_at >= since),
            ),
        )
        if project is not None:
            statement = statement.where(ArticleRecord.project == project)
        rows = (
            await self.session.scalars(
                statement.order_by(ArticleRecord.published_at.desc()).limit(limit)
            )
        ).all()
        return [Article.model_validate_json(row.payload) for row in rows]

    async def create_run(
        self, source: Source, task_id: UUID, now: datetime
    ) -> CollectionRun:
        run = CollectionRun(
            id=uuid4(),
            source_id=source.id,
            task_id=task_id,
            revision=source.revision,
            started_at=now,
        )
        await self.session.execute(
            insert(CollectionRunRecord)
            .values(
                id=run.id,
                source_id=run.source_id,
                task_id=run.task_id,
                revision=run.revision,
                started_at=run.started_at,
                completed_at=run.completed_at,
            )
            .on_conflict_do_nothing(index_elements=[CollectionRunRecord.task_id])
        )
        await self.session.flush()
        return run

    async def complete_run(
        self, task_id: UUID, result: CollectionResult, now: datetime
    ) -> None:
        row = await self.session.scalar(
            select(CollectionRunRecord).where(CollectionRunRecord.task_id == task_id)
        )
        if row:
            row.completed_at = now
            row.result = result.model_dump_json()

    async def runs(self, source_id: UUID) -> list[CollectionRun]:
        rows = (
            await self.session.scalars(
                select(CollectionRunRecord)
                .where(CollectionRunRecord.source_id == source_id)
                .order_by(CollectionRunRecord.started_at.desc())
                .limit(NewsLimits().collection_run_history)
            )
        ).all()
        return [
            CollectionRun(
                id=row.id,
                source_id=row.source_id,
                task_id=row.task_id,
                revision=row.revision,
                started_at=row.started_at,
                completed_at=row.completed_at,
                result=CollectionResult.model_validate_json(row.result)
                if row.result
                else None,
            )
            for row in rows
        ]
