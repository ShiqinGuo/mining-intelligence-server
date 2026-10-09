import argparse
import os
import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from mining_server.domain.pdf import ParserOperation, PdfLimits


@dataclass(frozen=True)
class ParserArguments:
    operation: ParserOperation
    path: Path
    page_number: int
    limits: PdfLimits
    operation_id: str


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", type=ParserOperation)
    parser.add_argument("path", type=Path)
    parser.add_argument("page_number", type=int)
    parser.add_argument("limits", type=PdfLimits.model_validate_json)
    parser.add_argument("operation_id")
    namespace = parser.parse_args()
    arguments = ParserArguments(
        operation=namespace.operation,
        path=namespace.path,
        page_number=namespace.page_number,
        limits=namespace.limits,
        operation_id=namespace.operation_id,
    )
    limits = arguments.limits
    lock_path = Path(tempfile.gettempdir()) / "mining_server_pdf_parser.lock"
    with lock_path.open("a+b") as lock:
        if os.name == "posix":
            import fcntl
            import resource

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            memory = limits.memory_bytes
            seconds = limits.timeout_seconds
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
        else:
            import msvcrt

            lock.seek(0)
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)

        with redirect_stdout(sys.stderr):
            from mining_server.infrastructure.documents.parser import (
                read_pdf_table,
                render_pdf_page,
                write_pdf_manifest,
            )

            match arguments.operation:
                case ParserOperation.PARSE:
                    result = write_pdf_manifest(
                        str(arguments.path), limits, arguments.operation_id
                    )
                    maximum = limits.manifest_output_bytes
                case ParserOperation.TABLE:
                    result = read_pdf_table(
                        str(arguments.path), arguments.page_number, limits
                    )
                    maximum = limits.output_bytes
                case ParserOperation.RENDER:
                    result = render_pdf_page(
                        str(arguments.path), arguments.page_number, limits
                    )
                    maximum = limits.output_bytes
        encoded = result.model_dump_json().encode("utf-8")
        if len(encoded) > maximum:
            raise ValueError("PDF output budget exceeded")
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        main()
    except MemoryError:
        os.write(2, b"PDF memory budget exceeded\n")
        raise SystemExit(2) from None
    except (ValueError, TypeError, RuntimeError, OSError, ImportError):
        os.write(2, b"PDF operation failed\n")
        raise SystemExit(2) from None
