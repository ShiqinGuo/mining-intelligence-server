from datetime import date, datetime
from http import HTTPStatus
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query

from mining_server.api.dependencies import (
    get_database,
    get_settings,
    get_task_service,
    require_admin,
    require_service,
)
from mining_server.application.market import MarketService
from mining_server.application.tasks import TaskService
from mining_server.domain.market import (
    InstrumentCreate,
    InstrumentList,
    InstrumentUpdate,
    LookupMode,
    MarketRefreshRequest,
    PriceInstrument,
    PriceResponse,
    TrendResponse,
)
from mining_server.domain.task_views import TaskView
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.market.repository import MarketRepository
from mining_server.infrastructure.settings import Settings

router = APIRouter(
    prefix="/api/v1", tags=["market"], dependencies=[Depends(require_service)]
)


def get_market_repository_factory() -> type[MarketRepository]:
    return MarketRepository


def get_market_service(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    tasks: Annotated[TaskService, Depends(get_task_service)],
    repository_factory: Annotated[
        type[MarketRepository], Depends(get_market_repository_factory)
    ],
) -> MarketService:
    return MarketService(database, settings, tasks, repository_factory)


@router.get("/price-instruments", response_model=InstrumentList)
async def list_instruments(
    service: Annotated[MarketService, Depends(get_market_service)],
) -> InstrumentList:
    return await service.instruments()


@router.post(
    "/price-instruments",
    response_model=PriceInstrument,
    status_code=HTTPStatus.CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_instrument(
    request: InstrumentCreate,
    service: Annotated[MarketService, Depends(get_market_service)],
) -> PriceInstrument:
    return await service.create(request)


@router.put(
    "/price-instruments/{slug}",
    response_model=PriceInstrument,
    dependencies=[Depends(require_admin)],
)
async def update_instrument(
    slug: str,
    request: InstrumentUpdate,
    service: Annotated[MarketService, Depends(get_market_service)],
) -> PriceInstrument:
    return await service.update(slug, request)


@router.post(
    "/price-instruments/{slug}/runs",
    response_model=TaskView,
    status_code=HTTPStatus.ACCEPTED,
    dependencies=[Depends(require_admin)],
)
async def refresh_instrument(
    slug: str,
    request: MarketRefreshRequest,
    service: Annotated[MarketService, Depends(get_market_service)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> TaskView:
    return await service.refresh(slug, request, idempotency_key or f"price:{uuid4()}")


@router.get("/prices", response_model=PriceResponse)
async def get_price(
    service: Annotated[MarketService, Depends(get_market_service)],
    commodity: str,
    date: date,
    mode: LookupMode = LookupMode.EXACT,
    known_at: datetime | None = None,
) -> PriceResponse:
    return await service.price(commodity, date, mode, known_at)


@router.get("/price-trends", response_model=TrendResponse)
async def get_trend(
    service: Annotated[MarketService, Depends(get_market_service)],
    commodity: str,
    days: Annotated[int, Query(ge=1, le=3650)] = 7,
    end: date | None = None,
    known_at: datetime | None = None,
) -> TrendResponse:
    return await service.trend(commodity, days, end, known_at)
