import base64
import hashlib
import secrets
from datetime import timedelta
from http import HTTPStatus
from typing import TypedDict
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from cryptography.fernet import Fernet
from mining_contracts.domain.core import ErrorCode
from pydantic import SecretStr, ValidationError

from mining_server.domain.auth import (
    AuthorizationAttempt,
    CatalogResponse,
    CatalogVisibility,
    Credentials,
    IdentityClaims,
    JwkSet,
    JwtHeader,
    OAuthCallback,
    OAuthClientRegistration,
    OAuthDiscovery,
    OAuthGrantType,
    OAuthLimits,
    OAuthResponseType,
    OAuthScope,
    ProofKeyMethod,
    SecretSerialization,
    SigningAlgorithm,
    TokenResponse,
    utcnow,
    validate_host_id,
)
from mining_server.domain.core import fail
from mining_server.domain.model import ModelCatalog, ModelCatalogItem

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN_ENDPOINT = f"{ISSUER}/api/accounts/oauth/token"
OAUTH_LIMITS = OAuthLimits()


class AuthorizationParameters(TypedDict, total=False):
    client_id: str
    ext_agent_host_id: str
    response_type: OAuthResponseType
    redirect_uri: str
    scope: str
    resource: str
    state: str
    nonce: str
    code_challenge_method: ProofKeyMethod
    code_challenge: str
    agent_name_hint: str


class JwtValidationOptions(TypedDict):
    require: list[str]


class AuthorizationCodeForm(TypedDict):
    grant_type: OAuthGrantType
    client_id: str
    code: str
    code_verifier: str
    redirect_uri: str
    resource: str


class RefreshForm(TypedDict):
    grant_type: OAuthGrantType
    client_id: str
    refresh_token: str
    resource: str


class RevocationForm(TypedDict):
    token: str
    token_type_hint: OAuthGrantType
    client_id: str


class CredentialCipher:
    def __init__(self, key: str):
        try:
            self.cipher = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeError) as error:
            raise ValueError(
                "master_key must be a Fernet URL-safe base64 encoded 32-byte key"
            ) from error

    def encrypt(self, credentials: Credentials) -> bytes:
        return self.cipher.encrypt(
            credentials.model_dump_json(context=SecretSerialization.REVEAL).encode()
        )

    def decrypt(self, ciphertext: bytes) -> Credentials:
        return Credentials.model_validate_json(self.cipher.decrypt(ciphertext))


def create_attempt(
    app_name: str,
    host_id: str,
    redirect_uri: str,
    client_id: str,
    expected_subject: str | None = None,
) -> AuthorizationAttempt:
    validate_host_id(host_id)
    parsed = urlsplit(redirect_uri)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.path != "/auth/callback"
        or parsed.query
        or parsed.fragment
    ):
        raise fail(
            ErrorCode.INVALID_INPUT,
            "OAuth callback must be an exact 127.0.0.1 HTTP loopback URI",
        )
    return AuthorizationAttempt(
        app_name=app_name,
        host_id=host_id,
        redirect_uri=redirect_uri,
        client_id=client_id,
        state=SecretStr(secrets.token_urlsafe(OAUTH_LIMITS.state_entropy_bytes)),
        nonce=SecretStr(secrets.token_urlsafe(OAUTH_LIMITS.state_entropy_bytes)),
        verifier=SecretStr(secrets.token_urlsafe(OAUTH_LIMITS.verifier_entropy_bytes)),
        expected_subject=expected_subject,
        expires_at=utcnow() + timedelta(seconds=OAUTH_LIMITS.attempt_seconds),
    )


