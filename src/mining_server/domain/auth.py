from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from mining_server.domain.core import Contract
from mining_server.domain.model import ModelCatalogItem


class ConnectionStatus(StrEnum):
    DISCONNECTED = "disconnected"
    AWAITING_AUTH = "awaiting_auth"
    CONNECTED = "connected"
    REFRESHING = "refreshing"
    REAUTH_REQUIRED = "reauth_required"
    UNAVAILABLE = "unavailable"


class RefreshState(StrEnum):
    NONE = "none"
    CLAIMED = "claimed"
    COMPLETE = "complete"
    UNKNOWN = "unknown"


class SecretSerialization(StrEnum):
    REVEAL = "reveal"


class OAuthTokenType(StrEnum):
    BEARER = "Bearer"
    LOWERCASE_BEARER = "bearer"


class OAuthGrantType(StrEnum):
    AUTHORIZATION_CODE = "authorization_code"
    REFRESH_TOKEN = "refresh_token"


class OAuthResponseType(StrEnum):
    CODE = "code"


class ProofKeyMethod(StrEnum):
    S256 = "S256"


class OAuthScope(StrEnum):
    OPENID = "openid"
    PROFILE = "profile"
    EMAIL = "email"
    OFFLINE_ACCESS = "offline_access"
    RESOURCE_INVOKE = "resource.invoke"
    CHATGPT_DIRECT = "chatgpt.tokens.use.direct"


class OAuthClientRegistration(StrEnum):
    DYNAMIC = "dynamic_agent_client"


class SigningAlgorithm(StrEnum):
    RS256 = "RS256"


class KeyType(StrEnum):
    RSA = "RSA"


class KeyUsage(StrEnum):
    SIGNATURE = "sig"


class CatalogVisibility(StrEnum):
    LIST = "list"
    HIDDEN = "hide"


class OAuthWireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class TokenResponse(OAuthWireModel):
    access_token: SecretStr
    refresh_token: SecretStr
    id_token: SecretStr
    scope: str
    token_type: OAuthTokenType
    expires_in: int = Field(gt=0)
    earliest_refresh_at: AwareDatetime | None = None


class IdentityClaims(OAuthWireModel):
    sub: str = Field(min_length=1)
    exp: int
    iss: str
    aud: str | list[str]
    nonce: str | None = None
    email: str | None = None
    client_id: str | None = None
    scope: str | None = None


class JwtHeader(OAuthWireModel):
    alg: SigningAlgorithm
    kid: str = Field(min_length=1)
    typ: str | None = None


class RsaPublicKey(OAuthWireModel):
    kty: KeyType
    n: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    e: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")


class RsaJwk(RsaPublicKey):
    kid: str = Field(min_length=1)
    alg: SigningAlgorithm | None = None
    use: KeyUsage | None = None


class JwkSet(OAuthWireModel):
    keys: list[RsaJwk]


class OAuthDiscovery(OAuthWireModel):
    revocation_endpoint: str

    @field_validator("revocation_endpoint")
    @classmethod
    def official_endpoint(cls, value: str) -> str:
        from urllib.parse import urlsplit

        endpoint = urlsplit(value)
        if endpoint.scheme != "https" or endpoint.netloc != "auth.openai.com":
            raise ValueError("Unexpected OAuth revocation endpoint")
        return value


class CatalogEntry(OAuthWireModel):
    slug: str
    display_name: str
    visibility: CatalogVisibility


class CatalogResponse(OAuthWireModel):
    models: list[CatalogEntry]


class OAuthLimits(Contract):
    state_entropy_bytes: int = Field(default=32, ge=32)
    verifier_entropy_bytes: int = Field(default=64, ge=32)
    attempt_seconds: int = Field(default=600, ge=1)
    installation_seconds: int = Field(default=900, ge=1)
    clock_leeway_seconds: int = Field(default=5, ge=0)
    refresh_margin_seconds: int = Field(default=60, ge=1)
    refresh_claim_seconds: int = Field(default=120, ge=1)
    callback_read_seconds: int = Field(default=5, ge=1)
    callback_header_bytes: int = Field(default=16384, ge=1024)
    http_timeout_seconds: int = Field(default=30, ge=1)
    handoff_bytes: int = Field(default=128 * 1024, ge=1024)


class Credentials(Contract):
    issuer: str = "https://auth.openai.com"
    subject: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    email: str | None = None
    access_token: SecretStr
    refresh_token: SecretStr
    id_token: SecretStr
    scopes: list[str]
    expires_at: AwareDatetime
    earliest_refresh_at: AwareDatetime | None = None

    @field_serializer("access_token", "refresh_token", "id_token")
    def serialize_token(self, value: SecretStr, info: SerializationInfo) -> str:
        return (
            value.get_secret_value()
            if info.context == SecretSerialization.REVEAL
            else "**********"
        )

    @model_validator(mode="after")
    def registration_identity(self):
        if self.issuer != "https://auth.openai.com":
            raise ValueError("Unexpected OAuth issuer")
        if self.client_id == OAuthClientRegistration.DYNAMIC:
            raise ValueError("An issued OAuth client ID is required")
        required = {
            OAuthScope.OPENID,
            OAuthScope.OFFLINE_ACCESS,
            OAuthScope.RESOURCE_INVOKE,
            OAuthScope.CHATGPT_DIRECT,
        }
        if not required.issubset(self.scopes):
            raise ValueError("ChatGPT plan scopes are required")
        for token in (self.access_token, self.refresh_token, self.id_token):
            if not token.get_secret_value():
                raise ValueError("OAuth tokens cannot be empty")
        return self


class RegistrationImport(Contract):
    installation_id: UUID
    target_host_id: str
    app_name: str
    credentials: Credentials


class ConnectionView(Contract):
    id: UUID | None = None
    status: ConnectionStatus
    revision: int = Field(ge=0)
    app_name: str
    host_id: str | None
    subject: str | None = None
    email: str | None = None
    client_id: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None
    models: list[ModelCatalogItem] = Field(default_factory=list)


class InstallationRequest(Contract):
    expected_revision: int = Field(ge=0)


class InstallationView(Contract):
    installation_id: UUID
    target_host_id: str
    app_name: str
    client_id: str
    expected_subject: str | None = None
    expires_at: datetime
    helper_module: str = "mining_server.infrastructure.auth.local_helper"
    import_module: str = "mining_server.infrastructure.auth.import_helper"


class ConnectionAction(Contract):
    expected_revision: int = Field(ge=1)


class OAuthCallback(Contract):
    state: str
    code: SecretStr | None = None
    client_id: str | None = None
    error: str | None = None
    scope: str | None = None


class AuthorizationAttempt(Contract):
    app_name: str
    host_id: str
    redirect_uri: str
    client_id: str
    state: SecretStr
    nonce: SecretStr
    verifier: SecretStr
    expected_subject: str | None = None
    expires_at: AwareDatetime


class ImportReceipt(Contract):
    id: UUID
    status: ConnectionStatus


def validate_host_id(value: str) -> str:
    if value.startswith("urn:uuid:"):
        identifier = UUID(value.removeprefix("urn:uuid:"))
        if identifier.version == 4 and value == f"urn:uuid:{identifier}":
            return value
    if (
        value.startswith(("urn:ietf:params:oauth:jwk-thumbprint:", "did:key:"))
        and value.rsplit(":", 1)[1]
    ):
        return value
    raise ValueError("Invalid OAuth host ID")


def utcnow() -> datetime:
    return datetime.now(UTC)
