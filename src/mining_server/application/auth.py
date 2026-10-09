from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy.dialects.postgresql import insert

from mining_server.domain.auth import (
    ConnectionAction,
    ConnectionStatus,
    ConnectionView,
    ImportReceipt,
    InstallationRequest,
    InstallationView,
    OAuthLimits,
    RefreshState,
    RegistrationImport,
    utcnow,
)
from mining_server.domain.core import DomainError, ErrorCode, fail
from mining_server.domain.model import ModelCatalog
from mining_server.infrastructure.auth.models import ModelConnection
from mining_server.infrastructure.auth.oauth import CredentialCipher, OAuthClient
from mining_server.infrastructure.auth.repository import AuthRepository
from mining_server.infrastructure.database import Database
from mining_server.infrastructure.settings import Settings


class AuthService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        oauth: OAuthClient,
        repository_factory: type[AuthRepository] = AuthRepository,
    ):
        self.database = database
        self.settings = settings
        self.oauth = oauth
        self.repositories = repository_factory
        self.limits = OAuthLimits()
        self.cipher = CredentialCipher(settings.master_key.get_secret_value())

    def view(self, row: ModelConnection | None) -> ConnectionView:
        if row is None:
            return ConnectionView(
                status=ConnectionStatus.DISCONNECTED,
                revision=0,
                app_name=self.settings.oauth_app_name,
                host_id=self.settings.oauth_host_id,
            )
        catalog = (
            ModelCatalog.model_validate_json(row.catalog_json).models
            if row.catalog_json
            else []
        )
        return ConnectionView(
            id=row.id,
            status=row.status,
            revision=row.revision,
            app_name=self.settings.oauth_app_name,
            host_id=self.settings.oauth_host_id,
            subject=row.subject,
            email=row.email,
            client_id=row.client_id,
            expires_at=row.expires_at,
            reason=row.reason,
            models=catalog,
        )

    async def status(self) -> ConnectionView:
        async with self.database.sessions() as session:
            return self.view(await self.repositories(session).current())

    async def install(self, request: InstallationRequest) -> InstallationView:
        if self.settings.oauth_host_id is None:
            raise fail(
                ErrorCode.WAITING_AUTH,
                "Configure the subscription host identity before installation",
            )
        async with self.database.sessions() as session, session.begin():
            identifier = uuid4()
            await session.execute(
                insert(ModelConnection)
                .values(
                    id=identifier,
                    slot=1,
                    status=ConnectionStatus.DISCONNECTED,
                    revision=0,
                    token_generation=0,
                    refresh_state=RefreshState.NONE,
                )
                .on_conflict_do_nothing(index_elements=[ModelConnection.slot])
            )
            row = await self.repositories(session).current(lock=True)
            if row.revision != request.expected_revision:
                raise fail(ErrorCode.CONFLICT, "Model connection revision changed")
            row.revision += 1
            row.token_generation += 1
            row.refresh_owner = None
            row.refresh_deadline = None
            row.refresh_state = RefreshState.NONE
            row.installation_id = uuid4()
            row.status = ConnectionStatus.AWAITING_AUTH
            row.installation_expires_at = utcnow() + timedelta(
                seconds=self.limits.installation_seconds
            )
            row.reason = None
            return InstallationView(
                installation_id=row.installation_id,
                target_host_id=self.settings.oauth_host_id,
                app_name=self.settings.oauth_app_name,
                client_id=row.client_id or self.settings.oauth_client_id,
                expected_subject=row.subject,
                expires_at=row.installation_expires_at,
            )

    async def import_registration(
        self, registration: RegistrationImport
    ) -> ImportReceipt:
        if (
            registration.target_host_id != self.settings.oauth_host_id
            or registration.app_name != self.settings.oauth_app_name
        ):
            raise fail(
                ErrorCode.WAITING_AUTH,
                "Credential handoff targets a different application or host",
            )
        await self.oauth.validate_registration(registration.credentials)
        async with self.database.sessions() as session, session.begin():
            row = await self.repositories(session).current(lock=True)
            if row is None or row.installation_id != registration.installation_id:
                raise fail(
                    ErrorCode.NOT_FOUND, "Model installation request was not found"
                )
            if row.status == ConnectionStatus.CONNECTED and row.encrypted_credentials:
                current = self.cipher.decrypt(row.encrypted_credentials)
                if current == registration.credentials:
                    return ImportReceipt(id=row.id, status=row.status)
                raise fail(
                    ErrorCode.CONFLICT,
                    "Installation already contains a different credential session",
                )
            if (
                row.status != ConnectionStatus.AWAITING_AUTH
                or row.installation_expires_at is None
                or utcnow() >= row.installation_expires_at
            ):
                raise fail(
                    ErrorCode.WAITING_AUTH,
                    "Model installation request expired or was not pending",
                )
            if (
                row.subject is not None
                and row.subject != registration.credentials.subject
            ):
                raise fail(
                    ErrorCode.WAITING_AUTH,
                    "Reauthorization changed the selected ChatGPT account",
                )
            if (
                row.client_id is not None
                and row.client_id != registration.credentials.client_id
            ):
                raise fail(
                    ErrorCode.WAITING_AUTH,
                    "Reauthorization changed the selected registration",
                )
            row.subject = registration.credentials.subject
            row.email = registration.credentials.email
            row.client_id = registration.credentials.client_id
            row.encrypted_credentials = self.cipher.encrypt(registration.credentials)
            row.expires_at = registration.credentials.expires_at
            row.token_generation += 1
            row.revision += 1
            row.status = ConnectionStatus.CONNECTED
            row.refresh_state = RefreshState.NONE
            row.refresh_owner = None
            row.refresh_deadline = None
            row.reason = None
            return ImportReceipt(id=row.id, status=row.status)

    async def access_token(self) -> str:
        if self.settings.oauth_host_id is None:
            raise fail(
                ErrorCode.WAITING_AUTH, "Subscription host identity is not configured"
            )
        owner = uuid4()
        async with self.database.sessions() as session, session.begin():
            row = await self.repositories(session).current(lock=True)
            if (
                row is None
                or row.encrypted_credentials is None
                or row.status
                not in {ConnectionStatus.CONNECTED, ConnectionStatus.REFRESHING}
            ):
                raise fail(
                    ErrorCode.WAITING_AUTH,
                    "Continue with ChatGPT to authorize model access",
                )
            credentials = self.cipher.decrypt(row.encrypted_credentials)
            if row.refresh_state == RefreshState.CLAIMED:
                if (
                    row.refresh_deadline is not None
                    and utcnow() >= row.refresh_deadline
                ):
                    row.status = ConnectionStatus.REAUTH_REQUIRED
                    row.refresh_state = RefreshState.UNKNOWN
                    row.reason = "refresh_outcome_unknown"
                rejected = True
            else:
                rejected = False
            if not rejected and credentials.expires_at > utcnow() + timedelta(
                seconds=self.limits.refresh_margin_seconds
            ):
                return credentials.access_token.get_secret_value()
            if (
                not rejected
                and credentials.earliest_refresh_at is not None
                and utcnow() < credentials.earliest_refresh_at
            ):
                raise fail(
                    ErrorCode.WAITING_AUTH,
                    "Credential cannot be refreshed before its allowed time",
                )
            if not rejected:
                generation = row.token_generation
                row.refresh_state = RefreshState.CLAIMED
                row.refresh_owner = owner
                row.refresh_deadline = utcnow() + timedelta(
                    seconds=self.limits.refresh_claim_seconds
                )
                row.status = ConnectionStatus.REFRESHING
        if rejected:
            raise fail(
                ErrorCode.WAITING_AUTH,
                "Credential refresh is already claimed or requires reauthorization",
            )
        try:
            replacement = await self.oauth.refresh(credentials)
        except BaseException as error:
            async with self.database.sessions() as session, session.begin():
                row = await self.repositories(session).current(lock=True)
                if (
                    row is not None
                    and row.refresh_owner == owner
                    and row.token_generation == generation
                ):
                    row.refresh_state = (
                        RefreshState.NONE
                        if isinstance(error, DomainError)
                        and error.code == ErrorCode.WAITING_AUTH
                        else RefreshState.UNKNOWN
                    )
                    row.status = ConnectionStatus.REAUTH_REQUIRED
                    row.reason = "refresh_failed_or_unknown"
            raise
        async with self.database.sessions() as session, session.begin():
            row = await self.repositories(session).current(lock=True)
            if (
                row is None
                or row.refresh_owner != owner
                or row.token_generation != generation
                or row.refresh_state != RefreshState.CLAIMED
            ):
                raise fail(
                    ErrorCode.WAITING_AUTH, "Credential refresh ownership changed"
                )
            row.encrypted_credentials = self.cipher.encrypt(replacement)
            row.expires_at = replacement.expires_at
            row.token_generation += 1
            row.revision += 1
            row.refresh_state = RefreshState.COMPLETE
            row.refresh_owner = None
            row.refresh_deadline = None
            row.status = ConnectionStatus.CONNECTED
            row.reason = None
        return replacement.access_token.get_secret_value()

    async def check(self, request: ConnectionAction) -> ConnectionView:
        async with self.database.sessions() as session:
            row = await self.repositories(session).current()
            if row is None or row.revision != request.expected_revision:
                raise fail(ErrorCode.CONFLICT, "Model connection revision changed")
        await self.access_token()
        async with self.database.sessions() as session:
            row = await self.repositories(session).current()
            if (
                row is None
                or row.status != ConnectionStatus.CONNECTED
                or row.encrypted_credentials is None
            ):
                raise fail(
                    ErrorCode.CONFLICT, "Model connection changed before catalog check"
                )
            generation = row.token_generation
            credentials = self.cipher.decrypt(row.encrypted_credentials)
        try:
            catalog = await self.oauth.catalog(credentials)
        except httpx.HTTPError as error:
            raise fail(
                ErrorCode.UPSTREAM_FAILURE, "Account model catalog request failed"
            ) from error
        async with self.database.sessions() as session, session.begin():
            row = await self.repositories(session).current(lock=True)
            if (
                row.token_generation != generation
                or row.status != ConnectionStatus.CONNECTED
            ):
                raise fail(
                    ErrorCode.CONFLICT, "Model connection changed during catalog check"
                )
            row.catalog_json = catalog.model_dump_json()
            row.revision += 1
            return self.view(row)

    async def disconnect(self, request: ConnectionAction) -> ConnectionView:
        async with self.database.sessions() as session, session.begin():
            row = await self.repositories(session).current(lock=True)
            if row is None or row.revision != request.expected_revision:
                raise fail(ErrorCode.CONFLICT, "Model connection revision changed")
            credentials = (
                self.cipher.decrypt(row.encrypted_credentials)
                if row.encrypted_credentials
                else None
            )
            row.status = ConnectionStatus.DISCONNECTED
            row.encrypted_credentials = None
            row.expires_at = None
            row.token_generation += 1
            row.revision += 1
            revision = row.revision
            row.reason = "remote_revocation_unconfirmed" if credentials else None
        if credentials:
            try:
                await self.oauth.revoke_token(
                    credentials.refresh_token.get_secret_value(), credentials.client_id
                )
            except (httpx.HTTPError, DomainError):
                return await self.status()
            async with self.database.sessions() as session, session.begin():
                row = await self.repositories(session).current(lock=True)
                if row.revision == revision:
                    row.reason = None
        return await self.status()
