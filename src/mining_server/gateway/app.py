import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field, ValidationError, model_validator
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mining_server.domain.core import DomainError, ErrorCode, ErrorResponse
from mining_server.domain.documents import (
    ExtractionRequest,
    ExtractionResponse,
    ExtractionSubmission,
    ReportingStandard,
    ResourceExtractionResult,
)
from mining_server.domain.gateway import GatewayHealth, MCPContentType, ServerName
from mining_server.domain.http import BackendPath, HttpHeader
from mining_server.domain.market import (
    InstrumentList,
    LookupMode,
    PriceResponse,
    TrendResponse,
)
from mining_server.domain.news import Article, NewsSearchResponse
from mining_server.domain.task_views import TaskView
from mining_server.gateway.client import BackendClient
from mining_server.gateway.settings import GatewaySettings


class ResultStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class GatewayResult[T: BaseModel](BaseModel):
    status: ResultStatus
    result: T | None = None
    error: ErrorResponse | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "GatewayResult[T]":
        if self.status == ResultStatus.SUCCESS and (
            self.result is None or self.error is not None
        ):
            raise ValueError("Success requires only a result")
        if self.status == ResultStatus.ERROR and (
            self.error is None or self.result is not None
        ):
            raise ValueError("Error requires only an error")
        return self


class GatewayAuthentication(BaseHTTPMiddleware):
    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        if request.url.path == BackendPath.LIVE:
            return await call_next(request)
        if HttpHeader.AUTHORIZATION not in request.headers or not secrets.compare_digest(
            request.headers[HttpHeader.AUTHORIZATION], f"Bearer {self.token}"
        ):
            error = ErrorResponse(
                code=ErrorCode.UNAUTHORIZED,
                message="Gateway service authentication required",
                request_id=str(uuid4()),
            )
            return JSONResponse(
                error.model_dump(mode="json"), status_code=HTTPStatus.UNAUTHORIZED
            )
        return await call_next(request)


async def invoke[T: BaseModel](action: Callable[[], Awaitable[T]]) -> CallToolResult:
    try:
        result = GatewayResult(status=ResultStatus.SUCCESS, result=await action())
        return CallToolResult(
            content=[
                TextContent(type=MCPContentType.TEXT, text=result.model_dump_json())
            ],
            structured_content=result.model_dump(mode="json"),
        )
    except DomainError as error:
        result = GatewayResult(
            status=ResultStatus.ERROR,
            error=ErrorResponse(
                code=error.code,
                message=error.message,
                details=error.details,
                request_id=str(uuid4()),
            ),
        )
        return CallToolResult(
            content=[
                TextContent(type=MCPContentType.TEXT, text=result.model_dump_json())
            ],
            structured_content=result.model_dump(mode="json"),
            is_error=True,
        )
    except ValidationError:
        result = GatewayResult(
            status=ResultStatus.ERROR,
            error=ErrorResponse(
                code=ErrorCode.INVALID_INPUT,
                message="Tool input validation failed",
                request_id=str(uuid4()),
            ),
        )
        return CallToolResult(
            content=[
                TextContent(type=MCPContentType.TEXT, text=result.model_dump_json())
            ],
            structured_content=result.model_dump(mode="json"),
            is_error=True,
        )


