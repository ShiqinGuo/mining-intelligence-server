import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from celery import Celery
from kombu.exceptions import KombuError
from pydantic import ValidationError
from sqlalchemy import literal, select
from sqlalchemy.exc import DBAPIError

from mining_server.domain.health import (
    HealthEvidence,
    HealthOrigin,
    HealthProbe,
    HealthResponse,
)
from mining_server.domain.tasks import WorkerTaskName
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.settings import Settings
from mining_server.worker.health import evidence_path


class HealthService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        celery: Celery | None,
        publisher_ready: Callable[[], bool],
    ):
        self.database = database
        self.settings = settings
        self.celery = celery
        self.publisher_ready = publisher_ready
        self.probe_lock = asyncio.Lock()

    async def evidence(self, origin: HealthOrigin) -> HealthEvidence | None:
        path = evidence_path(self.settings, origin)
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            return HealthEvidence.model_validate_json(raw)
        except (OSError, ValidationError):
            return None

    async def readiness(self) -> HealthResponse:
        database_ready = False
        worker_ready = False
        beat_ready = False
        publisher_ready = self.publisher_ready()
        try:
            async with asyncio.timeout(self.settings.health_probe_timeout_seconds):
                async with self.probe_lock:
                    async with self.database.sessions() as session:
                        await session.scalar(
                            select(
                                literal(self.settings.task_defaults.single_record_limit)
                            )
                        )
                    database_ready = True
                    beat = await self.evidence(HealthOrigin.BEAT)
                    if beat and beat.origin == HealthOrigin.BEAT:
                        age = (datetime.now(UTC) - beat.requested_at).total_seconds()
                        beat_ready = 0 <= age < self.settings.health_beat_ttl_seconds
                    if self.celery is not None and publisher_ready:
                        probe = HealthProbe(
                            origin=HealthOrigin.API,
                            nonce=uuid4(),
                            requested_at=datetime.now(UTC),
                        )
                        await asyncio.to_thread(
                            self.celery.send_task,
                            WorkerTaskName.HEALTH.value,
                            args=[probe.model_dump(mode="json")],
                            task_id=str(probe.nonce),
                            retry=False,
                            expires=self.settings.health_probe_timeout_seconds,
                        )
                        while True:
                            evidence = await self.evidence(HealthOrigin.API)
                            if (
                                evidence
                                and evidence.nonce == probe.nonce
                                and evidence.origin == HealthOrigin.API
                            ):
                                worker_ready = True
                                break
                            await asyncio.sleep(self.settings.health_poll_seconds)
        except (TimeoutError, KombuError, OSError, DBAPIError):
            pass
        return HealthResponse(
            ready=database_ready and worker_ready and beat_ready and publisher_ready,
            database=database_ready,
            worker=worker_ready,
            beat=beat_ready,
            publisher=publisher_ready,
        )
