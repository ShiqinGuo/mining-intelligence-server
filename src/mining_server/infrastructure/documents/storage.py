import asyncio
import hashlib
import os
import shutil
from collections.abc import AsyncIterator, Callable
from functools import partial
from pathlib import Path
from uuid import uuid4

from mining_server.domain.core import ErrorCode, fail
from mining_server.domain.documents import DocumentTransferLimits, StoredDocumentContent

MAX_PDF_BYTES = 250 * 1024 * 1024


async def disk_operation[T](action: Callable[[], T]) -> T:
    task = asyncio.create_task(asyncio.to_thread(action))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class DocumentStorage:
    def __init__(self, root: Path, limits: DocumentTransferLimits | None = None):
        self.limits = limits if limits is not None else DocumentTransferLimits()
        self.root = root.resolve() / "documents"

    def path(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(
            character not in "0123456789abcdef" for character in sha256
        ):
            raise fail(ErrorCode.INVALID_INPUT, "Invalid document content hash")
        return self.root / f"{sha256}.pdf"

    async def store(self, chunks: AsyncIterator[bytes]) -> StoredDocumentContent:
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)
        available = await asyncio.to_thread(shutil.disk_usage, self.root)
        if available.free < MAX_PDF_BYTES + self.limits.disk_reserve_bytes:
            raise fail(ErrorCode.BUSY, "Insufficient document storage space")
        temporary = self.root / f"{uuid4()}.part"
        digest = hashlib.sha256()
        size = 0
        header = bytearray()
        stream = None
        opening = asyncio.create_task(asyncio.to_thread(temporary.open, "xb"))
        try:
            stream = await asyncio.shield(opening)
            async for chunk in chunks:
                size += len(chunk)
                if size > MAX_PDF_BYTES:
                    raise fail(ErrorCode.INVALID_INPUT, "PDF exceeds 250 MiB limit")
                if len(header) < self.limits.header_bytes:
                    header.extend(chunk[: self.limits.header_bytes - len(header)])
                digest.update(chunk)
                await disk_operation(partial(stream.write, chunk))
            if size == 0 or b"%PDF-" not in header:
                raise fail(ErrorCode.INVALID_INPUT, "Uploaded content is not a PDF")
            await disk_operation(stream.flush)
            await disk_operation(lambda: os.fsync(stream.fileno()))
            await disk_operation(stream.close)
            sha256 = digest.hexdigest()
            destination = self.path(sha256)
            await disk_operation(lambda: os.replace(temporary, destination))
            return StoredDocumentContent(sha256=sha256, size_bytes=size)
        finally:
            while not opening.done():
                try:
                    await asyncio.shield(opening)
                except asyncio.CancelledError:
                    continue
            if (
                stream is None
                and not opening.cancelled()
                and opening.exception() is None
            ):
                stream = opening.result()
            if stream is not None and not stream.closed:
                await disk_operation(stream.close)
            if await disk_operation(temporary.exists):
                await disk_operation(temporary.unlink)
