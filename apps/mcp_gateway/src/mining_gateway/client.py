from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from uuid import UUID, uuid4

import httpx
from mining_contracts.domain.core import ErrorCode, ErrorDetails, ErrorResponse
from mining_contracts.domain.documents import (
    ExtractionRequest,
    ExtractionResponse,
    ExtractionSubmission,
    ResourceExtractionResult,
)
from mining_contracts.domain.http import (
    BackendPath,
    HttpHeader,
    HttpMethod,
    MediaType,
    QueryField,
)
from mining_contracts.domain.market import (
    InstrumentList,
    LookupMode,
    PriceResponse,
    TrendResponse,
)
from mining_contracts.domain.news import (
    Article,
    ArticleFetchRequest,
    NewsSearchResponse,
)
from mining_contracts.domain.task_views import TaskView
from pydantic import BaseModel, ValidationError

from mining_gateway.domain.errors import DomainError, fail


@dataclass(frozen=True)
class QueryParameters:
    query: str | None = None
    days: str | None = None
    commodity: str | None = None
    date: str | None = None
    mode: str | None = None

    def pairs(self) -> list[tuple[str, str]]:
        candidates = [
            (QueryField.QUERY, self.query),
            (QueryField.DAYS, self.days),
            (QueryField.COMMODITY, self.commodity),
            (QueryField.DATE, self.date),
            (QueryField.MODE, self.mode),
        ]
        return [(name, value) for name, value in candidates if value is not None]


class BackendClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def request[T: BaseModel](
        self,
        method: HttpMethod,
        path: str,
        model: type[T],
        payload: BaseModel | None = None,
        params: QueryParameters | None = None,
        idempotency_key: str | None = None,
    ) -> T:
        headers = [(HttpHeader.REQUEST_ID, str(uuid4()))]
        if idempotency_key:
            headers.append((HttpHeader.IDEMPOTENCY_KEY, idempotency_key))
        if payload is not None:
            headers.append((HttpHeader.CONTENT_TYPE, MediaType.JSON))
        try:
            response = await self.client.request(
                method,
                path,
                content=payload.model_dump_json() if payload is not None else None,
                params=params.pairs() if params is not None else None,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise fail(
                ErrorCode.UPSTREAM_FAILURE,
                "Backend transport failed",
                ErrorDetails(retryable=True, reason=type(exc).__name__),
            ) from exc
        if response.is_error:
            try:
                error = ErrorResponse.model_validate_json(response.content)
            except ValidationError as exc:
                raise fail(
                    ErrorCode.UPSTREAM_FAILURE,
                    "Backend returned an invalid error response",
                ) from exc
            raise DomainError(error.code, error.message, error.details)
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise fail(
                ErrorCode.UPSTREAM_FAILURE,
                "Backend returned an invalid response contract",
            ) from exc

    async def search(self, query: str, days: int) -> NewsSearchResponse:
        return await self.request(
            HttpMethod.GET,
            BackendPath.NEWS,
            NewsSearchResponse,
            params=QueryParameters(query=query, days=str(days)),
        )

    async def fetch_article(self, url: str) -> Article | TaskView:
        payload = ArticleFetchRequest(url=url)
        try:
            response = await self.client.post(
                BackendPath.ARTICLE_FETCH,
                content=payload.model_dump_json(),
                headers=[
                    (HttpHeader.IDEMPOTENCY_KEY, f"gateway-article:{uuid4()}"),
                    (HttpHeader.CONTENT_TYPE, MediaType.JSON),
                ],
            )
            if response.is_error:
                error = ErrorResponse.model_validate_json(response.content)
                raise DomainError(error.code, error.message, error.details)
            return (
                TaskView.model_validate_json(response.content)
                if response.status_code == HTTPStatus.ACCEPTED
                else Article.model_validate_json(response.content)
            )
        except httpx.HTTPError as exc:
            raise fail(
                ErrorCode.UPSTREAM_FAILURE,
                "Backend article transport failed",
                ErrorDetails(retryable=True),
            ) from exc
        except ValidationError as exc:
            raise fail(
                ErrorCode.UPSTREAM_FAILURE,
                "Backend returned an invalid article contract",
            ) from exc

    async def get_task(self, task_id: UUID) -> TaskView:
        return await self.request(
            HttpMethod.GET, f"{BackendPath.TASKS}/{task_id}", TaskView
        )

    async def instruments(self) -> InstrumentList:
        return await self.request(
            HttpMethod.GET, BackendPath.INSTRUMENTS, InstrumentList
        )

    async def get_price(
        self, commodity: str, requested_date: date, mode: LookupMode
    ) -> PriceResponse:
        return await self.request(
            HttpMethod.GET,
            BackendPath.PRICES,
            PriceResponse,
            params=QueryParameters(
                commodity=commodity, date=requested_date.isoformat(), mode=mode.value
            ),
        )

    async def get_trend(self, commodity: str, days: int) -> TrendResponse:
        return await self.request(
            HttpMethod.GET,
            BackendPath.TRENDS,
            TrendResponse,
            params=QueryParameters(commodity=commodity, days=str(days)),
        )

    async def extract_resources(
        self, request: ExtractionRequest
    ) -> ExtractionSubmission:
        return await self.request(
            HttpMethod.POST,
            BackendPath.EXTRACTIONS,
            ExtractionSubmission,
            payload=request,
            idempotency_key=f"gateway-extract:{uuid4()}",
        )

    async def get_extraction(self, extraction_id: UUID) -> ExtractionResponse:
        return await self.request(
            HttpMethod.GET,
            f"{BackendPath.EXTRACTIONS}/{extraction_id}",
            ExtractionResponse,
        )

    async def get_extraction_result(
        self, extraction_id: UUID
    ) -> ResourceExtractionResult:
        return await self.request(
            HttpMethod.GET,
            f"{BackendPath.EXTRACTIONS}/{extraction_id}/result",
            ResourceExtractionResult,
        )
