import asyncio
import sys
from uuid import uuid4

import pymupdf

from mining_server.domain.documents import TableResult
from mining_server.domain.pdf import ParserOperation, PdfLimits
from mining_server.infrastructure.documents.parser import PdfParser, read_bounded


async def test_real_table_library_diagnostics_do_not_corrupt_protocol_stdout(tmp_path):
    path = tmp_path / "table.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((40, 40), "Indicated 100 Mt")
        document.save(path)
    limits = PdfLimits()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mining_server.infrastructure.documents.parser_helper",
        ParserOperation.TABLE.value,
        str(path),
        "1",
        limits.model_dump_json(),
        uuid4().hex,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, diagnostics = await asyncio.gather(
        read_bounded(process.stdout, limits.output_bytes, limits.stream_chunk_bytes),
        read_bounded(process.stderr, limits.error_bytes, limits.stream_chunk_bytes),
    )
    await process.wait()
    assert process.returncode == 0
    result = TableResult.model_validate_json(output)
    assert result.page_number == 1
    assert output.startswith(b"{")
    assert b"Consider using the pymupdf_layout package" not in output
    assert b"Consider using the pymupdf_layout package" in diagnostics
    assert await PdfParser().table(path, 1) == result
