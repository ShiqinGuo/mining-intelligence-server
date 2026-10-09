from http import HTTPStatus

import httpx
from mining_contracts.domain.core import ErrorCode, ErrorDetails
from mining_contracts.domain.http import HttpHeader, HttpMethod, MediaType
from pydantic import ValidationError

from mining_server.domain.core import fail
from mining_server.domain.model import (
    ChatFinishReason,
    ChatFunctionDefinition,
    ChatFunctionInvocation,
    ChatImage,
    ChatImageUrl,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatRole,
    ChatText,
    ChatToolCall,
    ChatToolDefinition,
    ModelFunctionCall,
    ModelFunctionCallOutput,
    ModelFunctionTool,
    ModelInputImage,
    ModelInputText,
    ModelMessage,
    ModelNamespaceTool,
    ModelOutputText,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelToolCall,
    ModelTransportLimits,
)
from mining_server.infrastructure.model.http import model_timeout, transport_failure


def chat_body(request: ModelRequest) -> ChatRequest:
    messages: list[ChatMessage] = []
    pending: set[str] = set()
    seen: set[str] = set()
    for item in request.input:
        match item:
            case ModelFunctionCall():
                if item.call_id in seen:
                    raise fail(
                        ErrorCode.INVALID_INPUT,
                        "Duplicate tool call ID in model history",
                    )
                seen.add(item.call_id)
                if not messages or messages[-1].role != ChatRole.ASSISTANT:
                    messages.append(ChatMessage(role=ChatRole.ASSISTANT))
                call = ChatToolCall(
                    id=item.call_id,
                    function=ChatFunctionInvocation(
                        name=item.name, arguments=item.arguments
                    ),
                )
                previous = messages[-1]
                messages[-1] = ChatMessage(
                    role=previous.role,
                    content=previous.content,
                    tool_calls=[*previous.tool_calls, call],
                    tool_call_id=previous.tool_call_id,
                    refusal=previous.refusal,
                )
                pending.add(item.call_id)
            case ModelFunctionCallOutput():
                if item.call_id not in pending:
                    raise fail(
                        ErrorCode.INVALID_INPUT, "Tool result has no pending call"
                    )
                pending.remove(item.call_id)
                messages.append(
                    ChatMessage(
                        role=ChatRole.TOOL,
                        tool_call_id=item.call_id,
                        content=item.output,
                    )
                )
            case ModelMessage():
                if pending:
                    raise fail(
                        ErrorCode.INVALID_INPUT,
                        "Tool results must precede next message",
                    )
                role = (
                    ChatRole.SYSTEM
                    if item.role == ModelRole.DEVELOPER
                    else ChatRole(item.role.value)
                )
                match item.content:
                    case str():
                        content = item.content
                    case list():
                        content = []
                        for block in item.content:
                            match block:
                                case ModelInputText() | ModelOutputText():
                                    content.append(ChatText(text=block.text))
                                case ModelInputImage():
                                    if role != ChatRole.USER:
                                        raise fail(
                                            ErrorCode.INVALID_INPUT,
                                            "Images require a user message",
                                        )
                                    content.append(
                                        ChatImage(
                                            image_url=ChatImageUrl(
                                                url=block.image_url, detail=block.detail
                                            )
                                        )
                                    )
                messages.append(ChatMessage(role=role, content=content))
            case _:
                raise fail(
                    ErrorCode.INVALID_INPUT,
                    "Chat Completions cannot accept reasoning history",
                )
    if pending:
        raise fail(ErrorCode.INVALID_INPUT, "History has unanswered tool calls")
    tools: list[ChatToolDefinition] = []
    for item in request.tools:
        match item:
            case ModelFunctionTool():
                definitions = [item]
            case ModelNamespaceTool():
                definitions = item.tools
        for definition in definitions:
            tools.append(
                ChatToolDefinition(
                    function=ChatFunctionDefinition(
                        name=definition.name,
                        description=definition.description,
                        parameters=definition.parameters,
                        strict=definition.strict,
                    )
                )
            )
    names = [tool.function.name for tool in tools]
    if len(set(names)) != len(names):
        raise fail(ErrorCode.INVALID_INPUT, "Tool names must be unique")
    return ChatRequest(model=request.model, messages=messages, tools=tools)


