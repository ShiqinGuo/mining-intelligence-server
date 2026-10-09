import asyncio
import sys

import httpx

from mining_server.application.auth import AuthService
from mining_server.application.model_channels import ModelChannelsService
from mining_server.application.tasks import TaskService
from mining_server.domain.auth import OAuthLimits, RegistrationImport
from mining_server.domain.core import DomainError
from mining_server.domain.model import ModelChannel
from mining_server.infrastructure.auth.oauth import OAuthClient
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.settings import Settings


async def import_credentials() -> None:
    limits = OAuthLimits()
    raw = await asyncio.to_thread(sys.stdin.buffer.read, limits.handoff_bytes + 1)
    if len(raw) > limits.handoff_bytes:
        raise ValueError("Credential handoff exceeds its size limit")
    registration = RegistrationImport.model_validate_json(raw)
    settings = Settings()
    database = Database(settings.database_url)
    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, trust_env=False
        ) as client:
            receipt = await AuthService(
                database, settings, OAuthClient(client)
            ).import_registration(registration)
            tasks = TaskService(database, settings)
            await tasks.recover_waiting_auth(
                ModelChannelsService(database, settings, tasks).check_for_recovery,
                channel=ModelChannel.CHATGPT_SUBSCRIPTION,
                immediate=True,
            )
            print(receipt.model_dump_json())
    finally:
        await database.close()


if __name__ == "__main__":
    try:
        asyncio.run(import_credentials())
    except (DomainError, ValueError, OSError, httpx.HTTPError):
        print(
            "Credential import failed. Check backend installation state and transport.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
