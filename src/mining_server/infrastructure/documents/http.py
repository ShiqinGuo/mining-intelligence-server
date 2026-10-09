import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import IntEnum, StrEnum
from http import HTTPMethod, HTTPStatus
from typing import TypedDict, overload
from urllib.parse import urljoin, urlsplit

import httpx

from mining_server.domain.core import ErrorCode, ErrorDetails, fail
from mining_server.domain.documents import (
    DocumentTransferLimits,
    PublicBytesResponse,
    StoredDocumentContent,
)
from mining_server.infrastructure.documents.storage import (
    MAX_PDF_BYTES,
    DocumentStorage,
)

SERVER_ERROR_STATUS_STOP = 600


async def public_addresses(host: str, port: int) -> list[str]:
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, int(port), type=socket.SOCK_STREAM
        )
    except socket.gaierror as error:
        raise fail(
            ErrorCode.UPSTREAM_FAILURE,
            "Public host resolution failed",
            ErrorDetails(
                reason=type(error).__name__,
                retryable=error.errno == socket.EAI_AGAIN,
            ),
        ) from error
    values: list[str] = []
    for address in addresses:
        value = address[4][0]
        if value not in values:
            values.append(value)
    if not values:
        raise fail(ErrorCode.INVALID_INPUT, "URL has no resolved addresses")
    for value in values:
        address = ipaddress.ip_address(value)
        if (
            not address.is_global
            or address.is_multicast
            or (
                isinstance(address, ipaddress.IPv6Address)
                and address.ipv4_mapped is not None
            )
        ):
            raise fail(
                ErrorCode.FORBIDDEN, "Private or reserved URL targets are forbidden"
            )
    return values


class TransportExtensions(TypedDict):
    sni_hostname: str


class PublicWebPort(IntEnum):
    HTTP = 80
    HTTPS = 443


class PublicWebScheme(StrEnum):
    HTTP = "http"
    HTTPS = "https"


