from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, TypedDict
from urllib.parse import urlsplit

from mining_contracts.domain.core import Contract
from mining_contracts.domain.http import HttpScheme
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer


class ModelChannel(StrEnum):
    CHATGPT_SUBSCRIPTION = "chatgpt_subscription"
    OPENAI_COMPATIBLE = "openai_compatible"


class OpenAIProtocol(StrEnum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"


class ModelChannelState(StrEnum):
    NOT_CONFIGURED = "not_configured"
    CONFIGURED = "configured"
    READY = "ready"
    UNAVAILABLE = "unavailable"


class ModelChannelReason(StrEnum):
    MISSING_API_KEY = "missing_api_key"
    MISSING_OAUTH_HOST = "missing_oauth_host"
    SUBSCRIPTION_NOT_CONNECTED = "subscription_not_connected"
    CONFIGURATION_MISMATCH = "configuration_mismatch"
    UPSTREAM_HTTP_FAILURE = "upstream_http_failure"
    UPSTREAM_NETWORK_FAILURE = "upstream_network_failure"
    INVALID_CATALOG = "invalid_catalog"
    CATALOG_TOO_LARGE = "catalog_too_large"
    SUBSCRIPTION_AUTH_FAILURE = "subscription_auth_failure"


def validate_openai_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Invalid OpenAI base URL") from error
    if parsed.scheme not in (HttpScheme.HTTP, HttpScheme.HTTPS) or not parsed.hostname:
        raise ValueError("OpenAI base URL requires an HTTP(S) host")
    if any(character.isspace() for character in value):
        raise ValueError("OpenAI base URL must not contain whitespace")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "OpenAI base URL must not contain credentials, query, or fragment"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("OpenAI base URL port is outside its valid range")
    return value


class ModelRole(StrEnum):
    DEVELOPER = "developer"
    USER = "user"
    ASSISTANT = "assistant"


class ModelNamespace(StrEnum):
    FUNCTIONS = "functions"


class ModelProtocolType(StrEnum):
    MESSAGE = "message"
    INPUT_TEXT = "input_text"
    OUTPUT_TEXT = "output_text"
    REASONING = "reasoning"
    REASONING_TEXT = "reasoning_text"
    SUMMARY_TEXT = "summary_text"
    NAMESPACE = "namespace"
    FUNCTION = "function"
    FUNCTION_CALL = "function_call"
    FUNCTION_CALL_OUTPUT = "function_call_output"
    INPUT_IMAGE = "input_image"


class ModelImageDetail(StrEnum):
    AUTO = "auto"
    LOW = "low"
    HIGH = "high"


class ModelItemStatus(StrEnum):
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    INCOMPLETE = "incomplete"


class ModelMessagePhase(StrEnum):
    FINAL_ANSWER = "final_answer"
    COMMENTARY = "commentary"


class ModelInputText(Contract):
    type: Literal[ModelProtocolType.INPUT_TEXT] = ModelProtocolType.INPUT_TEXT
    text: str


class ModelInputImage(Contract):
    type: Literal[ModelProtocolType.INPUT_IMAGE] = ModelProtocolType.INPUT_IMAGE
    image_url: str = Field(min_length=1)
    detail: ModelImageDetail = ModelImageDetail.AUTO


class ModelAnnotationType(StrEnum):
    URL_CITATION = "url_citation"
    FILE_CITATION = "file_citation"


class ModelAnnotation(Contract):
    type: ModelAnnotationType
    start_index: int | None = None
    end_index: int | None = None
    url: str | None = None
    title: str | None = None
    file_id: str | None = None
    filename: str | None = None
    index: int | None = None


class ModelTokenProbability(Contract):
    token: str
    logprob: float
    bytes: list[int] | None = None


class ModelTokenLogprob(ModelTokenProbability):
    top_logprobs: list[ModelTokenProbability] = Field(default_factory=list)


class ModelOutputText(Contract):
    type: Literal[ModelProtocolType.OUTPUT_TEXT] = ModelProtocolType.OUTPUT_TEXT
    text: str
    annotations: list[ModelAnnotation] = Field(default_factory=list)
    logprobs: list[ModelTokenLogprob] = Field(default_factory=list)


class ModelSummaryText(Contract):
    type: Literal[ModelProtocolType.SUMMARY_TEXT] = ModelProtocolType.SUMMARY_TEXT
    text: str


class ModelMessage(Contract):
    type: Literal[ModelProtocolType.MESSAGE] = ModelProtocolType.MESSAGE
    role: ModelRole
    content: (
        str
        | list[
            Annotated[
                ModelInputText | ModelInputImage | ModelOutputText,
                Field(discriminator="type"),
            ]
        ]
    )
    id: str | None = None
    status: ModelItemStatus | None = None
    phase: ModelMessagePhase | None = None


class ModelToolCall(Contract):
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    namespace: ModelNamespace = ModelNamespace.FUNCTIONS
    arguments: str


class ModelFunctionCall(ModelToolCall):
    type: Literal[ModelProtocolType.FUNCTION_CALL] = ModelProtocolType.FUNCTION_CALL
    id: str | None = None
    status: ModelItemStatus | None = None


class ModelFunctionCallOutput(Contract):
    type: Literal[ModelProtocolType.FUNCTION_CALL_OUTPUT] = (
        ModelProtocolType.FUNCTION_CALL_OUTPUT
    )
    call_id: str = Field(min_length=1)
    output: str


class ModelReasoningText(Contract):
    type: Literal[ModelProtocolType.REASONING_TEXT] = ModelProtocolType.REASONING_TEXT
    text: str


class ModelReasoning(Contract):
    type: Literal[ModelProtocolType.REASONING] = ModelProtocolType.REASONING
    id: str
    summary: list[ModelSummaryText] = Field(default_factory=list)
    content: list[ModelReasoningText] | None = None
    encrypted_content: str | None = None
    status: ModelItemStatus | None = None


class JsonSchemaType(StrEnum):
    OBJECT = "object"
    ARRAY = "array"
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    NULL = "null"


class NamedJsonSchema(Contract):
    name: str
    definition: "JsonSchema"


SchemaReferencesWire = TypedDict(
    "SchemaReferencesWire", {"$defs": dict[str, "JsonSchema"], "$ref": str}, total=False
)


class SchemaWire(SchemaReferencesWire, total=False):
    type: str | list[str]
    title: str
    description: str
    properties: dict[str, "JsonSchema"]
    required: list[str]
    items: "JsonSchema"
    additionalProperties: "bool | JsonSchema"
    anyOf: list["JsonSchema"]
    oneOf: list["JsonSchema"]
    allOf: list["JsonSchema"]
    prefixItems: list["JsonSchema"]
    multipleOf: float
    enum: list[str | int | float | bool | None]
    const: str | int | float | bool | None
    default: str | int | float | bool | list[str | int | float | bool] | None
    minimum: float
    maximum: float
    exclusiveMinimum: float
    exclusiveMaximum: float
    minLength: int
    maxLength: int
    minItems: int
    maxItems: int
    pattern: str
    format: str


class JsonSchema(Contract):
    type: JsonSchemaType | list[JsonSchemaType] | None = None
    title: str | None = None
    description: str | None = None
    properties: list[NamedJsonSchema] = Field(default_factory=list)
    definitions: list[NamedJsonSchema] = Field(default_factory=list, alias="$defs")
    ref: str | None = Field(default=None, alias="$ref")
    required: list[str] = Field(default_factory=list)
    items: "JsonSchema | None" = None
    additional_properties: "bool | JsonSchema | None" = Field(
        default=None, alias="additionalProperties"
    )
    any_of: list["JsonSchema"] = Field(default_factory=list, alias="anyOf")
    one_of: list["JsonSchema"] = Field(default_factory=list, alias="oneOf")
    all_of: list["JsonSchema"] = Field(default_factory=list, alias="allOf")
    prefix_items: list["JsonSchema"] = Field(default_factory=list, alias="prefixItems")
    multiple_of: float | None = Field(default=None, alias="multipleOf")
    enum: list[str | int | float | bool | None] | None = None
    const: str | int | float | bool | None = None
    default: str | int | float | bool | list[str | int | float | bool] | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: float | None = Field(default=None, alias="exclusiveMinimum")
    exclusive_maximum: float | None = Field(default=None, alias="exclusiveMaximum")
    min_length: int | None = Field(default=None, alias="minLength")
    max_length: int | None = Field(default=None, alias="maxLength")
    min_items: int | None = Field(default=None, alias="minItems")
    max_items: int | None = Field(default=None, alias="maxItems")
    pattern: str | None = None
    format: str | None = None

    @field_validator("properties", "definitions", mode="before")
    @classmethod
    def named_schema_fields(cls, value):
        match value:
            case list():
                return value
            case dict():
                wire: SchemaWire = {"properties": value}
                return [
                    NamedJsonSchema(
                        name=name, definition=JsonSchema.model_validate(schema)
                    )
                    for name, schema in wire["properties"].items()
                ]
            case _:
                raise ValueError(
                    "Schema properties and definitions require named schemas"
                )

    @model_serializer
    def serialize_schema(self):
        wire: SchemaWire = {}
        if self.type is not None:
            wire["type"] = self.type
        if self.title is not None:
            wire["title"] = self.title
        if self.description is not None:
            wire["description"] = self.description
        if self.properties:
            wire["properties"] = {
                item.name: item.definition for item in self.properties
            }
        if self.definitions:
            wire["$defs"] = {item.name: item.definition for item in self.definitions}
        if self.ref is not None:
            wire["$ref"] = self.ref
        if self.required:
            wire["required"] = self.required
        if self.items is not None:
            wire["items"] = self.items
        if self.additional_properties is not None:
            wire["additionalProperties"] = self.additional_properties
        if self.any_of:
            wire["anyOf"] = self.any_of
        if self.one_of:
            wire["oneOf"] = self.one_of
        if self.all_of:
            wire["allOf"] = self.all_of
        if self.prefix_items:
            wire["prefixItems"] = self.prefix_items
        if self.multiple_of is not None:
            wire["multipleOf"] = self.multiple_of
        if self.enum is not None:
            wire["enum"] = self.enum
        if "const" in self.model_fields_set:
            wire["const"] = self.const
        if "default" in self.model_fields_set:
            wire["default"] = self.default
        if self.minimum is not None:
            wire["minimum"] = self.minimum
        if self.maximum is not None:
            wire["maximum"] = self.maximum
        if self.exclusive_minimum is not None:
            wire["exclusiveMinimum"] = self.exclusive_minimum
        if self.exclusive_maximum is not None:
            wire["exclusiveMaximum"] = self.exclusive_maximum
        if self.min_length is not None:
            wire["minLength"] = self.min_length
        if self.max_length is not None:
            wire["maxLength"] = self.max_length
        if self.min_items is not None:
            wire["minItems"] = self.min_items
        if self.max_items is not None:
            wire["maxItems"] = self.max_items
        if self.pattern is not None:
            wire["pattern"] = self.pattern
        if self.format is not None:
            wire["format"] = self.format
        return wire


class ModelFunctionTool(Contract):
    type: Literal[ModelProtocolType.FUNCTION] = ModelProtocolType.FUNCTION
    name: str = Field(min_length=1)
    description: str = ""
    parameters: JsonSchema
    strict: bool = False


class ModelNamespaceTool(Contract):
    type: Literal[ModelProtocolType.NAMESPACE] = ModelProtocolType.NAMESPACE
    name: ModelNamespace = ModelNamespace.FUNCTIONS
    description: str = ""
    tools: list[ModelFunctionTool]


type ModelInput = Annotated[
    ModelMessage | ModelFunctionCall | ModelFunctionCallOutput | ModelReasoning,
    Field(discriminator="type"),
]
type ModelOutput = Annotated[
    ModelMessage | ModelFunctionCall | ModelReasoning, Field(discriminator="type")
]
type ModelTool = Annotated[
    ModelFunctionTool | ModelNamespaceTool, Field(discriminator="type")
]


class ModelRequest(Contract):
    model: str = Field(min_length=1)
    input: list[ModelInput] = Field(min_length=1)
    tools: list[ModelTool] = Field(default_factory=list)


class ModelResponse(Contract):
    response_id: str = Field(min_length=1)
    text: str = ""
    tool_calls: list[ModelToolCall] = Field(default_factory=list)
    output: list[ModelOutput]
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class ResponseStatus(StrEnum):
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    QUEUED = "queued"


class ProviderEventKind(StrEnum):
    COMPLETED = "response.completed"
    FAILED = "response.failed"
    INCOMPLETE = "response.incomplete"
    ERROR = "error"
    ITEM_DONE = "response.output_item.done"
    CREATED = "response.created"
    IN_PROGRESS = "response.in_progress"
    ITEM_ADDED = "response.output_item.added"
    CONTENT_ADDED = "response.content_part.added"
    CONTENT_DONE = "response.content_part.done"
    TEXT_DELTA = "response.output_text.delta"
    TEXT_DONE = "response.output_text.done"
    ARGUMENT_DELTA = "response.function_call_arguments.delta"
    ARGUMENT_DONE = "response.function_call_arguments.done"
    REASONING_PART_ADDED = "response.reasoning_summary_part.added"
    REASONING_PART_DONE = "response.reasoning_summary_part.done"
    REASONING_TEXT_DELTA = "response.reasoning_summary_text.delta"
    REASONING_TEXT_DONE = "response.reasoning_summary_text.done"


class ModelStreamMarker(StrEnum):
    DATA = "data:"
    DONE = "[DONE]"


class ProviderError(Contract):
    code: str
    message: str | None = None
    param: str | None = None
    type: str | None = None


class ProviderErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    error: ProviderError


class ResponseUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class ResponsesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str = Field(min_length=1)
    status: ResponseStatus
    output: list[ModelOutput] = Field(default_factory=list)
    error: ProviderError | None = None
    usage: ResponseUsage | None = None


class CompletedResponse(ResponsesResponse):
    status: Literal[ResponseStatus.COMPLETED] = ResponseStatus.COMPLETED
    error: None = None

    @field_validator("output")
    @classmethod
    def completed_items(cls, value: list[ModelOutput]) -> list[ModelOutput]:
        if any(item.status not in {None, ModelItemStatus.COMPLETED} for item in value):
            raise ValueError("Completed response contains an unfinished output item")
        return value


class FailedResponse(ResponsesResponse):
    status: Literal[ResponseStatus.FAILED] = ResponseStatus.FAILED
    error: ProviderError = Field(...)


class IncompleteResponse(ResponsesResponse):
    status: Literal[ResponseStatus.INCOMPLETE] = ResponseStatus.INCOMPLETE


class ProviderWireEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class CompletedEvent(ProviderWireEvent):
    type: Literal[ProviderEventKind.COMPLETED] = ProviderEventKind.COMPLETED
    response: CompletedResponse


class ItemDoneEvent(ProviderWireEvent):
    type: Literal[ProviderEventKind.ITEM_DONE] = ProviderEventKind.ITEM_DONE
    item: ModelOutput
    output_index: int = Field(ge=0)

    @field_validator("item")
    @classmethod
    def completed_item(cls, value: ModelOutput) -> ModelOutput:
        if value.status not in {None, ModelItemStatus.COMPLETED}:
            raise ValueError("Done event contains an unfinished output item")
        return value


class FailedEvent(ProviderWireEvent):
    type: Literal[ProviderEventKind.FAILED] = ProviderEventKind.FAILED
    response: FailedResponse


class ErrorEvent(ProviderWireEvent):
    type: Literal[ProviderEventKind.ERROR] = ProviderEventKind.ERROR
    code: str | None
    message: str
    param: str | None


class IncompleteEvent(ProviderWireEvent):
    type: Literal[ProviderEventKind.INCOMPLETE] = ProviderEventKind.INCOMPLETE
    response: IncompleteResponse


class IncrementEvent(ProviderWireEvent):
    type: Literal[
        ProviderEventKind.CREATED,
        ProviderEventKind.IN_PROGRESS,
        ProviderEventKind.ITEM_ADDED,
        ProviderEventKind.CONTENT_ADDED,
        ProviderEventKind.CONTENT_DONE,
        ProviderEventKind.TEXT_DELTA,
        ProviderEventKind.TEXT_DONE,
        ProviderEventKind.ARGUMENT_DELTA,
        ProviderEventKind.ARGUMENT_DONE,
        ProviderEventKind.REASONING_PART_ADDED,
        ProviderEventKind.REASONING_PART_DONE,
        ProviderEventKind.REASONING_TEXT_DELTA,
        ProviderEventKind.REASONING_TEXT_DONE,
    ]


type ProviderEvent = Annotated[
    CompletedEvent
    | ItemDoneEvent
    | FailedEvent
    | ErrorEvent
    | IncompleteEvent
    | IncrementEvent,
    Field(discriminator="type"),
]


class ModelTransportLimits(Contract):
    max_response_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    read_chunk_bytes: int = Field(default=65536, ge=1024)
    quota_retry_seconds: int = Field(default=3600, ge=1)
    validation_issue_count: int = Field(default=10, ge=1)
    validation_reason_characters: int = Field(default=2048, ge=1)
    read_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    connect_timeout_seconds: float = Field(default=30, gt=0, le=300)
    write_timeout_seconds: float = Field(default=30, gt=0, le=300)
    pool_timeout_seconds: float = Field(default=30, gt=0, le=300)


class ModelTransportFailure(StrEnum):
    CONNECT_TIMEOUT = "ConnectTimeout"
    READ_TIMEOUT = "ReadTimeout"
    WRITE_TIMEOUT = "WriteTimeout"
    POOL_TIMEOUT = "PoolTimeout"
    CONNECT_ERROR = "ConnectError"
    READ_ERROR = "ReadError"
    WRITE_ERROR = "WriteError"
    CLOSE_ERROR = "CloseError"
    LOCAL_PROTOCOL_ERROR = "LocalProtocolError"
    REMOTE_PROTOCOL_ERROR = "RemoteProtocolError"
    PROXY_ERROR = "ProxyError"
    UNSUPPORTED_PROTOCOL = "UnsupportedProtocol"
    TRANSPORT_ERROR = "TransportError"


class ModelFailureReason(StrEnum):
    VALIDATION = "model_validation"
    TRANSPORT = "model_transport"
    STREAM_PROTOCOL = "model_stream_protocol"


class ModelValidationIssue(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    loc: list[str | int]
    type: str


class ModelEventHeader(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    type: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class ChatRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ChatPartType(StrEnum):
    TEXT = "text"
    IMAGE_URL = "image_url"


class ChatFinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    FUNCTION_CALL = "function_call"


class ChatText(Contract):
    type: Literal[ChatPartType.TEXT] = ChatPartType.TEXT
    text: str


class ChatImageUrl(Contract):
    url: str
    detail: ModelImageDetail


class ChatImage(Contract):
    type: Literal[ChatPartType.IMAGE_URL] = ChatPartType.IMAGE_URL
    image_url: ChatImageUrl


class ChatFunctionInvocation(Contract):
    name: str = Field(min_length=1)
    arguments: str


class ChatToolCall(Contract):
    id: str = Field(min_length=1)
    type: Literal[ModelProtocolType.FUNCTION] = ModelProtocolType.FUNCTION
    function: ChatFunctionInvocation


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    role: ChatRole
    content: (
        str | list[Annotated[ChatText | ChatImage, Field(discriminator="type")]] | None
    ) = None
    tool_calls: list[ChatToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    refusal: str | None = None


class ChatFunctionDefinition(Contract):
    name: str
    description: str
    parameters: JsonSchema
    strict: bool


class ChatToolDefinition(Contract):
    type: Literal[ModelProtocolType.FUNCTION] = ModelProtocolType.FUNCTION
    function: ChatFunctionDefinition


class ChatRequest(Contract):
    model: str
    messages: list[ChatMessage]
    tools: list[ChatToolDefinition] = Field(default_factory=list)
    stream: bool = False
    store: bool = False


class ChatChoice(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    finish_reason: ChatFinishReason
    message: ChatMessage


class ChatUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str = Field(min_length=1)
    choices: list[ChatChoice] = Field(min_length=1, max_length=1)
    usage: ChatUsage | None = None


class ModelCatalogItem(Contract):
    slug: str = Field(min_length=1, max_length=200)
    display_name: str

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("Model slug must not contain whitespace")
        return value


class ModelCatalog(Contract):
    models: list[ModelCatalogItem]


class ModelChannelView(Contract):
    channel: ModelChannel
    state: ModelChannelState
    protocol: OpenAIProtocol
    base_url: str
    key_configured: bool
    oauth_host_configured: bool = False
    reason: ModelChannelReason | None = None


class ModelChannelList(Contract):
    selected_channel: ModelChannel
    channels: list[ModelChannelView]


class ModelChannelCheck(Contract):
    channel: ModelChannel
    state: ModelChannelState
    checked_at: datetime
    models: list[ModelCatalogItem] = Field(default_factory=list)
    reason: ModelChannelReason | None = None
    upstream_status: int | None = None


class OpenAIModelEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str = Field(min_length=1, max_length=200)


class OpenAIModelsPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    data: list[OpenAIModelEntry] = Field(max_length=10000)
