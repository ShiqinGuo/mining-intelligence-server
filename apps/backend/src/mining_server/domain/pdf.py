from enum import StrEnum
from pathlib import Path

from mining_contracts.domain.core import Contract
from pydantic import Field

from mining_server.domain.documents import ParsedPage


class ParserOperation(StrEnum):
    PARSE = "parse"
    TABLE = "table"
    RENDER = "render"


class PdfLimits(Contract):
    memory_bytes: int = Field(
        default=512 * 1024 * 1024, ge=64 * 1024 * 1024, le=512 * 1024 * 1024
    )
    max_pages: int = Field(default=1000, ge=1, le=1000)
    page_chars: int = Field(default=200000, ge=1, le=200000)
    total_chars: int = Field(default=20000000, ge=1, le=20000000)
    line_bytes: int = Field(default=1024 * 1024, ge=1024, le=2 * 1024 * 1024)
    output_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=8 * 1024 * 1024)
    error_bytes: int = Field(default=65536, ge=1024, le=65536)
    timeout_seconds: int = Field(default=120, ge=1, le=120)
    manifest_output_bytes: int = Field(default=16384, ge=1024, le=16384)
    batch_pages: int = Field(default=20, ge=1, le=20)
    stream_chunk_bytes: int = Field(default=65536, ge=1024, le=65536)
    render_longest_side: int = Field(default=1600, ge=1, le=1600)
    render_scale: float = Field(default=1.5, gt=0, le=1.5)
    cache_shrink_percent: int = Field(default=100, ge=1, le=100)


class ParsedPdfManifest(Contract):
    page_count: int = Field(ge=1, le=1000)
    manifest_path: Path
    total_chars: int = Field(ge=0, le=20000000)


class ParsedPageBatch(Contract):
    pages: list[ParsedPage] = Field(min_length=1, max_length=20)
