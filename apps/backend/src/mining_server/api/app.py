import asyncio
import logging
import time
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mining_contracts.domain.core import ErrorCode, ErrorDetails, ErrorResponse
from mining_contracts.domain.http import HttpHeader
from starlette.middleware.base import RequestResponseEndpoint

from mining_server.api import settings as settings_api
from mining_server.api import tasks
from mining_server.api.auth import router as auth_router
from mining_server.api.dependencies import ApplicationState, get_health_service
from mining_server.api.documents import router as document_router
from mining_server.api.market import router as market_router
from mining_server.api.model_channels import router as model_channels_router
from mining_server.api.news import router as news_router
from mining_server.api.sources import router as source_router
from mining_server.api.upload_ingress import UploadIngressMiddleware
from mining_server.application.health import HealthService
from mining_server.application.market import seed_defaults as seed_market
from mining_server.application.news import seed_defaults as seed_news
from mining_server.application.outbox import OutboxPublisher
from mining_server.domain.core import DomainError
from mining_server.domain.health import HealthResponse
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.settings import Settings

logger = logging.getLogger(__name__)


def status_for(code: ErrorCode) -> HTTPStatus:
    match code:
        case ErrorCode.INVALID_INPUT | ErrorCode.STANDARD_MISMATCH:
            return HTTPStatus.UNPROCESSABLE_ENTITY
        case ErrorCode.NOT_FOUND | ErrorCode.NO_QUOTE:
            return HTTPStatus.NOT_FOUND
        case ErrorCode.UNAUTHORIZED | ErrorCode.WAITING_AUTH:
            return HTTPStatus.UNAUTHORIZED
        case ErrorCode.FORBIDDEN:
            return HTTPStatus.FORBIDDEN
        case (
            ErrorCode.CONFLICT
            | ErrorCode.UNKNOWN_RESULT
            | ErrorCode.LEASE_LOST
            | ErrorCode.CANCELLED
            | ErrorCode.COVERAGE_INSUFFICIENT
        ):
            return HTTPStatus.CONFLICT
        case ErrorCode.WAITING_QUOTA | ErrorCode.BUSY:
            return HTTPStatus.TOO_MANY_REQUESTS
        case ErrorCode.UPSTREAM_FAILURE:
            return HTTPStatus.BAD_GATEWAY
        case ErrorCode.INTERNAL:
            return HTTPStatus.INTERNAL_SERVER_ERROR


def create_app(
    settings: Settings | None = None, start_publisher: bool = True
) -> FastAPI:
    configuration = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        database = Database(configuration.database_url)
        publisher_task = None
        publisher = None
        celery = None
        try:
            await seed_news(database, configuration)
            await seed_market(database, configuration)
            if start_publisher:
                from mining_server.worker.broker import create_celery

                celery = create_celery(configuration)
                publisher = OutboxPublisher(database, configuration, celery)
                publisher_task = asyncio.create_task(publisher.run())
            health_service = HealthService(
                database,
                configuration,
                celery,
                lambda: (
                    publisher is not None
                    and publisher_task is not None
                    and not publisher_task.done()
                    and time.monotonic() - publisher.last_poll_at
                    < max(
                        configuration.publisher_stale_seconds,
                        configuration.outbox_interval_seconds
                        * configuration.publisher_stale_multiplier,
                    )
                ),
            )
            application.state.runtime = ApplicationState(
                database=database, settings=configuration, health_service=health_service
            )
            yield
        finally:
            if publisher_task:
                publisher_task.cancel()
                await asyncio.gather(publisher_task, return_exceptions=True)
            if celery:
                await asyncio.to_thread(celery.close)
            await database.close()

    application = FastAPI(
        title="Mining Business API", version="0.1.0", lifespan=lifespan
    )
    application.add_middleware(UploadIngressMiddleware)

    @application.middleware("http")
    async def request_identity(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers[HttpHeader.REQUEST_ID.value] = request.state.request_id
        return response

    @application.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError) -> JSONResponse:
        body = ErrorResponse(
            code=error.code,
            message=error.message,
            details=error.details,
            request_id=request.state.request_id,
        )
        return JSONResponse(
            status_code=status_for(error.code), content=body.model_dump(mode="json")
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        body = ErrorResponse(
            code=ErrorCode.INVALID_INPUT,
            message="Request validation failed",
            details=ErrorDetails(reason="Invalid request parameters"),
            request_id=request.state.request_id,
        )
        return JSONResponse(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            content=body.model_dump(mode="json"),
        )

    @application.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        logger.error("Request failed with %s", type(error).__name__)
        body = ErrorResponse(
            code=ErrorCode.INTERNAL,
            message="Internal server error",
            request_id=request.state.request_id,
        )
        return JSONResponse(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            content=body.model_dump(mode="json"),
        )

    @application.get("/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(ready=True)

    @application.get("/health/ready", response_model=HealthResponse)
    async def ready(
        response: Response,
        service: Annotated[HealthService, Depends(get_health_service)],
    ) -> HealthResponse:
        result = await service.readiness()
        if not result.ready:
            response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
        return result

    application.include_router(tasks.router)
    application.include_router(settings_api.router)
    application.include_router(source_router)
    application.include_router(news_router)
    application.include_router(market_router)
    application.include_router(document_router)
    application.include_router(auth_router)
    application.include_router(model_channels_router)
    return application
