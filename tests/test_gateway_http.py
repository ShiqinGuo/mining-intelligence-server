import httpx

from mining_server.domain.core import ErrorCode, ErrorDetails, ErrorResponse
from mining_server.domain.market import InstrumentList
from mining_server.gateway.app import create_app
from mining_server.gateway.settings import GatewaySettings


async def test_three_servers_have_independent_tools_and_structured_errors():
    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("price-instruments"):
            return httpx.Response(
                200, json=InstrumentList(instruments=[]).model_dump(mode="json")
            )
        return httpx.Response(
            404,
            json=ErrorResponse(
                code=ErrorCode.NO_QUOTE,
                message="No observed price",
                request_id="backend-request",
                details=ErrorDetails(retryable=False),
            ).model_dump(mode="json"),
        )

    async with httpx.AsyncClient(
        base_url="http://backend", transport=httpx.MockTransport(upstream)
    ) as client:
        app = create_app(
            GatewaySettings(service_token="test-service-token-long-enough"), client
        )
        news, documents, market = app.state.servers
        assert {tool.name for tool in await news.list_tools()} == {
            "search",
            "fetch_article",
            "get_task",
        }
        assert {tool.name for tool in await documents.list_tools()} == {
            "extract_resources",
            "get_extraction",
            "get_extraction_result",
        }
        assert {tool.name for tool in await market.list_tools()} == {
            "list_instruments",
            "get_price",
            "get_trend",
        }
        for server in (news, documents, market):
            for tool in await server.list_tools():
                assert tool.annotations is not None
                assert tool.annotations.read_only_hint is (
                    tool.name not in {"fetch_article", "extract_resources"}
                )
        success = await market.call_tool("list_instruments", {})
        assert not success.is_error
        assert success.structured_content["result"]["instruments"] == []
        error = await market.call_tool(
            "get_price", {"commodity": "missing", "date": "2026-10-08"}
        )
        assert error.is_error
        assert error.structured_content["error"]["code"] == "no_quote"
        assert error.structured_content["error"]["details"]["retryable"] is False


async def test_mcp_http_endpoint_requires_service_token_and_runs_all_lifespans():
    async with httpx.AsyncClient(
        base_url="http://backend",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=InstrumentList(instruments=[]).model_dump(mode="json")
            )
        ),
    ) as backend:
        app = create_app(
            GatewaySettings(service_token="test-service-token-long-enough"), backend
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                base_url="http://testserver", transport=httpx.ASGITransport(app=app)
            ) as client,
        ):
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.post("/mcp/market/")).status_code == 401
            headers = {
                "Authorization": "Bearer test-service-token-long-enough",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "MCP-Method": "tools/list",
            }
            response = await client.post(
                "/mcp/market/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        }
                    },
                },
            )
            assert response.status_code == 200
            assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
                "list_instruments",
                "get_price",
                "get_trend",
            }
            assert all(
                tool["annotations"]["readOnlyHint"] is True
                for tool in response.json()["result"]["tools"]
            )
