from http import HTTPStatus

import httpx
import pytest

from mining_server.domain.core import DomainError, ErrorCode
from mining_server.domain.model import (
    CompletedEvent,
    CompletedResponse,
    ItemDoneEvent,
    ModelFunctionCall,
    ModelMessage,
    ModelOutputText,
    ModelReasoning,
    ModelReasoningText,
    ModelRequest,
    ModelRole,
    ProviderEvent,
    ResponseStatus,
)
from mining_server.infrastructure.model.provider import ResponsesModelGateway


async def token() -> str:
    return "test-token"


def request() -> ModelRequest:
    return ModelRequest(
        model="gpt-6.1-sol",
        input=[ModelMessage(role=ModelRole.USER, content="Extract resources")],
    )


def encode(event: ProviderEvent) -> str:
    return "data: " + event.model_dump_json(exclude_none=True) + "\n\n"


@pytest.mark.parametrize("terminal", [False, True])
async def test_finished_items_require_terminal_completion_and_preserve_history(
    terminal,
):
    reasoning = ModelReasoning(id="reasoning_redacted", encrypted_content="redacted")
    message = ModelMessage(
        id="msg_redacted",
        role=ModelRole.ASSISTANT,
        content=[ModelOutputText(text="pong")],
    )
    call = ModelFunctionCall(
        call_id="call_redacted", name="diagnostic_ping", arguments="{}"
    )
    stream = "".join(
        encode(ItemDoneEvent(output_index=index, item=item))
        for index, item in enumerate([reasoning, message, call])
    )
    if terminal:
        stream += encode(
            CompletedEvent(
                response=CompletedResponse(
                    id="resp_redacted", status=ResponseStatus.COMPLETED
                ),
            )
        )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(HTTPStatus.OK, text=stream)
        )
    ) as client:
        if terminal:
            result = await ResponsesModelGateway(client, token).complete(request())
            assert result.text == "pong"
            assert result.output == [reasoning, message, call]
            assert result.tool_calls[0].call_id == "call_redacted"
        else:
            with pytest.raises(DomainError) as error:
                await ResponsesModelGateway(client, token).complete(request())
            assert error.value.code == ErrorCode.UNKNOWN_RESULT


async def test_finished_items_take_priority_without_duplicate_comparison():
    earlier = ModelFunctionCall(call_id="call", name="read_pages", arguments="{}")
    terminal = ModelFunctionCall(
        call_id="call", name="read_pages", arguments='{"pages":[1]}'
    )
    stream = encode(ItemDoneEvent(output_index=0, item=earlier)) + encode(
        CompletedEvent(
            response=CompletedResponse(
                id="resp", status=ResponseStatus.COMPLETED, output=[terminal]
            ),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(HTTPStatus.OK, text=stream)
        )
    ) as client:
        result = await ResponsesModelGateway(client, token).complete(request())
    assert result.tool_calls[0].arguments == earlier.arguments


async def test_empty_terminal_without_finished_items_is_valid():
    stream = encode(
        CompletedEvent(
            response=CompletedResponse(id="empty", status=ResponseStatus.COMPLETED),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(HTTPStatus.OK, text=stream)
        )
    ) as client:
        result = await ResponsesModelGateway(client, token).complete(request())
    assert result.output == []


async def test_empty_terminal_with_index_gap_is_unknown():
    call = ModelFunctionCall(call_id="call_gap", name="read_pages", arguments="{}")
    stream = encode(ItemDoneEvent(output_index=1, item=call)) + encode(
        CompletedEvent(
            response=CompletedResponse(id="gap", status=ResponseStatus.COMPLETED),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(HTTPStatus.OK, text=stream)
        )
    ) as client:
        with pytest.raises(DomainError) as error:
            await ResponsesModelGateway(client, token).complete(request())
    assert error.value.code == ErrorCode.UNKNOWN_RESULT


@pytest.mark.parametrize(
    "status,known",
    [
        (HTTPStatus.BAD_REQUEST, True),
        (HTTPStatus.NOT_FOUND, True),
        (HTTPStatus.REQUEST_TIMEOUT, False),
        (HTTPStatus.BAD_GATEWAY, False),
    ],
)
async def test_http_rejection_preserves_uncertainty(status, known):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(
                status, content='{"error":{"code":"model_not_found"}}'
            )
        )
    ) as client:
        with pytest.raises(DomainError) as error:
            await ResponsesModelGateway(client, token).complete(request())
    assert error.value.code == ErrorCode.UPSTREAM_FAILURE
    assert error.value.details.definitive_rejection is known


async def test_invalid_typed_output_boundary_is_unknown():
    stream = 'data: {"type":"response.completed","response":{"id":"bad","status":"completed","output":[{"type":"function_call","call_id":"missing_fields"}]}}\n\n'
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(HTTPStatus.OK, text=stream)
        )
    ) as client:
        with pytest.raises(DomainError) as error:
            await ResponsesModelGateway(client, token).complete(request())
    assert error.value.code == ErrorCode.UNKNOWN_RESULT


