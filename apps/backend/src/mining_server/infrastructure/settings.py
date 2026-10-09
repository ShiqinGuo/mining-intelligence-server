from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from mining_server.domain.model import (
    ModelChannel,
    OpenAIProtocol,
    validate_openai_base_url,
)
from mining_server.domain.tasks import TaskExecutionDefaults


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MINING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: str = Field(
        default="postgresql+asyncpg://mining:mining@localhost:5432/mining", repr=False
    )
    broker_url: str = Field(default="amqp://mining:mining@localhost:5672//", repr=False)
    admin_token: SecretStr = Field(min_length=20)
    service_token: SecretStr = Field(min_length=20)
    master_key: SecretStr = Field(min_length=32)
    data_dir: Path = Path("/data/mining")
    backend_url: str = "http://mining-backend:8000"
    lease_seconds: int = Field(default=120, ge=15, le=3600)
    outbox_interval_seconds: float = Field(default=2, gt=0, le=60)
    scheduler_interval_seconds: int = Field(default=60, ge=5, le=3600)
    health_probe_timeout_seconds: float = Field(default=3, ge=1, le=4)
    health_beat_interval_seconds: int = Field(default=10, ge=5, le=30)
    health_beat_ttl_seconds: int = Field(default=90, ge=30, le=180)
    worker_concurrency: int = Field(default=2, ge=1, le=16)
    task_defaults: ClassVar[TaskExecutionDefaults] = TaskExecutionDefaults()
    outbox_claim_seconds: int = Field(default=30, ge=1)
    health_poll_seconds: float = Field(default=0.05, gt=0)
    publisher_stale_seconds: float = Field(default=30, gt=0)
    publisher_stale_multiplier: int = Field(default=3, ge=1)
    process_shutdown_seconds: float = Field(default=20, gt=0)
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_host: str = "0.0.0.0"
    broker_socket_timeout_seconds: int = Field(default=2, ge=1)
    broker_connect_timeout_seconds: int = Field(default=3, ge=1)
    broker_pool_limit: int = Field(default=2, ge=1)
    worker_prefetch_multiplier: int = Field(default=1, ge=1)
    model_catalog_timeout_seconds: float = Field(default=10, gt=0)
    model_catalog_max_bytes: int = Field(default=1048576, ge=1)
    network_chunk_bytes: int = Field(default=65536, ge=1)
    request_timeout_seconds: float = Field(default=30, gt=0, le=300)
    model_read_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    model_connect_timeout_seconds: float = Field(default=30, gt=0, le=300)
    model_write_timeout_seconds: float = Field(default=30, gt=0, le=300)
    model_pool_timeout_seconds: float = Field(default=30, gt=0, le=300)
    model_name: str = Field(default="gpt-6.1-sol", min_length=1, max_length=100)
    model_channel: ModelChannel = ModelChannel.OPENAI_COMPATIBLE
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_protocol: OpenAIProtocol = OpenAIProtocol.CHAT_COMPLETIONS
    oauth_app_name: str = Field(default="Mining Server", min_length=1, max_length=100)
    oauth_client_id: str = Field(
        default="dynamic_agent_client", min_length=1, max_length=300
    )
    oauth_host_id: str | None = None

    @field_validator("openai_api_key")
    @classmethod
    def valid_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and (
            not value.get_secret_value()
            or any(character.isspace() for character in value.get_secret_value())
        ):
            raise ValueError("OpenAI API key must be nonempty without whitespace")
        return value

    @field_validator("openai_base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        return validate_openai_base_url(value)

    @field_validator("master_key")
    @classmethod
    def valid_master_key(cls, value: SecretStr) -> SecretStr:
        try:
            Fernet(value.get_secret_value().encode("ascii"))
        except (ValueError, UnicodeError) as error:
            raise ValueError("master_key must be a Fernet key") from error
        return value

    @field_validator("oauth_host_id")
    @classmethod
    def valid_host_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("urn:uuid:"):
            raise ValueError("oauth_host_id must be a UUID URN")
        UUID(value.removeprefix("urn:uuid:"))
        return value

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: str) -> str:
        try:
            parsed = make_url(value)
        except (ArgumentError, ValueError) as error:
            raise ValueError("Invalid database_url") from error
        if parsed.drivername != "postgresql+asyncpg":
            raise ValueError("database_url must use postgresql+asyncpg")
        if not parsed.host and "host" not in parsed.query:
            raise ValueError("database_url requires a host")
        if parsed.host and any(character.isspace() for character in parsed.host):
            raise ValueError("database_url host contains whitespace")
        if not parsed.database or not parsed.database.strip():
            raise ValueError("database_url requires a database")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("database_url port is outside its valid range")
        return value

    @field_validator("broker_url")
    @classmethod
    def rabbitmq_only(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as error:
            raise ValueError("Invalid broker_url") from error
        if parsed.scheme != "amqp":
            raise ValueError("broker_url must use amqp")
        if not parsed.hostname or any(
            character.isspace() for character in parsed.hostname
        ):
            raise ValueError("broker_url requires a valid host")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("broker_url port is outside its valid range")
        return value

    @field_validator("model_name", "oauth_app_name", "oauth_client_id")
    @classmethod
    def nonempty_identifier(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError(
                "Configuration identifiers must be nonempty without surrounding whitespace"
            )
        return value

    @model_validator(mode="after")
    def independent_credentials(self):
        if self.admin_token.get_secret_value() == self.service_token.get_secret_value():
            raise ValueError("Administrator and service tokens must be distinct")
        if (
            self.openai_api_key is not None
            and self.openai_api_key.get_secret_value() in self.openai_base_url
        ):
            raise ValueError("OpenAI API key must not appear in the base URL")
        return self
