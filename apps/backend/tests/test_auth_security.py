from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from mining_contracts.domain.core import ErrorCode

from mining_server.domain.auth import Credentials, OAuthCallback, utcnow
from mining_server.domain.core import DomainError
from mining_server.infrastructure.auth.oauth import (
    CredentialCipher,
    OAuthClient,
    authorization_url,
    create_attempt,
)


async def test_oauth_state_is_checked_before_token_endpoint():
    reached = []
    attempt = create_attempt(
        "Mining Server",
        f"urn:uuid:{uuid4()}",
        "http://127.0.0.1:12345/auth/callback",
        "dynamic_agent_client",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: reached.append(request))
    ) as client:
        with pytest.raises(DomainError) as error:
            await OAuthClient(client).exchange(
                attempt,
                OAuthCallback(state="wrong", code="code", client_id="oaiapp_test"),
            )
    assert error.value.code == ErrorCode.WAITING_AUTH
    assert reached == []
    assert "code_challenge_method=S256" in authorization_url(attempt)
    assert "agent_name_hint=Mining+Server" in authorization_url(attempt)


def test_credentials_are_encrypted_and_round_trip_without_secret_serialization():
    credentials = Credentials(
        subject="test-subject",
        client_id="oaiapp_test",
        access_token="test-access",
        refresh_token="test-refresh",
        id_token="test-id",
        scopes=[
            "openid",
            "profile",
            "email",
            "offline_access",
            "resource.invoke",
            "chatgpt.tokens.use.direct",
        ],
        expires_at=utcnow(),
    )
    cipher = CredentialCipher(Fernet.generate_key().decode())
    encrypted = cipher.encrypt(credentials)
    assert b"test-refresh" not in encrypted
    assert cipher.decrypt(encrypted) == credentials
    assert "test-refresh" not in credentials.model_dump_json()
