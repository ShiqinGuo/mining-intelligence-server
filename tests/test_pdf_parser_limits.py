import asyncio
import json
import sys

import pymupdf
import pytest

from mining_server.domain.core import DomainError, ErrorCode
from mining_server.domain.pdf import ParsedPdfManifest, PdfLimits
from mining_server.infrastructure.documents.parser import PdfParser


def create_pdf(path, pages=25):
    with pymupdf.open() as document:
        for index in range(pages):
            page = document.new_page()
            page.insert_text((40, 40), f"Page {index + 1} mining evidence")
        document.save(path)


async def test_manifest_roundtrip_is_incremental_and_declares_exact_coverage(tmp_path):
    path = tmp_path / "report.pdf"
    create_pdf(path)
    parser = PdfParser()
    manifest = await parser.parse_to_file(path)
    assert manifest.page_count == 25
    assert manifest.manifest_path.parent == path.parent
    batches = [batch async for batch in parser.iter_batches(manifest)]
    assert [len(batch.pages) for batch in batches] == [20, 5]
    assert (
        sum(len(page.text) for batch in batches for page in batch.pages)
        == manifest.total_chars
    )
    assert batches[-1].pages[-1].page_number == 25
    assert not list(tmp_path.glob("*.tmp"))


async def test_page_limit_failure_does_not_publish_partial_manifest(tmp_path):
    path = tmp_path / "oversized.pdf"
    create_pdf(path, 2)
    with pytest.raises(DomainError) as error:
        await PdfParser(PdfLimits(page_chars=5)).parse_to_file(path)
    assert error.value.code == ErrorCode.INVALID_INPUT
    assert not path.with_suffix(".pages.ndjson").exists()
    assert not list(tmp_path.glob("*.tmp"))


async def test_manifest_line_reader_rejects_oversized_line_before_json_decode(tmp_path):
    path = tmp_path / "pages.ndjson"
    path.write_bytes(b"x" * 2048)
    manifest = ParsedPdfManifest(page_count=1, manifest_path=path, total_chars=0)
    with pytest.raises(DomainError) as error:
        async for _ in PdfParser(PdfLimits(line_bytes=1024)).iter_batches(manifest):
            pytest.fail("Oversized manifest line reached a consumer")
    assert error.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
async def test_subprocess_output_limit_kills_before_eof(monkeypatch, tmp_path, channel):
    original = asyncio.create_subprocess_exec
    processes = []

    async def excessive(*arguments, **options):
        script = f"import sys,time; sys.{channel}.buffer.write(b'x'*100000); sys.{channel}.flush(); time.sleep(30)"
        process = await original(sys.executable, "-c", script, **options)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", excessive)
    with pytest.raises(DomainError) as error:
        await asyncio.wait_for(
            PdfParser().parse_to_file(tmp_path / "unused.pdf"), timeout=5
        )
    assert error.value.code == ErrorCode.INVALID_INPUT
    assert processes[0].returncode is not None


async def test_manifest_declared_coverage_mismatch_is_rejected(tmp_path):
    path = tmp_path / "report.pdf"
    create_pdf(path, 1)
    parser = PdfParser()
    manifest = await parser.parse_to_file(path)
    changed = manifest.model_copy(update={"total_chars": manifest.total_chars + 1})
    with pytest.raises(DomainError) as error:
        async for _ in parser.iter_batches(changed):
            pass
    assert error.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.skipif(sys.platform != "linux", reason="Linux RLIMIT_AS is required")
async def test_linux_helper_address_space_cap_rejects_large_allocation(tmp_path):
    script = "import sys,resource\nfrom mining_server.domain.pdf import PdfLimits\nimport mining_server.infrastructure.documents.parser as parser\nfrom mining_server.infrastructure.documents.parser_helper import main\ndef oversized(path,page,limits):\n    assert resource.getrlimit(resource.RLIMIT_AS)==(512*1024*1024,512*1024*1024)\n    return bytearray(513*1024*1024)\nparser.read_pdf_table=oversized\nsys.argv=['helper','table','unused.pdf','1',PdfLimits().model_dump_json(),'memorytest']\ntry: main()\nexcept MemoryError: print('bounded_memory_failure');sys.exit(0)\nsys.exit(3)"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode == 0
    assert stdout == b"bounded_memory_failure\n"
    assert stderr == b""


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process lock is required")
async def test_linux_helpers_serialize_across_processes(tmp_path):
    script = "import sys,time,json\nfrom pathlib import Path\nfrom mining_server.domain.pdf import PdfLimits\nfrom mining_server.domain.documents import TableResult\nimport mining_server.infrastructure.documents.parser as parser\nfrom mining_server.infrastructure.documents.parser_helper import main\noutput=Path(sys.argv[1])\ndef bounded(path,page,limits):\n    start=time.monotonic()\n    time.sleep(0.3)\n    output.write_text(json.dumps([start,time.monotonic()]))\n    return TableResult(page_number=1,tables=[])\nparser.read_pdf_table=bounded\nsys.argv=['helper','table','unused.pdf','1',PdfLimits().model_dump_json(),'locktest']\nmain()"
    paths = [tmp_path / "first.json", tmp_path / "second.json"]
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for path in paths
    ]
    await asyncio.wait_for(
        asyncio.gather(*(process.communicate() for process in processes)), timeout=10
    )
    assert all(process.returncode == 0 for process in processes)
    intervals = sorted(json.loads(path.read_text()) for path in paths)
    assert intervals[1][0] >= intervals[0][1]
