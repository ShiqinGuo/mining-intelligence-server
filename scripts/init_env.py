import os
import secrets
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet


class EnvironmentKey(StrEnum):
    DATABASE_PASSWORD = "MINING_DATABASE_PASSWORD"
    BROKER_PASSWORD = "MINING_BROKER_PASSWORD"
    ADMIN_TOKEN = "MINING_ADMIN_TOKEN"
    SERVICE_TOKEN = "MINING_SERVICE_TOKEN"
    MASTER_KEY = "MINING_MASTER_KEY"
    OAUTH_HOST_ID = "MINING_OAUTH_HOST_ID"


@dataclass(frozen=True)
class EnvironmentDefaults:
    password_bytes: int = 24
    token_bytes: int = 32
    private_file_mode: int = 0o600


def main() -> None:
    destination = Path(__file__).resolve().parent.parent / ".env"
    defaults = EnvironmentDefaults()
    lines = [
        f"{EnvironmentKey.DATABASE_PASSWORD}={secrets.token_urlsafe(defaults.password_bytes)}",
        f"{EnvironmentKey.BROKER_PASSWORD}={secrets.token_urlsafe(defaults.password_bytes)}",
        f"{EnvironmentKey.ADMIN_TOKEN}={secrets.token_urlsafe(defaults.token_bytes)}",
        f"{EnvironmentKey.SERVICE_TOKEN}={secrets.token_urlsafe(defaults.token_bytes)}",
        f"{EnvironmentKey.MASTER_KEY}={Fernet.generate_key().decode()}",
        f"{EnvironmentKey.OAUTH_HOST_ID}=urn:uuid:{uuid4()}",
    ]
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            defaults.private_file_mode,
        )
    except FileExistsError:
        print(
            "Existing .env preserved; credentials and host identity were not changed."
        )
        return
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(
        "Created local .env. Keep this file private and retain it with your deployment backup."
    )


if __name__ == "__main__":
    main()
