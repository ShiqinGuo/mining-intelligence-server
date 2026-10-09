import asyncio
import logging
import time
from datetime import timedelta
from uuid import uuid4

from celery import Celery
from kombu.exceptions import KombuError
from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError

from mining_server.application.tasks import now
from mining_server.domain.tasks import OutboxDelivery, TaskEnvelope, WorkerTaskName
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.settings import Settings
from mining_server.infrastructure.task_models import OutboxMessage

logger = logging.getLogger(__name__)


class OutboxPublisher:
    def __init__(self, database: Database, settings: Settings, celery: Celery):
        self.database = database
        self.settings = settings
        self.celery = celery
        self.last_poll_at = 0.0

    async def claim(self) -> OutboxDelivery | None:
        async with self.database.sessions() as session, session.begin():
            row = await session.scalar(
                select(OutboxMessage)
                .where(
                    OutboxMessage.published_at.is_(None),
                    or_(
                        OutboxMessage.claimed_until.is_(None),
                        OutboxMessage.claimed_until < now(),
                    ),
                )
                .order_by(OutboxMessage.created_at)
                .with_for_update(skip_locked=True)
                .limit(self.settings.task_defaults.single_record_limit)
            )
            if row is None:
                return None
            row.claimed_by = uuid4()
            row.claimed_until = now() + timedelta(
                seconds=self.settings.outbox_claim_seconds
            )
            row.attempts += self.settings.task_defaults.revision_increment
            return OutboxDelivery(
                message_id=row.id,
                owner=row.claimed_by,
                envelope=TaskEnvelope(task_id=row.task_id, generation=row.generation),
            )

    async def publish_one(self) -> bool:
        delivery = await self.claim()
        if delivery is None:
            return False
        try:
            await asyncio.to_thread(
                self.celery.send_task,
                WorkerTaskName.EXECUTE.value,
                args=[delivery.envelope.model_dump(mode="json")],
                task_id=str(delivery.message_id),
                retry=False,
            )
        except (KombuError, OSError) as error:
            async with self.database.sessions() as session, session.begin():
                row = await session.get(OutboxMessage, delivery.message_id)
                if row.claimed_by == delivery.owner:
                    row.last_error = type(error).__name__
            logger.warning("Outbox publish failed: %s", type(error).__name__)
            return False
        async with self.database.sessions() as session, session.begin():
            row = await session.get(OutboxMessage, delivery.message_id)
            if row.claimed_by == delivery.owner:
                row.published_at = now()
                row.claimed_until = None
                row.last_error = None
        return True

    async def run(self) -> None:
        while True:
            try:
                published = await self.publish_one()
                self.last_poll_at = time.monotonic()
            except DBAPIError as error:
                if not error.connection_invalidated:
                    raise
                logger.warning("Outbox database connection was lost")
                published = False
            except (OSError, TimeoutError):
                logger.warning("Outbox database connection is unavailable")
                published = False
            if not published:
                await asyncio.sleep(self.settings.outbox_interval_seconds)