def chat_response(response: ChatResponse) -> ModelResponse:
    choice = response.choices[0]
    if choice.finish_reason not in {ChatFinishReason.STOP, ChatFinishReason.TOOL_CALLS}:
        raise fail(
            ErrorCode.UNKNOWN_RESULT, "Model completion was truncated or unverified"
        )
    message = choice.message
    if message.role != ChatRole.ASSISTANT or message.refusal:
        raise fail(
            ErrorCode.UNKNOWN_RESULT,
            "Model response is not a completed assistant message",
        )
    match message.content:
        case str():
            text = message.content
        case None:
            text = ""
        case _:
            raise fail(ErrorCode.UNKNOWN_RESULT, "Model assistant output must be text")
    calls = [
        ModelToolCall(
            call_id=call.id, name=call.function.name, arguments=call.function.arguments
        )
        for call in message.tool_calls
    ]
    if (choice.finish_reason == ChatFinishReason.TOOL_CALLS) != bool(calls) or len(
        {call.call_id for call in calls}
    ) != len(calls):
        raise fail(
            ErrorCode.UNKNOWN_RESULT, "Tool calls disagree with completion status"
        )
    output = []
    if text:
        output.append(
            ModelMessage(role=ModelRole.ASSISTANT, content=[ModelOutputText(text=text)])
        )
    output.extend(
        ModelFunctionCall(
            call_id=call.call_id, name=call.name, arguments=call.arguments
        )
        for call in calls
    )
    usage = response.usage
    return ModelResponse(
        response_id=response.id,
        text=text,
        tool_calls=calls,
        output=output,
        input_tokens=usage.prompt_tokens if usage else None,
        output_tokens=usage.completion_tokens if usage else None,
    )


class ChatCompletionsModelGateway:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        limits: ModelTransportLimits | None = None,
    ):
        self.client = client
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.limits = limits or ModelTransportLimits()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        body = chat_body(request)
        try:
            async with self.client.stream(
                HttpMethod.POST,
                self.url,
                headers=httpx.Headers(
                    [
                        (HttpHeader.AUTHORIZATION, f"Bearer {self.api_key}"),
                        (HttpHeader.CONTENT_TYPE, MediaType.JSON),
                    ]
                ),
                content=body.model_dump_json(exclude_none=True),
                timeout=model_timeout(self.limits),
            ) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > self.limits.max_response_bytes:
                        raise fail(
                            ErrorCode.UNKNOWN_RESULT,
                            "Model response exceeded its byte budget",
                        )
                    raw.extend(chunk)
                if response.status_code != HTTPStatus.OK:
                    match response.status_code:
                        case HTTPStatus.UNAUTHORIZED | HTTPStatus.FORBIDDEN:
                            code = ErrorCode.WAITING_AUTH
                        case HTTPStatus.TOO_MANY_REQUESTS:
                            code = ErrorCode.WAITING_QUOTA
                        case _:
                            code = ErrorCode.UPSTREAM_FAILURE
                    raise fail(
                        code,
                        "Compatible model endpoint rejected the request",
                        ErrorDetails(
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
                            retryable=response.status_code
                            == HTTPStatus.TOO_MANY_REQUESTS,
                        ),
                    )
                result = chat_response(ChatResponse.model_validate_json(raw))
                allowed = {tool.function.name for tool in body.tools}
                if any(call.name not in allowed for call in result.tool_calls):
                    raise fail(
                        ErrorCode.UNKNOWN_RESULT,
                        "Provider requested an undeclared tool",
                    )
                return result
        except httpx.TransportError as error:
            failure = transport_failure(error)
            raise fail(
                ErrorCode.UNKNOWN_RESULT,
                "Compatible model transport did not confirm completion: " + failure,
                ErrorDetails(reason=failure),
            ) from None
        except ValidationError as error:
            raise fail(
                ErrorCode.UNKNOWN_RESULT,
                "Compatible model outcome could not be verified",
            ) from error