class PublicHttpClient:
    def __init__(
        self,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
        limits: DocumentTransferLimits | None = None,
    ):
        if timeout_seconds <= 0:
            raise fail(ErrorCode.INVALID_INPUT, "Public HTTP timeout must be positive")
        self.limits = limits if limits is not None else DocumentTransferLimits()
        self.timeout_seconds = timeout_seconds
        self.client = client

    async def get(self, url: str, max_bytes: int) -> PublicBytesResponse:
        if max_bytes < 1:
            raise fail(ErrorCode.INVALID_INPUT, "Response byte limit must be positive")
        async with self._deadline():
            if self.client is not None:
                return await self._get(self.client, url, max_bytes)
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False, trust_env=False
            ) as client:
                return await self._get(client, url, max_bytes)

    async def download(
        self, url: str, storage: DocumentStorage, max_bytes: int = MAX_PDF_BYTES
    ) -> StoredDocumentContent:
        if max_bytes < 1 or max_bytes > MAX_PDF_BYTES:
            raise fail(ErrorCode.INVALID_INPUT, "Invalid document byte limit")
        async with self._deadline():
            if self.client is not None:
                return await self._get(self.client, url, max_bytes, storage)
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False, trust_env=False
            ) as client:
                return await self._get(client, url, max_bytes, storage)

    @asynccontextmanager
    async def _deadline(self) -> AsyncIterator[None]:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                yield
        except TimeoutError as error:
            raise fail(
                ErrorCode.UPSTREAM_FAILURE,
                "Public source transfer timed out",
                ErrorDetails(reason=type(error).__name__, retryable=True),
            ) from error

    @asynccontextmanager
    async def _stream(
        self,
        client: httpx.AsyncClient,
        url: str,
        addresses: list[str],
        host: str,
        hostname: str,
    ) -> AsyncIterator[httpx.Response]:
        for index, address in enumerate(addresses):
            target = httpx.URL(url).copy_with(host=address)
            extensions: TransportExtensions = {"sni_hostname": hostname}
            request = client.build_request(
                HTTPMethod.GET,
                target,
                headers=[("Host", host), ("Accept-Encoding", "identity")],
                extensions=extensions,
            )
            try:
                response = await client.send(
                    request, stream=True, follow_redirects=False
                )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index == len(addresses) - 1:
                    raise
                continue
            try:
                yield response
            finally:
                await response.aclose()
            return

    @overload
    async def _get(
        self, client: httpx.AsyncClient, url: str, max_bytes: int
    ) -> PublicBytesResponse: ...

    @overload
    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        max_bytes: int,
        storage: DocumentStorage,
    ) -> StoredDocumentContent: ...

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        max_bytes: int,
        storage: DocumentStorage | None = None,
    ) -> PublicBytesResponse | StoredDocumentContent:
        current = url
        for hop in range(self.limits.redirect_hops):
            parsed = urlsplit(current)
            if (
                parsed.scheme not in {PublicWebScheme.HTTP, PublicWebScheme.HTTPS}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise fail(
                    ErrorCode.INVALID_INPUT,
                    "A public HTTP(S) URL without credentials is required",
                )
            try:
                port = parsed.port or (
                    PublicWebPort.HTTPS
                    if parsed.scheme == PublicWebScheme.HTTPS
                    else PublicWebPort.HTTP
                )
            except ValueError as error:
                raise fail(ErrorCode.INVALID_INPUT, "Invalid URL port") from error
            if port not in {PublicWebPort.HTTP, PublicWebPort.HTTPS}:
                raise fail(ErrorCode.FORBIDDEN, "Only public web ports are permitted")
            addresses = await public_addresses(parsed.hostname, port)
            host = (
                parsed.hostname if parsed.port is None else f"{parsed.hostname}:{port}"
            )
            try:
                async with self._stream(
                    client, current, addresses, host, parsed.hostname
                ) as response:
                    location = None
                    declared = None
                    content_type = None
                    encoding = "identity"
                    if "location" in response.headers:
                        location = response.headers["location"]
                    if "content-length" in response.headers:
                        declared = response.headers["content-length"]
                    if "content-type" in response.headers:
                        content_type = response.headers["content-type"]
                    if "content-encoding" in response.headers:
                        encoding = response.headers["content-encoding"]
                    if response.status_code in {
                        HTTPStatus.MOVED_PERMANENTLY,
                        HTTPStatus.FOUND,
                        HTTPStatus.SEE_OTHER,
                        HTTPStatus.TEMPORARY_REDIRECT,
                        HTTPStatus.PERMANENT_REDIRECT,
                    }:
                        if not location or hop == self.limits.redirect_hops - 1:
                            raise fail(
                                ErrorCode.UPSTREAM_FAILURE,
                                "Redirect limit or missing redirect target",
                            )
                        current = urljoin(current, location)
                        continue
                    if response.status_code != HTTPStatus.OK:
                        raise fail(
                            ErrorCode.UPSTREAM_FAILURE,
                            "Public source returned a non-success response",
                            ErrorDetails(
                                reason=f"HTTP {response.status_code}",
                                retryable=(
                                    response.status_code == HTTPStatus.TOO_MANY_REQUESTS
                                    or HTTPStatus.INTERNAL_SERVER_ERROR
                                    <= response.status_code
                                    < SERVER_ERROR_STATUS_STOP
                                ),
                            ),
                        )
                    if declared is not None:
                        try:
                            length = int(declared)
                        except ValueError as error:
                            raise fail(
                                ErrorCode.UPSTREAM_FAILURE,
                                "Invalid source content length",
                            ) from error
                        if length < 0 or length > max_bytes:
                            raise fail(
                                ErrorCode.INVALID_INPUT,
                                "Source exceeds response byte limit",
                            )
                    if storage is not None:
                        if encoding.lower().strip() != "identity":
                            raise fail(
                                ErrorCode.INVALID_INPUT,
                                "Compressed HTTP document transfer is not supported",
                            )

                        async def bounded_chunks() -> AsyncIterator[bytes]:
                            size = 0
                            async for chunk in response.aiter_bytes(
                                chunk_size=self.limits.chunk_bytes
                            ):
                                size += len(chunk)
                                if size > max_bytes:
                                    raise fail(
                                        ErrorCode.INVALID_INPUT,
                                        "Source exceeds response byte limit",
                                    )
                                yield chunk

                        stored = await storage.store(bounded_chunks())
                        return StoredDocumentContent(
                            sha256=stored.sha256,
                            size_bytes=stored.size_bytes,
                            source_url=current,
                        )
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes(
                        chunk_size=self.limits.chunk_bytes
                    ):
                        if len(chunks) + len(chunk) > max_bytes:
                            raise fail(
                                ErrorCode.INVALID_INPUT,
                                "Source exceeds response byte limit",
                            )
                        chunks.extend(chunk)
                    return PublicBytesResponse(
                        data=bytes(chunks),
                        final_url=current,
                        content_type=content_type,
                    )
            except httpx.HTTPError as error:
                raise fail(
                    ErrorCode.UPSTREAM_FAILURE,
                    "Public source transport failed",
                    ErrorDetails(
                        reason=type(error).__name__,
                        retryable=isinstance(
                            error,
                            (
                                httpx.ConnectError,
                                httpx.TimeoutException,
                                httpx.ReadError,
                            ),
                        ),
                    ),
                ) from error
        raise fail(ErrorCode.UPSTREAM_FAILURE, "Redirect limit exceeded")