def authorization_url(attempt: AuthorizationAttempt) -> str:
    challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(attempt.verifier.get_secret_value().encode()).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    parameters: AuthorizationParameters = {
        "client_id": attempt.client_id,
        "ext_agent_host_id": attempt.host_id,
        "response_type": OAuthResponseType.CODE,
        "redirect_uri": attempt.redirect_uri,
        "scope": " ".join(OAuthScope),
        "resource": RESOURCE,
        "state": attempt.state.get_secret_value(),
        "nonce": attempt.nonce.get_secret_value(),
        "code_challenge_method": ProofKeyMethod.S256,
        "code_challenge": challenge,
    }
    if attempt.client_id == OAuthClientRegistration.DYNAMIC:
        parameters["agent_name_hint"] = attempt.app_name
    return f"{ISSUER}/api/accounts/authorize?{urlencode(parameters)}"


class OAuthClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.consumed_states: set[str] = set()

    async def validate_jwt(self, token: str, audience: str) -> IdentityClaims:
        response = await self.client.get(f"{ISSUER}/.well-known/jwks.json")
        response.raise_for_status()
        try:
            keys = JwkSet.model_validate_json(response.content)
            header = JwtHeader.model_validate(jwt.get_unverified_header(token))
            key = next((item for item in keys.keys if item.kid == header.kid), None)
            if key is None:
                raise fail(ErrorCode.WAITING_AUTH, "OIDC signing key was not found")
            options: JwtValidationOptions = {"require": ["exp", "iss", "aud", "sub"]}
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_json(key.model_dump_json(exclude_none=True)).key,
                algorithms=[SigningAlgorithm.RS256.value],
                issuer=ISSUER,
                audience=audience,
                leeway=OAUTH_LIMITS.clock_leeway_seconds,
                options=options,
            )
            return IdentityClaims.model_validate(claims)
        except (jwt.PyJWTError, ValidationError) as error:
            raise fail(
                ErrorCode.WAITING_AUTH, "OIDC token verification failed"
            ) from error

    async def validate_registration(self, credentials: Credentials) -> None:
        identity = await self.validate_jwt(
            credentials.id_token.get_secret_value(), credentials.client_id
        )
        access = await self.validate_jwt(
            credentials.access_token.get_secret_value(), RESOURCE
        )
        required = {
            OAuthScope.OPENID,
            OAuthScope.OFFLINE_ACCESS,
            OAuthScope.RESOURCE_INVOKE,
            OAuthScope.CHATGPT_DIRECT,
        }
        if (
            identity.sub != credentials.subject
            or access.sub != credentials.subject
            or access.client_id != credentials.client_id
        ):
            raise fail(
                ErrorCode.WAITING_AUTH,
                "OAuth account or registration identity mismatch",
            )
        if access.scope is None or not required.issubset(access.scope.split()):
            raise fail(
                ErrorCode.WAITING_AUTH,
                "OAuth access token does not grant ChatGPT plan permission",
            )

    async def exchange(
        self, attempt: AuthorizationAttempt, callback: OAuthCallback
    ) -> Credentials:
        state = attempt.state.get_secret_value()
        if state in self.consumed_states or utcnow() >= attempt.expires_at:
            raise fail(
                ErrorCode.WAITING_AUTH, "OAuth attempt expired or was already consumed"
            )
        if not secrets.compare_digest(callback.state, state):
            raise fail(ErrorCode.WAITING_AUTH, "OAuth state mismatch")
        self.consumed_states.add(state)
        if callback.error:
            raise fail(ErrorCode.WAITING_AUTH, "OAuth authorization was denied")
        client_id = callback.client_id or attempt.client_id
        if client_id == OAuthClientRegistration.DYNAMIC or (
            attempt.client_id != OAuthClientRegistration.DYNAMIC
            and client_id != attempt.client_id
        ):
            raise fail(ErrorCode.WAITING_AUTH, "OAuth issued client ID mismatch")
        if callback.code is None:
            raise fail(
                ErrorCode.WAITING_AUTH, "OAuth callback has no authorization code"
            )
        form: AuthorizationCodeForm = {
            "grant_type": OAuthGrantType.AUTHORIZATION_CODE,
            "client_id": client_id,
            "code": callback.code.get_secret_value(),
            "code_verifier": attempt.verifier.get_secret_value(),
            "redirect_uri": attempt.redirect_uri,
            "resource": RESOURCE,
        }
        response = await self.client.post(TOKEN_ENDPOINT, data=form)
        response.raise_for_status()
        data = TokenResponse.model_validate_json(response.content)
        identity = await self.validate_jwt(data.id_token.get_secret_value(), client_id)
        if identity.nonce != attempt.nonce.get_secret_value() or (
            attempt.expected_subject is not None
            and identity.sub != attempt.expected_subject
        ):
            await self.revoke_token(data.refresh_token.get_secret_value(), client_id)
            raise fail(ErrorCode.WAITING_AUTH, "OAuth identity or nonce mismatch")
        credentials = self._credentials(data, client_id, identity)
        await self.validate_registration(credentials)
        return credentials

    def _credentials(
        self, response: TokenResponse, client_id: str, identity: IdentityClaims
    ) -> Credentials:
        earliest = response.earliest_refresh_at
        return Credentials(
            subject=identity.sub,
            client_id=client_id,
            email=identity.email,
            access_token=response.access_token,
            refresh_token=response.refresh_token,
            id_token=response.id_token,
            scopes=response.scope.split(),
            expires_at=utcnow() + timedelta(seconds=response.expires_in),
            earliest_refresh_at=earliest,
        )

    async def refresh(self, credentials: Credentials) -> Credentials:
        form: RefreshForm = {
            "grant_type": OAuthGrantType.REFRESH_TOKEN,
            "client_id": credentials.client_id,
            "refresh_token": credentials.refresh_token.get_secret_value(),
            "resource": RESOURCE,
        }
        try:
            response = await self.client.post(
                TOKEN_ENDPOINT,
                data=form,
            )
        except httpx.TransportError as error:
            raise fail(
                ErrorCode.UNKNOWN_RESULT,
                "OAuth refresh outcome is unknown; reauthorization is required",
            ) from error
        if response.status_code in {
            HTTPStatus.BAD_REQUEST,
            HTTPStatus.UNAUTHORIZED,
            HTTPStatus.FORBIDDEN,
        }:
            raise fail(ErrorCode.WAITING_AUTH, "OAuth refresh grant was rejected")
        if response.status_code != HTTPStatus.OK:
            raise fail(
                ErrorCode.UNKNOWN_RESULT,
                "OAuth refresh did not return a verified replacement",
            )
        data = TokenResponse.model_validate_json(response.content)
        identity = await self.validate_jwt(
            data.id_token.get_secret_value(), credentials.client_id
        )
        if identity.sub != credentials.subject:
            raise fail(
                ErrorCode.WAITING_AUTH, "OAuth refresh changed the selected account"
            )
        replacement = self._credentials(data, credentials.client_id, identity)
        await self.validate_registration(replacement)
        return replacement

    async def catalog(self, credentials: Credentials) -> ModelCatalog:
        response = await self.client.get(
            f"{RESOURCE}/models",
            headers=httpx.Headers(
                [
                    (
                        "Authorization",
                        f"Bearer {credentials.access_token.get_secret_value()}",
                    )
                ]
            ),
        )
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            raise fail(ErrorCode.WAITING_AUTH, "Model authorization is required")
        response.raise_for_status()
        data = CatalogResponse.model_validate_json(response.content)
        return ModelCatalog(
            models=[
                ModelCatalogItem(slug=item.slug, display_name=item.display_name)
                for item in data.models
                if item.visibility == CatalogVisibility.LIST
            ]
        )

    async def revoke_token(self, token: str, client_id: str) -> None:
        discovery = await self.client.get(f"{ISSUER}/.well-known/openid-configuration")
        discovery.raise_for_status()
        endpoint = OAuthDiscovery.model_validate_json(
            discovery.content
        ).revocation_endpoint
        form: RevocationForm = {
            "token": token,
            "token_type_hint": OAuthGrantType.REFRESH_TOKEN,
            "client_id": client_id,
        }
        response = await self.client.post(endpoint, data=form)
        response.raise_for_status()
