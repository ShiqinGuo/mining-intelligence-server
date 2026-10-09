from enum import StrEnum
from http import HTTPMethod, HTTPStatus
from uuid import uuid4

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mining_server.domain.core import DomainError, ErrorCode, ErrorResponse, fail
from mining_server.infrastructure.documents.storage import MAX_PDF_BYTES


class UploadMessageType(StrEnum):
    HTTP = "http"
    REQUEST = "http.request"
    RESPONSE_START = "http.response.start"


class UploadIngressMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int = MAX_PDF_BYTES + 1024 * 1024,
        concurrency: int = 2,
    ):
        if max_bytes < 1 or concurrency < 1:
            raise fail(ErrorCode.INVALID_INPUT, "Upload limits must be positive")
        self.app = app
        self.max_bytes = max_bytes
        self.concurrency = concurrency
        self.active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != UploadMessageType.HTTP
            or scope["path"] != "/api/v1/documents/upload"
            or scope["method"] != HTTPMethod.POST
        ):
            await self.app(scope, receive, send)
            return

        async def reject(
            status: HTTPStatus, message: str, code: ErrorCode = ErrorCode.INVALID_INPUT
        ):
            request_id = str(uuid4())
            if "state" in scope and "request_id" in scope["state"]:
                request_id = scope["state"]["request_id"]
            error = fail(code, message)
            response = JSONResponse(
                ErrorResponse(
                    code=error.code, message=error.message, request_id=request_id
                ).model_dump(mode="json"),
                status_code=status,
                headers=Headers(raw=[(b"x-request-id", request_id.encode("ascii"))]),
            )
            await response(scope, receive, send)

        lengths = [
            value
            for name, value in scope["headers"]
            if name.lower() == b"content-length"
        ]
        if lengths:
            if len(lengths) != 1 or not lengths[0].isdigit():
                await reject(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Invalid upload content length"
                )
                return
            if int(lengths[0]) > self.max_bytes:
                await reject(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "Upload exceeds request byte limit",
                )
                return
        if self.active >= self.concurrency:
            await reject(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Upload capacity is exhausted",
                ErrorCode.BUSY,
            )
            return
        self.active += 1
        size = 0
        exceeded = False
        started = False

        async def limited_receive() -> Message:
            nonlocal size, exceeded
            message = await receive()
            if message["type"] == UploadMessageType.REQUEST:
                size += len(message["body"]) if "body" in message else 0
                if size > self.max_bytes:
                    exceeded = True
                    raise fail(
                        ErrorCode.INVALID_INPUT, "Upload exceeds request byte limit"
                    )
            return message

        async def bounded_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                return
            if message["type"] == UploadMessageType.RESPONSE_START:
                started = True
            await send(message)

        try:
            try:
                await self.app(scope, limited_receive, bounded_send)
            except DomainError:
                if not exceeded:
                    raise
            if exceeded and not started:
                await reject(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "Upload exceeds request byte limit",
                )
        finally:
            self.active -= 1
