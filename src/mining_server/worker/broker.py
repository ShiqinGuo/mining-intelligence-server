import os
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from celery import Celery

from mining_server.domain.tasks import WorkerTaskName
from mining_server.infrastructure.settings import Settings


class BrokerPlatform(StrEnum):
    WINDOWS = "nt"
    POSIX = "posix"


class CelerySerializer(StrEnum):
    JSON = "json"


class CeleryDeliveryMode(StrEnum):
    PERSISTENT = "persistent"


class BeatEntryName(StrEnum):
    HEALTH = "health-evidence"
    RECOVER = "recover-leases"
    SOURCES_DUE = "check-due-sources"


@dataclass(frozen=True)
class BeatEntry:
    name: BeatEntryName
    task: WorkerTaskName
    interval_seconds: float
    expires_seconds: float | None = None


def create_celery(settings: Settings) -> Celery:
    class BrokerTransportOptions(TypedDict, total=False):
        confirm_publish: bool
        read_timeout: int
        write_timeout: int

    class BeatOptions(TypedDict):
        expires: float

    class BeatDefinition(TypedDict, total=False):
        task: str
        schedule: float
        options: BeatOptions

    BeatSchedule = TypedDict(
        "BeatSchedule",
        {
            "health-evidence": BeatDefinition,
            "recover-leases": BeatDefinition,
            "check-due-sources": BeatDefinition,
        },
        total=False,
    )
    entries = (
        BeatEntry(
            BeatEntryName.HEALTH,
            WorkerTaskName.HEALTH,
            settings.health_beat_interval_seconds,
            settings.health_beat_ttl_seconds,
        ),
        BeatEntry(
            BeatEntryName.RECOVER,
            WorkerTaskName.RECOVER,
            settings.scheduler_interval_seconds,
        ),
        BeatEntry(
            BeatEntryName.SOURCES_DUE,
            WorkerTaskName.SOURCES_DUE,
            settings.scheduler_interval_seconds,
        ),
    )
    schedule: BeatSchedule = {}
    for entry in entries:
        definition: BeatDefinition = {
            "task": entry.task.value,
            "schedule": entry.interval_seconds,
        }
        if entry.expires_seconds is not None:
            definition["options"] = BeatOptions(expires=entry.expires_seconds)
        schedule[entry.name.value] = definition
    transport_options: BrokerTransportOptions = {"confirm_publish": True}
    match BrokerPlatform(os.name):
        case BrokerPlatform.POSIX:
            transport_options.update(
                read_timeout=settings.broker_socket_timeout_seconds,
                write_timeout=settings.broker_socket_timeout_seconds,
            )
        case BrokerPlatform.WINDOWS:
            pass
    app = Celery(
        "mining", broker=settings.broker_url, include=["mining_server.worker.tasks"]
    )
    app.conf.update(
        task_serializer=CelerySerializer.JSON.value,
        accept_content=[CelerySerializer.JSON.value],
        result_serializer=CelerySerializer.JSON.value,
        task_ignore_result=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=settings.worker_prefetch_multiplier,
        worker_enable_remote_control=False,
        worker_send_task_events=False,
        task_send_sent_event=False,
        worker_cancel_long_running_tasks_on_connection_loss=True,
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=settings.broker_connect_timeout_seconds,
        broker_transport_options=transport_options,
        broker_pool_limit=settings.broker_pool_limit,
        task_publish_retry=False,
        task_default_delivery_mode=CeleryDeliveryMode.PERSISTENT.value,
        beat_schedule=schedule,
    )
    return app