@pytest.mark.parametrize(
    "payload",
    [
        '{"type":"response.output_item.done","item":{"type":"reasoning","id":"reasoning","summary":[]}}',
        '{"type":"response.output_item.done","output_index":0,"item":null}',
        '{"type":"response.completed","response":null}',
        '{"type":"response.completed","response":{"id":"unfinished","status":"in_progress","output":[]}}',
        '{"type":"response.failed","response":{"id":"failed","status":"failed"}}',
    ],
)
async def test_consumed_event_missing_required_fields_fails_at_json_boundary(payload):
    stream = "data: " + payload + "\n\n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(HTTPStatus.OK, text=stream)
        )
    ) as client:
        with pytest.raises(DomainError) as error:
            await ResponsesModelGateway(client, token).complete(request())
    assert error.value.code == ErrorCode.UNKNOWN_RESULT


async def test_official_top_level_error_event_is_consumed_without_optional_envelope():
    stream = 'data: {"type":"error","code":"subscription_sharing_usage_limit_exceeded","message":"Quota unavailable","param":null}\n\n'
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(HTTPStatus.OK, text=stream)
        )
    ) as client:
        with pytest.raises(DomainError) as error:
            await ResponsesModelGateway(client, token).complete(request())
    assert error.value.code == ErrorCode.WAITING_QUOTA


async def test_failed_event_requires_declared_failure_and_preserves_definitive_rejection():
    stream = 'data: {"type":"response.failed","response":{"id":"failed","status":"failed","error":{"code":"server_error"},"output":[]}}\n\n'
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(HTTPStatus.OK, text=stream)
        )
    ) as client:
        with pytest.raises(DomainError) as error:
            await ResponsesModelGateway(client, token).complete(request())
    assert error.value.code == ErrorCode.UPSTREAM_FAILURE
    assert error.value.details.definitive_rejection is True


@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            '{"type":"response.reasoning_text.delta","delta":"private-body-sentinel"}',
            "event=response.reasoning_text.delta|:union_tag_invalid",
        ),
        (
            '{"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","call_id":"private-body-sentinel","name":"read_pages"}}',
            "arguments:missing",
        ),
    ],
)
async def test_validation_diagnostic_has_event_location_type_and_no_payload(
    payload, expected
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(
                HTTPStatus.OK, text="data: " + payload + "\n\n"
            )
        )
    ) as client:
        with pytest.raises(DomainError) as error:
            await ResponsesModelGateway(client, token).complete(request())
    assert expected in error.value.message
    assert "model_validation" in error.value.details.reason
    assert "private-body-sentinel" not in error.value.message
    assert "private-body-sentinel" not in error.value.details.reason
    assert error.value.__cause__ is None


async def test_transport_failure_is_distinct_from_contract_validation():
    def disconnected(incoming: httpx.Request):
        raise httpx.ReadError("private-transport-sentinel", request=incoming)

    async with httpx.AsyncClient(transport=httpx.MockTransport(disconnected)) as client:
        with pytest.raises(DomainError) as error:
            await ResponsesModelGateway(client, token).complete(request())
    assert error.value.details.reason == "model_transport|ReadError"
    assert "transport" in error.value.message
    assert "private-transport-sentinel" not in error.value.message
    assert error.value.__cause__ is None


async def test_reasoning_content_is_typed_preserved_for_replay_and_not_response_text():
    reasoning = ModelReasoning(
        id="reasoning",
        content=[ModelReasoningText(text="private-reasoning-sentinel")],
        encrypted_content="encrypted-redacted",
    )
    call = ModelFunctionCall(call_id="read", name="read_pages", arguments="{}")
    stream = (
        encode(ItemDoneEvent(output_index=0, item=reasoning))
        + encode(ItemDoneEvent(output_index=1, item=call))
        + encode(CompletedEvent(response=CompletedResponse(id="completed")))
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(HTTPStatus.OK, text=stream)
        )
    ) as client:
        result = await ResponsesModelGateway(client, token).complete(request())
    assert result.output[0] == reasoning
    assert result.text == ""
    history = ModelRequest(
        model="gpt-6.1-sol", input=[*request().input, *result.output]
    )
    assert (
        ModelRequest.model_validate_json(history.model_dump_json()).input[1]
        == reasoning
    )


async def test_unknown_reasoning_content_type_fails_at_boundary_with_safe_path():
    stream = 'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"reasoning","id":"reasoning","summary":[],"content":[{"type":"unknown_reasoning","text":"private-reasoning-sentinel"}]}}\n\n'
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(HTTPStatus.OK, text=stream)
        )
    ) as client:
        with pytest.raises(DomainError) as error:
            await ResponsesModelGateway(client, token).complete(request())
    assert "reasoning.content.0.type:literal_error" in error.value.message
    assert "private-reasoning-sentinel" not in error.value.message
