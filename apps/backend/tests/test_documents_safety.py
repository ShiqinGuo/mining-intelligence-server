import asyncio
import multiprocessing
import os
import sys
from pathlib import Path

import httpx
import pymupdf
import pytest
from mining_contracts.domain.core import ErrorCode
from mining_contracts.domain.documents import ReportingStandard

from mining_server.domain.core import DomainError
from mining_server.domain.documents import (
    FinishExtractionArguments,
    ParsedDocument,
    ParsedPage,
)
from mining_server.infrastructure.documents.agent import (
    DocumentAgent,
    InMemoryDocumentPages,
)
from mining_server.infrastructure.documents.http import PublicHttpClient
from mining_server.infrastructure.documents.parser import PdfParser
from mining_server.infrastructure.documents.storage import DocumentStorage


async def test_private_pdf_url_never_reaches_transport():
    reached = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: reached.append(request))
    ) as client:
        with pytest.raises(DomainError) as error:
            await PublicHttpClient(5, client).get("http://127.0.0.1/private.pdf", 1000)
    assert error.value.code == ErrorCode.FORBIDDEN
    assert reached == []


async def test_upload_hash_ignores_filename_and_rejects_non_pdf(tmp_path):
    storage = DocumentStorage(tmp_path)

    async def valid():
        yield b"%PDF-1.7\nfixed content"

    first = await storage.store(valid())
    second = await storage.store(valid())
    assert first == second
    assert len(list(storage.root.iterdir())) == 1

    async def invalid():
        yield b"not a PDF"

    with pytest.raises(DomainError):
        await storage.store(invalid())
    assert len(list(storage.root.iterdir())) == 1


async def test_journal_standard_and_evidence_cannot_be_invented():
    agent = DocumentAgent(None, PdfParser(), "gpt-6.1-sol")
    document = ParsedDocument(
        pages=[
            ParsedPage(
                page_number=1,
                text="JORC indicated 100 Mt 1.2% Li2O",
                width=600,
                height=800,
            )
        ]
    )
    result = FinishExtractionArguments(
        reporting_standard=ReportingStandard.NI_43_101, complete=True
    )
    with pytest.raises(DomainError) as error:
        await agent.verify(
            result, InMemoryDocumentPages(document), ReportingStandard.AUTO, set()
        )
    assert error.value.code == ErrorCode.STANDARD_MISMATCH


@pytest.mark.parametrize(
    "filename,pages",
    [("sigma-2023.pdf", 568), ("falchani-2023.pdf", 106), ("pilgangoora-2025.pdf", 83)],
)
async def test_real_report_parses_in_separate_process(filename, pages):
    configured = None
    if "MINING_REPORT_FIXTURE_DIR" in os.environ:
        configured = os.environ["MINING_REPORT_FIXTURE_DIR"]
    if configured is None:
        pytest.skip(
            "Download the three public reports listed in docs/DESIGN.md and set MINING_REPORT_FIXTURE_DIR to their directory"
        )
    if not configured or not Path(configured).is_dir():
        pytest.fail("MINING_REPORT_FIXTURE_DIR must name an existing report directory")
    path = Path(configured) / filename
    if not path.is_file():
        pytest.fail(
            f"Download the report listed in docs/DESIGN.md as {filename} into MINING_REPORT_FIXTURE_DIR"
        )
    result = await PdfParser().parse(path)
    assert len(result.pages) == pages
    assert any("inferred" in page.text.casefold() for page in result.pages)


def parse_inside_daemon(path: str) -> None:
    result = asyncio.run(PdfParser().parse(Path(path)))
    if len(result.pages) != 1:
        raise ValueError("Unexpected daemon parser page count")


def test_parser_runs_inside_daemonic_worker(tmp_path):
    path = tmp_path / "daemon-test.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((40, 40), "JORC indicated resources")
        document.save(path)
    process = multiprocessing.get_context("spawn").Process(
        target=parse_inside_daemon, args=(str(path),), daemon=True
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        pytest.fail("Daemon parser did not terminate")
    assert process.exitcode == 0


async def test_parser_cancellation_terminates_subprocess(monkeypatch, tmp_path):
    original = asyncio.create_subprocess_exec
    processes = []
    started = asyncio.Event()

    async def slow_process(*args, **kwargs):
        process = await original(
            sys.executable, "-c", "import time; time.sleep(30)", **kwargs
        )
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_process)
    task = asyncio.create_task(PdfParser().parse(tmp_path / "unused.pdf"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=3)
    assert processes[0].returncode is not None
