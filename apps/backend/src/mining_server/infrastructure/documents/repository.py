from uuid import UUID

from mining_contracts.domain.core import ErrorCode
from mining_contracts.domain.documents import (
    DocumentResponse,
    PageEvidence,
    ReportingStandard,
)
from sqlalchemy import Float, Select, and_, case, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from mining_server.domain.core import fail
from mining_server.domain.documents import (
    DocumentCandidates,
    DocumentPageLimits,
    DocumentSearchResult,
    ParsedPage,
    ReadPagesArguments,
    ResourcePagePriority,
    SearchPagesArguments,
)
from mining_server.domain.pdf import ParsedPageBatch
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.documents.models import (
    Document,
    DocumentPage,
    ResourceExtraction,
)


class DocumentRepository:
    def __init__(self, session: AsyncSession, limits: DocumentPageLimits | None = None):
        self.limits = limits if limits is not None else DocumentPageLimits()
        self.session = session

    async def get(self, identifier: UUID) -> Document:
        document = await self.session.get(Document, identifier)
        if document is None:
            raise fail(ErrorCode.NOT_FOUND, "Document was not found")
        return document

    async def get_extraction(self, identifier: UUID) -> ResourceExtraction:
        result = await self.session.get(ResourceExtraction, identifier)
        if result is None:
            raise fail(ErrorCode.NOT_FOUND, "Resource extraction was not found")
        return result

    @staticmethod
    def text_expression() -> ColumnElement[str]:
        return cast(DocumentPage.content_json, JSONB)["text"].astext

    @classmethod
    def normalized_text(cls) -> ColumnElement[str]:
        return func.lower(
            func.btrim(func.regexp_replace(cls.text_expression(), r"\s+", " ", "g"))
        )

    async def read(
        self, identifier: UUID, arguments: ReadPagesArguments
    ) -> DocumentSearchResult:
        rows = (
            await self.session.execute(
                self.page_select(self.limits.read_chars)
                .where(
                    DocumentPage.document_id == identifier,
                    DocumentPage.page_number.in_(arguments.pages),
                )
                .order_by(DocumentPage.page_number)
                .limit(self.limits.read_pages)
            )
        ).all()
        if len(rows) != len(set(arguments.pages)):
            raise fail(ErrorCode.INVALID_INPUT, "Requested PDF page does not exist")
        parsed = [
            ParsedPage(
                page_number=row.page_number,
                text=row.text,
                width=row.width,
                height=row.height,
            )
            for row in rows
        ]
        return DocumentSearchResult(
            pages=parsed, truncated=any(row.truncated for row in rows)
        )

    @classmethod
    def page_select(
        cls, character_limit: int
    ) -> Select[tuple[int, str, float, float, bool]]:
        content = cast(DocumentPage.content_json, JSONB)
        text = cls.text_expression()
        return select(
            DocumentPage.page_number,
            func.substr(text, 1, character_limit).label("text"),
            cast(content["width"].astext, Float).label("width"),
            cast(content["height"].astext, Float).label("height"),
            (func.length(text) > character_limit).label("truncated"),
        )

    async def search(
        self, identifier: UUID, arguments: SearchPagesArguments
    ) -> DocumentSearchResult:
        query = " ".join(arguments.query.split()).lower()
        rows = (
            await self.session.execute(
                self.page_select(self.limits.search_chars)
                .where(
                    DocumentPage.document_id == identifier,
                    self.normalized_text().contains(query, autoescape=True),
                )
                .order_by(DocumentPage.page_number)
                .limit(arguments.limit + 1)
            )
        ).all()
        selected = rows[: arguments.limit]
        return DocumentSearchResult(
            pages=[
                ParsedPage(
                    page_number=row.page_number,
                    text=row.text,
                    width=row.width,
                    height=row.height,
                )
                for row in selected
            ],
            truncated=len(rows) > arguments.limit
            or any(row.truncated for row in selected),
        )

    async def candidates(self, identifier: UUID) -> DocumentCandidates:
        text = func.lower(self.text_expression())
        indicated = text.contains("indicated", autoescape=True)
        inferred = text.contains("inferred", autoescape=True)
        estimate = text.contains("mineral resource estimate", autoescape=True)
        resource = text.contains("mineral resource", autoescape=True)
        priority = case(
            (
                and_(indicated, inferred, text.contains("table", autoescape=True)),
                ResourcePagePriority.CATEGORY_TABLE.value,
            ),
            (and_(indicated, inferred), ResourcePagePriority.CATEGORIES.value),
            (estimate, ResourcePagePriority.ESTIMATE.value),
            else_=ResourcePagePriority.RESOURCE.value,
        )
        pages = list(
            (
                await self.session.scalars(
                    select(DocumentPage.page_number)
                    .where(
                        DocumentPage.document_id == identifier,
                        or_(indicated, inferred, estimate, resource),
                    )
                    .order_by(priority, DocumentPage.page_number)
                    .limit(self.limits.candidate_pages + 1)
                )
            ).all()
        )
        return DocumentCandidates(
            pages=pages[: self.limits.candidate_pages],
            truncated=len(pages) > self.limits.candidate_pages,
        )

    async def has_standard(self, identifier: UUID, standard: ReportingStandard) -> bool:
        text = func.lower(self.text_expression())
        match standard:
            case ReportingStandard.NI_43_101:
                condition = or_(
                    *(
                        text.contains(marker, autoescape=True)
                        for marker in ("43-101", "43–101", "43 101")
                    )
                )
            case ReportingStandard.JORC:
                condition = text.contains("jorc", autoescape=True)
            case _:
                return False
        return (
            await self.session.scalar(
                select(DocumentPage.page_number)
                .where(DocumentPage.document_id == identifier, condition)
                .limit(1)
            )
            is not None
        )

    async def has_quote(self, identifier: UUID, evidence: PageEvidence) -> bool:
        quote = " ".join(evidence.quote.split()).lower()
        return (
            await self.session.scalar(
                select(DocumentPage.page_number)
                .where(
                    DocumentPage.document_id == identifier,
                    DocumentPage.page_number == evidence.pdf_page,
                    self.normalized_text().contains(quote, autoescape=True),
                )
                .limit(1)
            )
            is not None
        )

    async def count_pages(self, identifier: UUID) -> int:
        return await self.session.scalar(
            select(func.count())
            .select_from(DocumentPage)
            .where(DocumentPage.document_id == identifier)
        )

    async def save_pages(self, identifier: UUID, parsed: ParsedPageBatch) -> None:
        for page in parsed.pages:
            await self.session.execute(
                insert(DocumentPage)
                .values(
                    document_id=identifier,
                    page_number=page.page_number,
                    content_json=page.model_dump_json(),
                )
                .on_conflict_do_nothing()
            )

    @staticmethod
    def view(document: Document) -> DocumentResponse:
        return DocumentResponse(
            id=document.id,
            sha256=document.sha256,
            size_bytes=document.size_bytes,
            page_count=document.page_count,
            status=document.status,
            source_url=document.source_url,
            created_at=document.created_at,
        )


