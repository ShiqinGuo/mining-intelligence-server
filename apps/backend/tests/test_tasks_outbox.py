import asyncio
import os
from dataclasses import dataclass
from uuid import uuid4

import pytest
from apps.backend.tests.test_tasks_recovery import Input, service
from mining_contracts.domain.tasks import TaskKind
from pydantic import TypeAdapter
from sqlalchemy import select

from mining_server.application.outbox import OutboxPublisher
from mining_server.application.tasks import TaskService
from mining_server.domain.tasks import TaskEnvelope
from mining_server.infrastructure.settings import Settings
from mining_server.infrastructure.task_models import OutboxMessage
from mining_server.worker.broker import create_celery

__all__ = ["service"]


@dataclass
class ExecuteKeywords:
    pass


@dataclass
class EmbeddedCallbacks:
    callbacks: None = None
    errbacks: None = None
    chain: None = None
    chord: None = None


def broker_settings(service: TaskService, broker: str) -> Settings:
    return Settings(
        database_url=service.settings.database_url,
        admin_token=service.settings.admin_token,
        service_token=service.settings.service_token,
        master_key=service.settings.master_key,
        oauth_host_id=service.settings.oauth_host_id,
        broker_url=broker,
    )


async def test_confirmed_publish_and_confirmation_loss_replay(service):
    broker = os.getenv("MINING_TEST_BROKER_URL")
    if not broker:
        pytest.skip("MINING_TEST_BROKER_URL is required for RabbitMQ integration tests")
    settings = broker_settings(service, broker)
    celery = create_celery(settings)
    queue_name = f"mining-test-{uuid4().hex}"
    celery.conf.task_default_queue = queue_name
    publisher = OutboxPublisher(service.database, settings, celery)
    task = await service.submit(TaskKind.FETCH_ARTICLE, Input(value=1), "outbox")

    def receive():
        with celery.connection_for_read() as connection:
            queue = connection.SimpleQueue(queue_name)
            try:
                message = queue.get(block=True, timeout=5)
                payload = TypeAdapter(
                    tuple[list[TaskEnvelope], ExecuteKeywords, EmbeddedCallbacks]
                ).validate_python(message.payload)
                message.ack()
                return payload[0][0]
            finally:
                queue.close()

    def cleanup():
        with celery.connection_for_read() as connection:
            connection.channel().queue_delete(queue=queue_name)
        celery.close()

    try:
        assert await publisher.publish_one() is True
        first = await asyncio.to_thread(receive)
        async with service.database.sessions() as session, session.begin():
            row = await session.scalar(select(OutboxMessage))
            assert row.published_at is not None
            row.published_at = None
            row.claimed_until = None
        assert await publisher.publish_one() is True
        second = await asyncio.to_thread(receive)
        assert first == second
        assert first.task_id == task.id
        assert await service.claim(first, uuid4()) is not None
        assert await service.claim(second, uuid4()) is None
    finally:
        await asyncio.to_thread(cleanup)


async def test_broker_unavailable_preserves_durable_outbox(service):
    settings = broker_settings(
        service, "amqp://mining:mining-local-test@127.0.0.1:35674//"
    )
    celery = create_celery(settings)
    try:
        task = await service.submit(
            TaskKind.FETCH_ARTICLE, Input(value=1), "unavailable"
        )
        publisher = OutboxPublisher(service.database, settings, celery)
        assert await publisher.publish_one() is False
        async with service.database.sessions() as session:
            row = await session.scalar(
                select(OutboxMessage).where(OutboxMessage.task_id == task.id)
            )
            assert row.published_at is None
            assert row.attempts == 1
            assert row.last_error is not None
    finally:
        celery.close()
