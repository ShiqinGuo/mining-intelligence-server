from datetime import timedelta
from typing import TypedDict
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mining_server.domain.auth import Credentials, JwkSet, RsaJwk, RsaPublicKey, utcnow
from mining_server.domain.core import DomainError, ErrorCode
from mining_server.infrastructure.auth.oauth import ISSUER, RESOURCE, OAuthClient


class SignedClaims(TypedDict, total=False):
    iss: str
    sub: str
    aud: str
    exp: int
    client_id: str
    scope: str


class SigningHeader(TypedDict):
    kid: str


@pytest.mark.parametrize(
    "access_subject,scope,audience,valid",
    [
        (
            "account",
            "openid offline_access resource.invoke chatgpt.tokens.use.direct",
            RESOURCE,
            True,
        ),
        (
            "other",
            "openid offline_access resource.invoke chatgpt.tokens.use.direct",
            RESOURCE,
            False,
        ),
        ("account", "openid offline_access resource.invoke", RESOURCE, False),
        (
            "account",
            "openid offline_access resource.invoke chatgpt.tokens.use.direct",
            "wrong",
            False,
        ),
    ],
)
async def test_backend_import_verifies_signed_identity_scope_and_audience(
    access_subject, scope, audience, valid
):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    parsed_key = RsaPublicKey.model_validate_json(
        jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())
    )
    public = RsaJwk(kty=parsed_key.kty, kid="test-key", n=parsed_key.n, e=parsed_key.e)
    expires = int((utcnow() + timedelta(hours=1)).timestamp())
    identity_claims: SignedClaims = {
        "iss": ISSUER,
        "sub": "account",
        "aud": "oaiapp_test",
        "exp": expires,
    }
    access_claims: SignedClaims = {
        "iss": ISSUER,
        "sub": access_subject,
        "aud": audience,
        "exp": expires,
        "client_id": "oaiapp_test",
        "scope": scope,
    }
    headers: SigningHeader = {"kid": "test-key"}
    identity = jwt.encode(
        identity_claims,
        private,
        algorithm="RS256",
        headers=headers,
    )
    access = jwt.encode(
        access_claims,
        private,
        algorithm="RS256",
        headers=headers,
    )
    credentials = Credentials(
        subject="account",
        client_id="oaiapp_test",
        access_token=access,
        refresh_token=f"test-refresh-{uuid4()}",
        id_token=identity,
        scopes=[
            "openid",
            "offline_access",
            "resource.invoke",
            "chatgpt.tokens.use.direct",
        ],
        expires_at=utcnow() + timedelta(hours=1),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=JwkSet(keys=[public]).model_dump_json()
            )
        )
    ) as client:
        oauth = OAuthClient(client)
        if valid:
            await oauth.validate_registration(credentials)
        else:
            with pytest.raises(DomainError) as error:
                await oauth.validate_registration(credentials)
            assert error.value.code == ErrorCode.WAITING_AUTH
