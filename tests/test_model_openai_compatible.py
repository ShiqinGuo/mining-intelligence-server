from http import HTTPStatus

import httpx
import pytest

from mining_server.domain.core import DomainError, ErrorCode
from mining_server.domain.model import (
    ChatRequest,
    JsonSchema,
    JsonSchemaType,
    ModelFunctionCallOutput,
    ModelFunctionTool,
    ModelImageDetail,
    ModelInputImage,
    ModelInputText,
    ModelMessage,
    ModelNamespaceTool,
    ModelRequest,
    ModelRole,
    ModelTransportLimits,
)
from mining_server.infrastructure.model.openai_compatible import (
    ChatCompletionsModelGateway,
)

RESPONSE = '{"id":"completion-1","choices":[{"finish_reason":"tool_calls","message":{"role":"assistant","content":"Reading","tool_calls":[{"id":"call-a","type":"function","function":{"name":"read_page","arguments":"{}"}},{"id":"call-b","type":"function","function":{"name":"read_page","arguments":"{}"}}]}}],"usage":{"prompt_tokens":7,"completion_tokens":9}}'


def request() -> ModelRequest:
    return ModelRequest(
        model="configured-model",
        input=[
            ModelMessage(role=ModelRole.DEVELOPER, content="Extract evidence"),
            ModelMessage(
                role=ModelRole.USER,
                content=[
                    ModelInputText(text="Read page"),
                    ModelInputImage(
                        image_url="data:image/png;base64,aGVsbG8=",
                        detail=ModelImageDetail.HIGH,
                    ),
                ],
            ),
        ],
        tools=[
            ModelNamespaceTool(
                tools=[
                    ModelFunctionTool(
                        name="read_page",
                        parameters=JsonSchema(type=JsonSchemaType.OBJECT),
                    )
                ]
            )
        ],
    )


async def test_image_namespace_and_parallel_tool_history_roundtrip():
    bodies: list[ChatRequest] = []

    def endpoint(incoming: httpx.Request) -> httpx.Response:
        assert incoming.url == "https://vendor.example/v1/chat/completions"
        assert incoming.headers["Authorization"] == "Bearer test-key"
        bodies.append(ChatRequest.model_validate_json(incoming.content))
        return httpx.Response(HTTPStatus.OK, content=RESPONSE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        gateway = ChatCompletionsModelGateway(
            client, "https://vendor.example/v1/", "test-key"
        )
        first = await gateway.complete(request())
        assert [call.call_id for call in first.tool_calls] == ["call-a", "call-b"]
        history = (
            request().input
            + first.output
            + [
                ModelFunctionCallOutput(call_id=identifier, output="page result")
                for identifier in ("call-a", "call-b")
            ]
        )
        await gateway.complete(
            ModelRequest(model="configured-model", input=history, tools=request().tools)
        )
    assert bodies[0].messages[0].role.value == "system"
    assert bodies[0].tools[0].function.name == "read_page"
    assert bodies[0].stream is False
    assert bodies[0].store is False
    assert len(bodies[1].messages[2].tool_calls) == 2
    assert [message.tool_call_id for message in bodies[1].messages[3:]] == [
        "call-a",
        "call-b",
    ]


@pytest.mark.parametrize(
    "finish", ["length", "content_filter", "function_call", "stop"]
)
async def test_unverified_or_truncated_response_never_returns_tools(finish):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(
                HTTPStatus.OK,
                content=RESPONSE.replace(
                    '"finish_reason":"tool_calls"', '"finish_reason":"' + finish + '"'
                ),
            )
        )
    ) as client:
        with pytest.raises(DomainError) as failure:
            await ChatCompletionsModelGateway(
                client, "https://vendor.example/v1", "test-key"
            ).complete(request())
    assert failure.value.code == ErrorCode.UNKNOWN_RESULT


@pytest.mark.parametrize(
    "status, expected",
    [
        (HTTPStatus.UNAUTHORIZED, ErrorCode.WAITING_AUTH),
        (HTTPStatus.TOO_MANY_REQUESTS, ErrorCode.WAITING_QUOTA),
        (HTTPStatus.INTERNAL_SERVER_ERROR, ErrorCode.UPSTREAM_FAILURE),
    ],
)
async def test_http_rejection_does_not_expose_secret(status, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(
                status, text="private provider body test-key"
            )
        )
    ) as client:
        with pytest.raises(DomainError) as failure:
            await ChatCompletionsModelGateway(
                client, "https://vendor.example/v1", "test-key"
            ).complete(request())
    assert failure.value.code == expected
    assert "test-key" not in str(failure.value)


async def test_unknown_tool_and_response_byte_budget_are_unknown():
    for content, limit in [
        (RESPONSE.replace('"name":"read_page"', '"name":"undeclared"'), 100000),
        (RESPONSE + " " * 2000, 1024),
    ]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda incoming, result=content: httpx.Response(
                    HTTPStatus.OK, content=result
                )
            )
        ) as client:
            with pytest.raises(DomainError) as failure:
                await ChatCompletionsModelGateway(
                    client,
                    "https://vendor.example/v1",
                    "test-key",
                    ModelTransportLimits(max_response_bytes=limit),
                ).complete(request())
        assert failure.value.code == ErrorCode.UNKNOWN_RESULT


async def test_store_rejection_is_not_retried_with_changed_request():
    requests: list[ChatRequest] = []

    def endpoint(incoming):
        requests.append(ChatRequest.model_validate_json(incoming.content))
        return httpx.Response(
            HTTPStatus.BAD_REQUEST,
            content='{"error":{"message":"Unsupported field store"}}',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        with pytest.raises(DomainError) as failure:
            await ChatCompletionsModelGateway(
                client, "https://vendor.example/v1", "test-key"
            ).complete(request())
    assert failure.value.code == ErrorCode.UPSTREAM_FAILURE
    assert len(requests) == 1


async def test_malformed_wire_json_is_unknown():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(HTTPStatus.OK, content="{")
        )
    ) as client:
        with pytest.raises(DomainError) as failure:
            await ChatCompletionsModelGateway(
                client, "https://vendor.example/v1", "test-key"
            ).complete(request())
    assert failure.value.code == ErrorCode.UNKNOWN_RESULT
