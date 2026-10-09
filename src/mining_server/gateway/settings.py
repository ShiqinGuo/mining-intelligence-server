from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mining_server.domain.http import HttpScheme


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MINING_", extra="ignore")
    service_token: SecretStr = Field(min_length=20)
    backend_url: str = "http://mining-backend:8000"
    request_timeout_seconds: float = Field(default=30, gt=0, le=300)

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
