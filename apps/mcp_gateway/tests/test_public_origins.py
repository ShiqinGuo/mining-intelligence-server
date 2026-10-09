from http import HTTPStatus
from typing import TypedDict

import httpx
import pytest
from mcp.types import JSONRPCRequest
from pydantic import ValidationError
from pydantic_settings import SettingsError

from mining_gateway.app import create_app
from mining_gateway.settings import GatewaySettings


@pytest.mark.parametrize(
    "origin",
    [
        "http://mcp.example.com",
        "https://user@mcp.example.com",
        "https://user:password@mcp.example.com",
        "https://mcp.example.com/path",
        "https://mcp.example.com/path/..",
        "https://@mcp.example.com",
        "https://mcp.example.com?query=value",
        "https://mcp.example.com?",
        "https://mcp.example.com#fragment",
        "https://*.example.com",
        "invalid",
    ],
)
def test_public_origins_reject_invalid_explicit_configuration(origin: str):
    with pytest.raises(ValidationError):
        GatewaySettings(
            service_token="test-service-token-long-enough", public_origins=[origin]
        )


def test_public_origins_environment_requires_a_json_list(monkeypatch):
    monkeypatch.setenv("MINING_PUBLIC_ORIGINS", '["https://mcp.example.com"]')
    settings = GatewaySettings(service_token="test-service-token-long-enough")
    assert str(settings.public_origins[0]) == "https://mcp.example.com/"
    monkeypatch.setenv("MINING_PUBLIC_ORIGINS", "")
    with pytest.raises(SettingsError):
        GatewaySettings(service_token="test-service-token-long-enough")


@pytest.mark.parametrize("path", ["news", "documents", "market"])
async def test_public_origins_preserve_authentication_and_exact_allowlists(path: str):
    RequestMetadata = TypedDict(
        "RequestMetadata",
        {
            "io.modelcontextprotocol/protocolVersion": str,
            "io.modelcontextprotocol/clientCapabilities": dict[str, str],
        },
    )

    class ToolListParameters(TypedDict):
        _meta: RequestMetadata

    metadata: RequestMetadata = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    parameters: ToolListParameters = {"_meta": metadata}
    async with httpx.AsyncClient(base_url="http://backend") as backend:
        app = create_app(
            GatewaySettings(
                service_token="test-service-token-long-enough",
                public_origins=[
                    "https://mcp.example.com",
                    "https://other.example.com:8443",
                ],
            ),
            backend,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                base_url="https://mcp.example.com",
                transport=httpx.ASGITransport(app=app),
            ) as client,
        ):
            endpoint = f"/mcp/{path}/"
            content = JSONRPCRequest(
                jsonrpc="2.0", id=1, method="tools/list", params=parameters
            ).model_dump_json()
            headers = [
                ("Authorization", "Bearer test-service-token-long-enough"),
                ("Content-Type", "application/json"),
                ("Accept", "application/json, text/event-stream"),
                ("MCP-Protocol-Version", "2026-07-28"),
                ("MCP-Method", "tools/list"),
            ]
            assert (
                await client.post(endpoint, content=content)
            ).status_code == HTTPStatus.UNAUTHORIZED
            for host in [
                "mcp.example.com",
                "mcp.example.com:443",
                "other.example.com:8443",
            ]:
                origin = (
                    "https://other.example.com:8443"
                    if host.startswith("other")
                    else "https://mcp.example.com"
                )
                response = await client.post(
                    endpoint,
                    content=content,
                    headers=[*headers, ("Host", host), ("Origin", origin)],
                )
                assert response.status_code == HTTPStatus.OK, (host, response.text)
            for host in [
                "unknown.example.com",
                "mcp.example.com:8443",
                "other.example.com",
            ]:
                response = await client.post(
                    endpoint, content=content, headers=[*headers, ("Host", host)]
                )
                assert response.status_code == HTTPStatus.MISDIRECTED_REQUEST
            response = await client.post(
                endpoint,
                content=content,
                headers=[*headers, ("Origin", "https://unknown.example.com")],
            )
            assert response.status_code == HTTPStatus.FORBIDDEN
