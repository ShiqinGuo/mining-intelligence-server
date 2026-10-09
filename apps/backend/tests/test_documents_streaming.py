import asyncio

import httpx
import pytest

from mining_server.api.upload_ingress import UploadIngressMiddleware
from mining_server.domain.core import DomainError
from mining_server.infrastructure.documents.http import PublicHttpClient
from mining_server.infrastructure.documents.storage import DocumentStorage


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


async def test_compressed_transfer_is_rejected_before_decoding(tmp_path, monkeypatch):
    async def addresses(host, port):
        return ["8.8.8.8"]

    monkeypatch.setattr(
        "mining_server.infrastructure.documents.http.public_addresses", addresses
    )
    stream = Stream([b"unconsumed compressed content"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=stream
            )
        )
    ) as client:
        with pytest.raises(DomainError):
            await PublicHttpClient(5, client).download(
                "https://example.com/report.pdf", DocumentStorage(tmp_path)
            )
    assert stream.consumed == 0


async def test_url_stream_limit_and_cleanup(tmp_path, monkeypatch):
    async def addresses(host, port):
        return ["8.8.8.8"]

    monkeypatch.setattr(
        "mining_server.infrastructure.documents.http.public_addresses", addresses
    )
    stream = Stream([b"%PDF-" + b"x" * 65531, b"x" * 65536, b"unused"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream)
        )
    ) as client:
        public = PublicHttpClient(5, client)
        storage = DocumentStorage(tmp_path)
        with pytest.raises(DomainError):
            await public.download("https://example.com/report.pdf", storage, 65536)
        assert stream.consumed == 2
        assert not list(storage.root.glob("*"))


async def test_storage_cancellation_removes_partial(tmp_path):
    entered = asyncio.Event()

    async def chunks():
        yield b"%PDF-content"
        entered.set()
        await asyncio.Event().wait()

    storage = DocumentStorage(tmp_path)
    task = asyncio.create_task(storage.store(chunks()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(storage.root.glob("*.part"))


async def test_download_final_redirect_url_and_digest(tmp_path, monkeypatch):
    async def addresses(host, port):
        return ["8.8.8.8"]

    monkeypatch.setattr(
        "mining_server.infrastructure.documents.http.public_addresses", addresses
    )

    def handle(request):
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "example.com"
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final.pdf"})
        return httpx.Response(200, stream=Stream([b"%PDF-", b"content"]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        storage = DocumentStorage(tmp_path)
        result = await PublicHttpClient(5, client).download(
            "https://example.com/start", storage
        )
        assert result.source_url == "https://example.com/final.pdf"
        assert storage.path(result.sha256).read_bytes() == b"%PDF-content"


async def test_ingress_stops_before_consuming_all_chunks():
    received = 0
    sent = []

    async def receive():
        nonlocal received
        received += 1
        return {"type": "http.request", "body": b"x" * 6, "more_body": True}

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        while True:
            await receive()

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/documents/upload",
        "headers": [],
    }
    await UploadIngressMiddleware(app, max_bytes=10)(scope, receive, send)
    assert received == 2
    assert sent[0]["status"] == 413
    assert b"invalid_input" in sent[1]["body"]


async def test_ingress_declared_length_and_concurrency():
    entered = asyncio.Event()
    release = asyncio.Event()
    sent = []

    async def app(scope, receive, send):
        entered.set()
        await release.wait()

    async def receive():
        raise AssertionError("Body must not be consumed")

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/documents/upload",
        "headers": [],
    }
    middleware = UploadIngressMiddleware(app, max_bytes=10, concurrency=1)
    first = asyncio.create_task(middleware(scope, receive, send))
    await entered.wait()
    await middleware(scope, receive, send)
    assert sent[0]["status"] == 503
    release.set()
    await first
    sent.clear()
    await middleware(scope | {"headers": [(b"content-length", b"11")]}, receive, send)
    assert sent[0]["status"] == 413


async def test_actual_multipart_parser_returns_clean_413(monkeypatch):
    import tempfile

    import starlette.formparsers
    from fastapi import FastAPI, File, UploadFile

    opened = []
    factory = tempfile.SpooledTemporaryFile

    def capture(*args, **kwargs):
        stream = factory(*args, **kwargs)
        opened.append(stream)
        return stream

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", capture)
    app = FastAPI()
    app.add_middleware(UploadIngressMiddleware, max_bytes=180)

    file_field = File()

    @app.post("/api/v1/documents/upload")
    async def upload(file: UploadFile = file_field):
        await file.close()
        return {"ok": True}

    async def body():
        yield b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="x.pdf"\r\nContent-Type: application/pdf\r\n\r\n%PDF-small'
        yield b"x" * 100
        raise AssertionError("Exceeded body must not be drained")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/documents/upload",
            headers={"Content-Type": "multipart/form-data; boundary=boundary"},
            content=body(),
        )
    assert response.status_code == 413
    assert response.json()["code"] == "invalid_input"
    assert opened and all(stream.closed for stream in opened)


async def test_cancel_during_delayed_open_closes_and_removes_file(
    tmp_path, monkeypatch
):
    import threading
    from pathlib import Path

    entered = threading.Event()
    release = threading.Event()
    opened = []
    original = Path.open

    def delayed(path, *args, **kwargs):
        if path.suffix == ".part":
            entered.set()
            assert release.wait(5)
            stream = original(path, *args, **kwargs)
            opened.append(stream)
            return stream
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", delayed)

    async def chunks():
        yield b"%PDF-content"

    storage = DocumentStorage(tmp_path)
    task = asyncio.create_task(storage.store(chunks()))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert opened and all(stream.closed for stream in opened)
    assert not list(storage.root.glob("*.part"))
