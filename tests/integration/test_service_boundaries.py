import ast
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pytest


class Package(StrEnum):
    BACKEND = "mining_server"
    GATEWAY = "mining_gateway"
    CONTRACTS = "mining_contracts"
    SQLALCHEMY = "sqlalchemy"
    CELERY = "celery"
    PDF = "pymupdf"
    PDF_LEGACY = "fitz"


@dataclass(frozen=True)
class Boundary:
    source: Path
    forbidden: tuple[Package, ...]


@pytest.mark.parametrize(
    "boundary",
    (
        Boundary(Path("apps/backend/src"), (Package.GATEWAY,)),
        Boundary(
            Path("apps/mcp_gateway/src"),
            (
                Package.BACKEND,
                Package.SQLALCHEMY,
                Package.CELERY,
                Package.PDF,
                Package.PDF_LEGACY,
            ),
        ),
        Boundary(
            Path("packages/contracts/src"),
            (Package.BACKEND, Package.GATEWAY, Package.SQLALCHEMY, Package.CELERY),
        ),
    ),
)
def test_service_imports_respect_independent_package_boundaries(boundary: Boundary):
    root = Path(__file__).resolve().parents[2]
    files = tuple((root / boundary.source).rglob("*.py"))
    assert files
    for path in files:
        module = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(module):
            match node:
                case ast.Import(names=names):
                    imports = tuple(name.name for name in names)
                case ast.ImportFrom(module=name) if name is not None:
                    imports = (name,)
                case _:
                    continue
            for name in imports:
                assert name.split(".")[0] not in boundary.forbidden, (
                    f"{path.relative_to(root)} imports forbidden dependency {name}"
                )
