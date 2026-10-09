from typing import Annotated
from urllib.parse import urlsplit

from mining_contracts.domain.http import HttpScheme
from pydantic import BeforeValidator, Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def validate_public_origin_input(value: str | HttpUrl) -> str | HttpUrl:
    parts = urlsplit(str(value))
    if (
        parts.path not in {"", "/"}
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("Public origins must have no credentials or non-root path")
    return value


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MINING_", extra="ignore")
    service_token: SecretStr = Field(min_length=20)
    backend_url: str = "http://mining-backend:8000"
    request_timeout_seconds: float = Field(default=30, gt=0, le=300)
    public_origins: list[
        Annotated[HttpUrl, BeforeValidator(validate_public_origin_input)]
    ] = Field(default_factory=list)

    @field_validator("public_origins")
    @classmethod
    def validate_public_origins(cls, value: list[HttpUrl]) -> list[HttpUrl]:
        for origin in value:
            if (
                origin.scheme != HttpScheme.HTTPS
                or origin.username is not None
                or origin.password is not None
                or origin.query is not None
                or origin.fragment is not None
                or origin.path not in {None, "/"}
                or "*" in origin.host
            ):
                raise ValueError(
                    "Public origins must be exact HTTPS origins without credentials"
                )
        return value

    @field_validator("backend_url")
    @classmethod
    def validate_backend_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if (
            parts.scheme not in {HttpScheme.HTTP, HttpScheme.HTTPS}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Backend URL must be an HTTP origin without credentials")
        return value
