import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from mining_server.application.auth import AuthService
from mining_server.domain.auth import (
    ConnectionStatus,
    Credentials,
    InstallationRequest,
    RefreshState,
    RegistrationImport,
    utcnow,
)
from mining_server.domain.core import DomainError, ErrorCode, fail
from mining_server.infrastructure.auth.models import ModelConnection
from mining_server.infrastructure.database import Base, Database
from mining_server.infrastructure.settings import Settings


class ScriptedOAuth:
    def __init__(self):
        self.refreshes = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.unknown = False

    async def validate_registration(self, credentials):
        return None

    async def refresh(self, credentials):
        self.refreshes += 1
        self.started.set()
        await self.release.wait()
        if self.unknown:
            raise fail(ErrorCode.UNKNOWN_RESULT, "Scripted transport outcome unknown")
        return Credentials(
            issuer=credentials.issuer,
            subject=credentials.subject,
            client_id=credentials.client_id,
            email=credentials.email,
            access_token=credentials.access_token,
            refresh_token=credentials.refresh_token,
            id_token=credentials.id_token,
            scopes=credentials.scopes,
            expires_at=utcnow() + timedelta(hours=1),
            earliest_refresh_at=credentials.earliest_refresh_at,
        )


@pytest_asyncio.fixture
async def auth_service(tmp_path):
    url = os.environ.get("MINING_AUTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Dedicated PostgreSQL integration database is required")
    database = Database(url)
    schema = f"auth_test_{uuid4().hex}"
    async with database.engine.begin() as connection:
        await connection.execute(CreateSchema(schema))
        await connection.execution_options(schema_translate_map={None: schema})
        await connection.run_sync(Base.metadata.create_all)
    database.sessions = async_sessionmaker(
        database.engine.execution_options(schema_translate_map={None: schema}),
        expire_on_commit=False,
    )
    settings = Settings(
        database_url=url,
        admin_token="test-admin-token-123456",
        service_token="test-service-token-123456",
        master_key=Fernet.generate_key().decode(),
        oauth_host_id=f"urn:uuid:{uuid4()}",
        data_dir=tmp_path,
    )
    oauth = ScriptedOAuth()
    service = AuthService(database, settings, oauth)
    ticket = await service.install(InstallationRequest(expected_revision=0))
    credentials = Credentials(
        subject="test-account",
        client_id="oaiapp_test",
        access_token="access-test",
        refresh_token="refresh-test",
        id_token="identity-test",
        scopes=[
            "openid",
            "profile",
            "email",
            "offline_access",
            "resource.invoke",
            "chatgpt.tokens.use.direct",
        ],
        expires_at=utcnow() - timedelta(seconds=1),
    )
    await service.import_registration(
        RegistrationImport(
            installation_id=ticket.installation_id,
            target_host_id=ticket.target_host_id,
            app_name=ticket.app_name,
            credentials=credentials,
        )
    )
    try:
        yield service, oauth
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await database.close()


async def test_refresh_is_exclusive_across_independent_sessions(auth_service):
    service, oauth = auth_service
    first = asyncio.create_task(service.access_token())
    await oauth.started.wait()
    with pytest.raises(DomainError) as error:
        await service.access_token()
    assert error.value.code == ErrorCode.WAITING_AUTH
    oauth.release.set()
    assert await first == "access-test"
    assert await service.access_token() == "access-test"
    assert oauth.refreshes == 1
    assert (await service.status()).status == ConnectionStatus.CONNECTED


async def test_uncertain_refresh_is_persisted_and_never_replayed(auth_service):
    service, oauth = auth_service
    oauth.unknown = True
    oauth.release.set()
    with pytest.raises(DomainError):
        await service.access_token()
    with pytest.raises(DomainError) as error:
        await service.access_token()
    assert error.value.code == ErrorCode.WAITING_AUTH
    assert oauth.refreshes == 1
    async with service.database.sessions() as session:
        row = await session.get(ModelConnection, (await service.status()).id)
        assert row.refresh_state == RefreshState.UNKNOWN
        assert row.status == ConnectionStatus.REAUTH_REQUIRED