def create_app(
    settings: GatewaySettings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> Starlette:
    configuration = settings or GatewaySettings()
    owned_client = http_client is None
    client = http_client or httpx.AsyncClient(
        base_url=str(configuration.backend_url),
        headers=[
            (
                HttpHeader.AUTHORIZATION,
                f"Bearer {configuration.service_token.get_secret_value()}",
            )
        ],
        timeout=configuration.request_timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    )
    backend = BackendClient(client)
    news = MCPServer(
        ServerName.NEWS,
        version="0.1.0",
        instructions="Search the authenticated backend news corpus; returned tasks require polling",
    )
    documents = MCPServer(
        ServerName.DOCUMENTS,
        version="0.1.0",
        instructions="Extract evidence-backed resources from a public PDF URL or registered document; poll asynchronous submissions",
    )
    market = MCPServer(
        ServerName.MARKET,
        version="0.1.0",
        instructions="Choose an explicit instrument slug; spot, futures and quarterly realised prices have separate series and coverage",
    )

    @news.tool(
        description="Search collected public mining news and return source coverage",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def search(
        query: Annotated[str, Field(min_length=1, max_length=500)],
        days: Annotated[int, Field(ge=1, le=3650)] = 7,
    ) -> Annotated[CallToolResult, GatewayResult[NewsSearchResponse]]:
        return await invoke(lambda: backend.search(query, days))

    @news.tool(
        description="Return a stored article or submit an asynchronous public article fetch",
        annotations=ToolAnnotations(read_only_hint=False),
    )
    async def fetch_article(
        url: str,
    ) -> Annotated[CallToolResult, GatewayResult[Article | TaskView]]:
        return await invoke(lambda: backend.fetch_article(url))

    @news.tool(
        description="Poll an article-fetch or source collection task",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def get_task(
        task_id: UUID,
    ) -> Annotated[CallToolResult, GatewayResult[TaskView]]:
        return await invoke(lambda: backend.get_task(task_id))

    @documents.tool(
        description="Extract NI 43-101 or JORC resources; exactly one PDF URL or document ID is required",
        annotations=ToolAnnotations(read_only_hint=False),
    )
    async def extract_resources(
        pdf_url: str | None = None,
        document_id: UUID | None = None,
        standard: ReportingStandard = ReportingStandard.AUTO,
    ) -> Annotated[CallToolResult, GatewayResult[ExtractionSubmission]]:
        async def submit() -> ExtractionSubmission:
            return await backend.extract_resources(
                ExtractionRequest(
                    pdf_url=pdf_url, document_id=document_id, standard=standard
                )
            )

        return await invoke(submit)

    @documents.tool(
        description="Poll a resource extraction by extraction ID",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def get_extraction(
        extraction_id: UUID,
    ) -> Annotated[CallToolResult, GatewayResult[ExtractionResponse]]:
        return await invoke(lambda: backend.get_extraction(extraction_id))

    @documents.tool(
        description="Read completed or partial extraction records and page evidence",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def get_extraction_result(
        extraction_id: UUID,
    ) -> Annotated[CallToolResult, GatewayResult[ResourceExtractionResult]]:
        return await invoke(lambda: backend.get_extraction_result(extraction_id))

    @market.tool(
        description="List explicit price instruments, specifications and permitted-use notices",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def list_instruments() -> Annotated[
        CallToolResult, GatewayResult[InstrumentList]
    ]:
        return await invoke(backend.instruments)

    @market.tool(
        description="Get an exact observation or explicitly request the latest available observation",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def get_price(
        commodity: str, date: date, mode: LookupMode = LookupMode.EXACT
    ) -> Annotated[CallToolResult, GatewayResult[PriceResponse]]:
        return await invoke(lambda: backend.get_price(commodity, date, mode))

    @market.tool(
        description="Return only actual observations, with frequency and gaps; never fill missing daily quotes",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def get_trend(
        commodity: str, days: Annotated[int, Field(ge=1, le=3650)] = 7
    ) -> Annotated[CallToolResult, GatewayResult[TrendResponse]]:
        return await invoke(lambda: backend.get_trend(commodity, days))

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["localhost:*", "127.0.0.1:*", "mcp-gateway:*", "testserver"],
        allowed_origins=["http://localhost:*", "http://127.0.0.1:*"],
    )
    routes = [
        Mount(
            "/mcp/news",
            news.streamable_http_app(
                streamable_http_path="/",
                stateless_http=True,
                json_response=True,
                host="0.0.0.0",
                transport_security=security,
            ),
        ),
        Mount(
            "/mcp/documents",
            documents.streamable_http_app(
                streamable_http_path="/",
                stateless_http=True,
                json_response=True,
                host="0.0.0.0",
                transport_security=security,
            ),
        ),
        Mount(
            "/mcp/market",
            market.streamable_http_app(
                streamable_http_path="/",
                stateless_http=True,
                json_response=True,
                host="0.0.0.0",
                transport_security=security,
            ),
        ),
    ]

    async def health(request: Request) -> JSONResponse:
        response = GatewayHealth(servers=list(ServerName))
        return JSONResponse(response.model_dump(mode="json"))

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(news.session_manager.run())
                await stack.enter_async_context(documents.session_manager.run())
                await stack.enter_async_context(market.session_manager.run())
                yield
        finally:
            if owned_client:
                await client.aclose()

    app = Starlette(
        routes=[Route(BackendPath.LIVE, health), *routes], lifespan=lifespan
    )
    app.add_middleware(
        GatewayAuthentication, token=configuration.service_token.get_secret_value()
    )
    app.state.servers = (news, documents, market)
    return app


def app_factory() -> Starlette:
    return create_app()
