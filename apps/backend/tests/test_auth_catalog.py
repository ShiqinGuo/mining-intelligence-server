from http import HTTPStatus

import httpx
import pytest
from mining_contracts.domain.http import HttpMethod
from pydantic import ValidationError

from mining_server.domain.auth import (
    CatalogEntry,
    CatalogResponse,
    CatalogVisibility,
    Credentials,
    OAuthScope,
    utcnow,
)
from mining_server.infrastructure.auth.oauth import OAuthClient


async def test_subscription_catalog_filters_verified_hide_value():
    entries = [
        CatalogEntry(
            slug=f"visible-{index}",
            display_name=f"Visible {index}",
            visibility=CatalogVisibility.LIST,
        )
        for index in range(7)
    ]
    entries.extend(
        CatalogEntry(
            slug=f"hidden-{index}",
            display_name=f"Hidden {index}",
            visibility=CatalogVisibility.HIDDEN,
        )
        for index in range(3)
    )
    payload = CatalogResponse(models=entries)
    credentials = Credentials(
        subject="test-account",
        client_id="issued-client",
        access_token="test-access",
        refresh_token="test-refresh",
        id_token="test-identity",
        scopes=[
            OAuthScope.OPENID,
            OAuthScope.OFFLINE_ACCESS,
            OAuthScope.RESOURCE_INVOKE,
            OAuthScope.CHATGPT_DIRECT,
        ],
        expires_at=utcnow(),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == HttpMethod.GET
        assert request.url.path == "/v1/models"
        return httpx.Response(HTTPStatus.OK, content=payload.model_dump_json())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        catalog = await OAuthClient(client).catalog(credentials)
    assert [entry.slug for entry in catalog.models] == [
        f"visible-{index}" for index in range(7)
    ]
    assert CatalogVisibility.HIDDEN.value == "hide"


@pytest.mark.parametrize("visibility", ["hidden", "unknown", ""])
def test_unverified_catalog_visibility_fails_at_boundary(visibility):
    with pytest.raises(ValidationError):
        CatalogEntry(slug="model", display_name="Model", visibility=visibility)
