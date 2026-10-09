import asyncio
from datetime import datetime, timedelta
from uuid import UUID

from celery import Task

from mining_server.application.model_channels import ModelChannelsService
from mining_server.application.tasks import TaskService
from mining_server.domain.health import HealthOrigin, HealthProbe
from mining_server.domain.tasks import TaskEnvelope, WorkerTaskName
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.settings import Settings
from mining_server.worker.celery_app import app
from mining_server.worker.composition import create_registry
from mining_server.worker.health import save_evidence


async def execute(envelope: TaskEnvelope) -> None:
    settings = Settings()
    database = Database(settings.database_url)
    try:
        await create_registry(database, settings).execute(
            TaskService(database, settings), envelope
        )
    finally:
        await database.close()


async def recover() -> None:
    settings = Settings()
    database = Database(settings.database_url)
    try:
        tasks = TaskService(database, settings)
        await tasks.recover_due()
        await tasks.recover_waiting_auth(
            ModelChannelsService(database, settings, tasks).check_for_recovery
        )
    finally:
        await database.close()


async def sources_due() -> None:
    settings = Settings()
    database = Database(settings.database_url)
    try:
        for handler in create_registry(database, settings).due_sources:
            await handler()
    finally:
        await database.close()


@app.task(name=WorkerTaskName.EXECUTE.value, pydantic=True)
def execute_task(payload: TaskEnvelope) -> None:
    asyncio.run(execute(payload))


@app.task(name=WorkerTaskName.RECOVER.value)
def recover_task() -> None:
    asyncio.run(recover())


@app.task(name=WorkerTaskName.SOURCES_DUE.value)
def sources_due_task() -> None:
    asyncio.run(sources_due())


@app.task(name=WorkerTaskName.HEALTH.value, bind=True, pydantic=True)
def health_task(self: Task, payload: HealthProbe | None = None) -> None:
    settings = Settings()
    if payload is not None:
        probe = payload
    else:
        expires = self.request.expires
        if expires is None:
            raise ValueError("Beat health probe requires an expiry")
        probe = HealthProbe(
            origin=HealthOrigin.BEAT,
            nonce=UUID(self.request.id),
            requested_at=datetime.fromisoformat(expires)
            - timedelta(seconds=settings.health_beat_ttl_seconds),
        )
    save_evidence(settings, probe)
