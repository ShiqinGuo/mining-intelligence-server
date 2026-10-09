import asyncio
import base64
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from mining_contracts.domain.core import ErrorCode
from pydantic import BaseModel, ValidationError

from mining_server.domain.core import fail
from mining_server.domain.documents import (
    ParsedDocument,
    ParsedPage,
    RenderedPage,
    TableResult,
)
from mining_server.domain.pdf import (
    ParsedPageBatch,
    ParsedPdfManifest,
    ParserOperation,
    PdfLimits,
)


def write_pdf_manifest(
    path: str, limits: PdfLimits, operation_id: str
) -> ParsedPdfManifest:
    import pymupdf

    target = Path(path).with_suffix(".pages.ndjson")
    temporary = target.parent / (operation_id + ".pages.tmp")
    characters = 0
    try:
        with (
            pymupdf.open(path, filetype="pdf") as document,
            temporary.open("xb") as output,
        ):
            if (
                not document.is_pdf
                or document.is_encrypted
                or not 1 <= document.page_count <= limits.max_pages
            ):
                raise fail(
                    ErrorCode.INVALID_INPUT,
                    "PDF is encrypted, empty, or exceeds page limit",
                )
            for index in range(document.page_count):
                page = document.load_page(index)
                text = page.get_text("text", sort=True)
                characters += len(text)
                if len(text) > limits.page_chars or characters > limits.total_chars:
                    raise fail(
                        ErrorCode.INVALID_INPUT, "PDF text exceeds extraction limits"
                    )
                parsed = ParsedPage(
                    page_number=index + 1,
                    text=text,
                    width=page.rect.width,
                    height=page.rect.height,
                )
                line = parsed.model_dump_json().encode("utf-8") + b"\n"
                if len(line) > limits.line_bytes:
                    raise fail(
                        ErrorCode.INVALID_INPUT, "PDF page exceeds manifest line budget"
                    )
                output.write(line)
                del page, text, parsed, line
                pymupdf.TOOLS.store_shrink(limits.cache_shrink_percent)
            output.flush()
            os.fsync(output.fileno())
            result = ParsedPdfManifest(
                page_count=document.page_count,
                manifest_path=target,
                total_chars=characters,
            )
        temporary.replace(target)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def read_pdf_table(path: str, page_number: int, limits: PdfLimits) -> TableResult:
    import pymupdf

    with pymupdf.open(path, filetype="pdf") as document:
        if page_number < 1 or page_number > min(document.page_count, limits.max_pages):
            raise fail(ErrorCode.INVALID_INPUT, "Requested PDF page does not exist")
        tables = document[page_number - 1].find_tables()
        return TableResult(
            page_number=page_number, tables=[table.extract() for table in tables.tables]
        )


def render_pdf_page(path: str, page_number: int, limits: PdfLimits) -> RenderedPage:
    import pymupdf

    with pymupdf.open(path, filetype="pdf") as document:
        if page_number < 1 or page_number > min(document.page_count, limits.max_pages):
            raise fail(ErrorCode.INVALID_INPUT, "Requested PDF page does not exist")
        page = document[page_number - 1]
        scale = min(
            limits.render_scale,
            limits.render_longest_side / max(page.rect.width, page.rect.height),
        )
        image = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), alpha=False
        ).tobytes("png")
        return RenderedPage(
            page_number=page_number,
            image_data_url="data:image/png;base64," + base64.b64encode(image).decode(),
        )


@dataclass(frozen=True)
class PageBatchRead:
    batch: ParsedPageBatch | None
    next_offset: int


def read_page_batch(
    path: Path, offset: int, batch_size: int, limits: PdfLimits
) -> PageBatchRead:
    pages = []
    with path.open("rb") as stream:
        stream.seek(offset)
        for _ in range(batch_size):
            line = stream.readline(limits.line_bytes + 1)
            if not line:
                break
            if len(line) > limits.line_bytes or not line.endswith(b"\n"):
                raise fail(
                    ErrorCode.INVALID_INPUT,
                    "PDF manifest line exceeds its budget or is incomplete",
                )
            page = ParsedPage.model_validate_json(line)
            if len(page.text) > limits.page_chars:
                raise fail(
                    ErrorCode.INVALID_INPUT, "PDF manifest page exceeds text budget"
                )
            pages.append(page)
        next_offset = stream.tell()
    return PageBatchRead(ParsedPageBatch(pages=pages) if pages else None, next_offset)