class RepositoryDocumentPages:
    def __init__(
        self,
        database: Database,
        document_id: UUID,
        page_count: int,
        repository_factory: type[DocumentRepository] = DocumentRepository,
    ):
        if page_count < 1 or page_count > DocumentPageLimits().max_pages:
            raise fail(
                ErrorCode.INVALID_INPUT,
                "Document page count is outside supported limits",
            )
        self.database = database
        self.document_id = document_id
        self.page_count = page_count
        self.repositories = repository_factory

    async def read(self, arguments: ReadPagesArguments) -> DocumentSearchResult:
        if any(number < 1 or number > self.page_count for number in arguments.pages):
            raise fail(ErrorCode.INVALID_INPUT, "Requested PDF page does not exist")
        async with self.database.sessions() as session:
            return await self.repositories(session).read(self.document_id, arguments)

    async def search(self, arguments: SearchPagesArguments) -> DocumentSearchResult:
        async with self.database.sessions() as session:
            return await self.repositories(session).search(self.document_id, arguments)

    async def candidates(self) -> DocumentCandidates:
        async with self.database.sessions() as session:
            return await self.repositories(session).candidates(self.document_id)

    async def has_standard(self, standard: ReportingStandard) -> bool:
        async with self.database.sessions() as session:
            return await self.repositories(session).has_standard(
                self.document_id, standard
            )

    async def has_quote(self, evidence: PageEvidence) -> bool:
        async with self.database.sessions() as session:
            return await self.repositories(session).has_quote(
                self.document_id, evidence
            )
