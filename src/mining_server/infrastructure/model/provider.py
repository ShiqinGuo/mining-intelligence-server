from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Protocol

import httpx
from pydantic import TypeAdapter, ValidationError

from mining_server.domain.core import ErrorCode, ErrorDetails, fail
from mining_server.domain.http import HttpHeader, HttpMethod, MediaType
from mining_server.domain.model import (
    CompletedEvent,
    CompletedResponse,
    ErrorEvent,
    FailedEvent,
    IncompleteEvent,
    IncrementEvent,
    ItemDoneEvent,
    ModelEventHeader,
    ModelFailureReason,
    ModelFunctionCall,
    ModelMessage,
    ModelOutput,
    ModelOutputText,
    ModelRequest,
    ModelResponse,
    ModelStreamMarker,
    ModelToolCall,
    ModelTransportLimits,
    ModelValidationIssue,
    ProviderErrorEnvelope,
    ProviderEvent,
)
from mining_server.infrastructure.model.http import model_timeout, transport_failure


class ModelGateway(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class ProviderErrorCode(StrEnum):
    USAGE_LIMIT = "subscription_sharing_usage_limit_exceeded"
    USAGE_UNAVAILABLE = "subscription_sharing_usage_unavailable"
    INVALID_TOKEN = "invalid_token"
    TOKEN_EXPIRED = "token_expired"
    INVALID_USER = "subscription_sharing_invalid_user"


class ResponsesRequest(ModelRequest):
    store: bool = False
    stream: bool = True


@dataclass(frozen=True)
class FinishedOutput:
    index: int
    item: ModelOutput


def provider_failure(code: str | None, status: int, definitive_rejection: bool = False):
    if (
        code in {ProviderErrorCode.USAGE_LIMIT, ProviderErrorCode.USAGE_UNAVAILABLE}
        or status == HTTPStatus.TOO_MANY_REQUESTS
    ):
        raise fail(
            ErrorCode.WAITING_QUOTA,
            "Model usage is currently unavailable",
            ErrorDetails(
                retryable=True,
                retry_after=ModelTransportLimits().quota_retry_seconds,
                reason=code,
                definitive_rejection=True,
            ),
        )
    if (
        code
        in {
            ProviderErrorCode.INVALID_TOKEN,
            ProviderErrorCode.TOKEN_EXPIRED,
            ProviderErrorCode.INVALID_USER,
        }
        or status == HTTPStatus.UNAUTHORIZED
    ):
        raise fail(
            ErrorCode.WAITING_AUTH,
            "Model authorization is required",
            ErrorDetails(reason=code, definitive_rejection=True),
        )
    raise fail(
        ErrorCode.UPSTREAM_FAILURE,
        "Model request was rejected",
        ErrorDetails(reason=code, definitive_rejection=definitive_rejection),
    )


class ResponsesModelGateway:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token_provider: Callable[[], Awaitable[str]],
        limits: ModelTransportLimits | None = None,
        endpoint: str = "https://api.openai.com/v1/responses",
    ):
        self.client = client
        self.token_provider = token_provider
        self.limits = limits or ModelTransportLimits()
        self.endpoint = endpoint
        self.event_adapter = TypeAdapter(ProviderEvent)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        token = await self.token_provider()
        body = ResponsesRequest(
            model=request.model, input=request.input, tools=request.tools
        )
        finished: list[FinishedOutput] = []
        event_header: ModelEventHeader | None = None
        try:
            async with self.client.stream(
                HttpMethod.POST,
                self.endpoint,
                headers=httpx.Headers(
                    [
                        (HttpHeader.AUTHORIZATION, f"Bearer {token}"),
                        (HttpHeader.CONTENT_TYPE, MediaType.JSON),
                    ]
                ),
                content=body.model_dump_json(exclude_none=True),
                timeout=model_timeout(self.limits),
            ) as response:
                if response.status_code != HTTPStatus.OK:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > self.limits.max_response_bytes:
                            raise fail(
                                ErrorCode.UNKNOWN_RESULT,
                                "Model error response exceeded its byte budget",
                            )
                        raw.extend(chunk)
                    error = ProviderErrorEnvelope.model_validate_json(raw).error
                    provider_failure(
                        error.code,
                        response.status_code,
                        definitive_rejection=response.status_code
                        in {
                            HTTPStatus.BAD_REQUEST,
                            HTTPStatus.UNAUTHORIZED,
                            HTTPStatus.FORBIDDEN,
                            HTTPStatus.NOT_FOUND,
                            HTTPStatus.METHOD_NOT_ALLOWED,
                            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            HTTPStatus.TOO_MANY_REQUESTS,
                        },
                    )
                data: list[str] = []
                size = 0
                async for line in response.aiter_lines():
                    size += len(line.encode())
                    if size > self.limits.max_response_bytes:
                        raise fail(
                            ErrorCode.UNKNOWN_RESULT,
                            "Model stream exceeded its byte budget before completion",
                        )
                    if line.startswith(ModelStreamMarker.DATA):
                        data.append(line.removeprefix(ModelStreamMarker.DATA).lstrip())
                    elif not line and data:
                        payload = "\n".join(data)
                        data.clear()
                        if payload == ModelStreamMarker.DONE:
                            continue
                        event_header = None
                        event_header = ModelEventHeader.model_validate_json(payload)
                        event = self.event_adapter.validate_json(payload)
                        match event:
                            case ItemDoneEvent():
                                finished = [
                                    item
                                    for item in finished
                                    if item.index != event.output_index
                                ]
                                finished.append(
                                    FinishedOutput(event.output_index, event.item)
                                )
                            case CompletedEvent():
                                terminal = event.response
                                if finished:
                                    ordered = sorted(
                                        finished, key=lambda item: item.index
                                    )
                                    if [item.index for item in ordered] != list(
                                        range(len(ordered))
                                    ):
                                        raise ValueError(
                                            "Completed response lacks contiguous finished output"
                                        )
                                    terminal = CompletedResponse(
                                        id=terminal.id,
                                        output=[item.item for item in ordered],
                                        usage=terminal.usage,
                                    )
                                return completed_response(terminal)
                            case FailedEvent():
                                provider_failure(
                                    event.response.error.code,
                                    HTTPStatus.OK,
                                    definitive_rejection=True,
                                )
                            case ErrorEvent():
                                provider_failure(event.code, HTTPStatus.OK)
                            case IncompleteEvent():
                                raise fail(
                                    ErrorCode.UPSTREAM_FAILURE,
                                    "Model returned an incomplete response",
                                )
                            case IncrementEvent():
                                continue
        except ValidationError as error:
            issues = TypeAdapter(list[ModelValidationIssue]).validate_python(
                error.errors(
                    include_input=False, include_context=False, include_url=False
                )
            )
            locations = [
                ".".join(str(component) for component in issue.loc) + ":" + issue.type
                for issue in issues[: self.limits.validation_issue_count]
            ]
            event_name = (
                "|event=" + event_header.type if event_header is not None else ""
            )
            reason = (
                ModelFailureReason.VALIDATION + event_name + "|" + ";".join(locations)
            )[: self.limits.validation_reason_characters]
            raise fail(
                ErrorCode.UNKNOWN_RESULT,
                "Model stream response failed contract validation: " + reason,
                ErrorDetails(reason=reason),
            ) from None
        except httpx.TransportError as error:
            failure = transport_failure(error)
            raise fail(
                ErrorCode.UNKNOWN_RESULT,
                "Model stream transport did not confirm completion: " + failure,
                ErrorDetails(reason=ModelFailureReason.TRANSPORT + "|" + failure),
            ) from None
        except ValueError as error:
            raise fail(
                ErrorCode.UNKNOWN_RESULT,
                "Model stream completion protocol was invalid",
                ErrorDetails(reason=ModelFailureReason.STREAM_PROTOCOL),
            ) from error
        raise fail(
            ErrorCode.UNKNOWN_RESULT, "Model stream ended without response.completed"
        )


def completed_response(response: CompletedResponse) -> ModelResponse:
    text: list[str] = []
    calls: list[ModelToolCall] = []
    for item in response.output:
        match item:
            case ModelMessage(content=content):
                match content:
                    case str():
                        text.append(content)
                    case list():
                        for block in content:
                            match block:
                                case ModelOutputText(text=value):
                                    text.append(value)
                                case _:
                                    continue
            case ModelFunctionCall():
                calls.append(
                    ModelToolCall(
                        call_id=item.call_id,
                        name=item.name,
                        arguments=item.arguments,
                        namespace=item.namespace,
                    )
                )
            case _:
                continue
    usage = response.usage
    return ModelResponse(
        response_id=response.id,
        text="".join(text),
        tool_calls=calls,
        output=response.output,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
    )