async def read_bounded(
    stream: asyncio.StreamReader, limit: int, chunk_bytes: int
) -> bytes:
    output = bytearray()
    while chunk := await stream.read(min(chunk_bytes, limit - len(output) + 1)):
        if len(output) + len(chunk) > limit:
            raise fail(
                ErrorCode.INVALID_INPUT, "PDF subprocess exceeded its output budget"
            )
        output.extend(chunk)
    return bytes(output)


class PdfParser:
    def __init__(self, limits: PdfLimits | None = None):
        self.limits = limits or PdfLimits()

    async def parse_to_file(self, path: Path) -> ParsedPdfManifest:
        return await self._run(ParserOperation.PARSE, ParsedPdfManifest, path)

    async def iter_batches(
        self, manifest: ParsedPdfManifest, batch_size: int = 20
    ) -> AsyncIterator[ParsedPageBatch]:
        if not 1 <= batch_size <= self.limits.batch_pages:
            raise fail(
                ErrorCode.INVALID_INPUT, "PDF batch size must be between one and twenty"
            )
        offset = 0
        count = 0
        characters = 0
        while True:
            try:
                reading = await asyncio.to_thread(
                    read_page_batch,
                    manifest.manifest_path,
                    offset,
                    batch_size,
                    self.limits,
                )
            except (ValidationError, OSError) as error:
                raise fail(
                    ErrorCode.INVALID_INPUT, "PDF manifest cannot be read"
                ) from error
            batch = reading.batch
            offset = reading.next_offset
            if batch is None:
                break
            for page in batch.pages:
                count += 1
                characters += len(page.text)
                if (
                    page.page_number != count
                    or count > manifest.page_count
                    or characters > self.limits.total_chars
                ):
                    raise fail(
                        ErrorCode.INVALID_INPUT,
                        "PDF manifest exceeds declared page or text coverage",
                    )
            yield batch
        if count != manifest.page_count or characters != manifest.total_chars:
            raise fail(
                ErrorCode.INVALID_INPUT, "PDF manifest differs from declared coverage"
            )

    async def parse(self, path: Path) -> ParsedDocument:
        manifest = await self.parse_to_file(path)
        pages = []
        async for batch in self.iter_batches(manifest):
            pages.extend(batch.pages)
        return ParsedDocument(pages=pages)

    async def table(self, path: Path, page_number: int) -> TableResult:
        return await self._run(ParserOperation.TABLE, TableResult, path, page_number)

    async def render(self, path: Path, page_number: int) -> RenderedPage:
        return await self._run(ParserOperation.RENDER, RenderedPage, path, page_number)

    async def _run[T: BaseModel](
        self,
        operation: ParserOperation,
        result_type: type[T],
        path: Path,
        page_number: int = 1,
    ) -> T:
        operation_id = uuid4().hex
        target = path.with_suffix(".pages.ndjson")
        temporary = target.parent / (operation_id + ".pages.tmp")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mining_server.infrastructure.documents.parser_helper",
            operation.value,
            str(path),
            str(page_number),
            self.limits.model_dump_json(),
            operation_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_limit = (
            self.limits.manifest_output_bytes
            if operation == ParserOperation.PARSE
            else self.limits.output_bytes
        )
        readers = [
            asyncio.create_task(
                read_bounded(
                    process.stdout, stdout_limit, self.limits.stream_chunk_bytes
                )
            ),
            asyncio.create_task(
                read_bounded(
                    process.stderr,
                    self.limits.error_bytes,
                    self.limits.stream_chunk_bytes,
                )
            ),
        ]
        try:
            async with asyncio.timeout(self.limits.timeout_seconds):
                output, _ = await asyncio.gather(*readers)
                await process.wait()
            if process.returncode:
                raise fail(
                    ErrorCode.INVALID_INPUT,
                    "PDF parser rejected the document or exceeded resource limits",
                )
            try:
                return result_type.model_validate_json(output)
            except ValidationError as error:
                raise fail(
                    ErrorCode.INVALID_INPUT, "PDF subprocess returned invalid output"
                ) from error
        except TimeoutError as error:
            raise fail(
                ErrorCode.INVALID_INPUT, "PDF parser process exceeded its time budget"
            ) from error
        finally:
            if process.returncode is None:
                process.kill()
                await asyncio.shield(process.wait())
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await asyncio.to_thread(temporary.unlink, missing_ok=True)
