import asyncio
import socket
from http import HTTPStatus

import httpx
import pytest
from mining_contracts.domain.core import ErrorCode

from mining_server.domain.core import DomainError
from mining_server.infrastructure.documents.http import (
    PublicHttpClient,
    public_addresses,
)
from mining_server.infrastructure.documents.storage import DocumentStorage

IPV6 = "2606:4700:4700::1111"
IPV4 = "1.1.1.1"


def resolve(monkeypatch, addresses):
    async def getaddrinfo(host, port, *, type):
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port),
            )
            for address in addresses
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_unreachable_ipv6_falls_back_to_pinned_ipv4(monkeypatch, failure):
    resolve(monkeypatch, [IPV6, IPV4, IPV6])
    assert await public_addresses("example.com", 443) == [IPV6, IPV4]
    attempts = []

    async def handle(request):
        attempts.append(request.url.host)
        assert request.headers["host"] == "example.com:443"
        assert request.extensions["sni_hostname"] == "example.com"
        assert request.headers["accept-encoding"] == "identity"
        if request.url.host == IPV6:
            raise failure("Failed https://example.com/?secret=value", request=request)
        return httpx.Response(HTTPStatus.OK, content=b"feed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await PublicHttpClient(5, client).get(
            "https://example.com:443/feed", 100
        )
    assert result.data == b"feed"
    assert attempts == [IPV6, IPV4]


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (HTTPStatus.FORBIDDEN, False),
        (HTTPStatus.NOT_FOUND, False),
        (HTTPStatus.TOO_MANY_REQUESTS, True),
        (HTTPStatus.SERVICE_UNAVAILABLE, True),
        (HTTPStatus.INTERNAL_SERVER_ERROR, True),
    ],
)
async def test_http_status_classification_does_not_fall_back(
    monkeypatch, status, retryable
):
    resolve(monkeypatch, [IPV6, IPV4])
    attempts = []

    def handle(request):
        attempts.append(request.url.host)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DomainError) as caught:
            await PublicHttpClient(5, client).get("https://example.com/feed", 100)
    assert attempts == [IPV6]
    assert caught.value.details.reason == f"HTTP {status}"
    assert caught.value.details.retryable is retryable


@pytest.mark.parametrize("addresses", [[IPV4, "127.0.0.1"], ["::1", IPV4]])
async def test_any_nonpublic_dns_address_rejects_before_request(monkeypatch, addresses):
    resolve(monkeypatch, addresses)

    def handle(request):
        raise AssertionError("A rejected DNS answer must not reach transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DomainError) as caught:
            await PublicHttpClient(5, client).get("https://example.com/feed", 100)
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert not caught.value.details.retryable


class FailingStream(httpx.AsyncByteStream):
    def __init__(self, failure):
        self.closed = False
        self.failure = failure

    async def __aiter__(self):
        yield b"%PDF-" + b"x" * 65536
        raise self.failure("Failed https://example.com/?secret=value")

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    "failure", [httpx.ReadError, httpx.ReadTimeout, httpx.ConnectError]
)
async def test_read_failure_does_not_retry_and_removes_partial(
    monkeypatch, tmp_path, failure
):
    resolve(monkeypatch, [IPV6, IPV4])
    attempts = []
    stream = FailingStream(failure)

    def handle(request):
        attempts.append(request.url.host)
        return httpx.Response(HTTPStatus.OK, stream=stream)

    storage = DocumentStorage(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DomainError) as caught:
            await PublicHttpClient(5, client).download(
                "https://example.com/report.pdf?secret=value", storage
            )
    assert attempts == [IPV6]
    assert caught.value.details.reason == failure.__name__
    assert caught.value.details.retryable
    assert stream.closed
    assert not list(storage.root.glob("*"))


async def test_exhausted_connect_attempts_report_safe_reason(monkeypatch):
    resolve(monkeypatch, [IPV6, IPV4])
    attempts = []

    def handle(request):
        attempts.append(request.url.host)
        raise httpx.ConnectError("URL secret=value", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DomainError) as caught:
            await PublicHttpClient(5, client).get(
                "https://example.com/?secret=value", 100
            )
    assert attempts == [IPV6, IPV4]
    assert caught.value.details.reason == "ConnectError"
    assert caught.value.details.retryable


async def test_address_attempts_share_total_timeout(monkeypatch):
    resolve(monkeypatch, [IPV6, IPV4])
    entered_second = asyncio.Event()

    async def handle(request):
        if request.url.host == IPV6:
            await asyncio.sleep(0.2)
            raise httpx.ConnectTimeout("Failed", request=request)
        entered_second.set()
        await asyncio.sleep(0.2)
        return httpx.Response(HTTPStatus.OK, content=b"feed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DomainError) as caught:
            await PublicHttpClient(0.35, client).get("https://example.com/feed", 100)
    assert entered_second.is_set()
    assert caught.value.code == ErrorCode.UPSTREAM_FAILURE
    assert caught.value.details.reason == "TimeoutError"
    assert caught.value.details.retryable


@pytest.mark.parametrize(
    ("dns_code", "retryable"),
    [(socket.EAI_AGAIN, True), (socket.EAI_NONAME, False)],
)
async def test_dns_error_retry_classification(monkeypatch, dns_code, retryable):
    async def getaddrinfo(host, port, *, type):
        raise socket.gaierror(dns_code, "URL secret=value")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    with pytest.raises(DomainError) as caught:
        await PublicHttpClient(5).get("https://example.com/feed", 100)
    assert caught.value.details.reason == "gaierror"
    assert caught.value.details.retryable is retryable


async def test_invalid_content_length_is_not_retryable(monkeypatch):
    resolve(monkeypatch, [IPV4])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                HTTPStatus.OK, headers=[("content-length", "invalid")]
            )
        )
    ) as client:
        with pytest.raises(DomainError) as caught:
            await PublicHttpClient(5, client).get("https://example.com/feed", 100)
    assert not caught.value.details.retryable


class StalledStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b"%PDF-" + b"x" * 65536
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


async def test_total_download_deadline_cleans_partial_and_is_retryable(
    monkeypatch, tmp_path
):
    resolve(monkeypatch, [IPV4])
    stream = StalledStream()
    storage = DocumentStorage(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(HTTPStatus.OK, stream=stream)
        )
    ) as client:
        with pytest.raises(DomainError) as caught:
            await PublicHttpClient(0.2, client).download(
                "https://example.com/report.pdf", storage
            )
    assert caught.value.details.retryable
    assert caught.value.details.reason == "TimeoutError"
    assert stream.closed
    assert not list(storage.root.glob("*"))
