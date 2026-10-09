import argparse
import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import pytest

from mining_server.domain.documents import GoldenReports
from mining_server.infrastructure.settings import Settings


class Command(StrEnum):
    FIXTURES = "fixtures"
    TEST = "test"
    SMOKE = "smoke"


class Namespace(StrEnum):
    NEWS = "news"
    DOCUMENTS = "documents"
    MARKET = "market"


class ToolName(StrEnum):
    SEARCH = "search"
    FETCH_ARTICLE = "fetch_article"
    GET_TASK = "get_task"
    EXTRACT_RESOURCES = "extract_resources"
    GET_EXTRACTION = "get_extraction"
    GET_EXTRACTION_RESULT = "get_extraction_result"
    LIST_INSTRUMENTS = "list_instruments"
    GET_PRICE = "get_price"
    GET_TREND = "get_trend"


@dataclass(frozen=True)
class Defaults:
    download_timeout_seconds: float = 180
    probe_timeout_seconds: float = 30
    backend_url: str = "http://127.0.0.1:28110"
    gateway_url: str = "http://127.0.0.1:28111"


@dataclass
class RejectSkippedTests:
    skipped: list[str] = field(default_factory=list)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.skipped:
            self.skipped.append(report.nodeid)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.skipped:
            self.skipped.append(report.nodeid)

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if self.skipped:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if self.skipped:
            terminalreporter.write_sep("=", "CI rejects skipped tests")
            for nodeid in self.skipped:
                terminalreporter.write_line(nodeid)


async def download_fixtures(defaults: Defaults) -> None:
    import httpx

    destination = Path(os.environ["MINING_REPORT_FIXTURE_DIR"]).resolve()
    if destination.is_relative_to(Path.cwd().resolve()):
        raise ValueError("CI fixtures must be outside the checkout")
    golden = GoldenReports.model_validate_json(
        Path("tests/fixtures/document-golden.json").read_text(encoding="utf-8")
    )
    destination.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        timeout=defaults.download_timeout_seconds, follow_redirects=True
    ) as client:
        for report in golden.reports:
            path = destination / report.filename
            if path.exists():
                data = await asyncio.to_thread(path.read_bytes)
                digest = hashlib.sha256(data).hexdigest()
                if digest == report.sha256:
                    continue
            response = await client.get(str(report.pdf_url))
            response.raise_for_status()
            if hashlib.sha256(response.content).hexdigest() != report.sha256:
                raise ValueError(f"Public fixture hash mismatch: {report.filename}")
            await asyncio.to_thread(path.write_bytes, response.content)
            print(f"Verified public fixture: {report.filename}")


def expected_tools(namespace: Namespace) -> frozenset[ToolName]:
    match namespace:
        case Namespace.NEWS:
            return frozenset(
                (ToolName.SEARCH, ToolName.FETCH_ARTICLE, ToolName.GET_TASK)
            )
        case Namespace.DOCUMENTS:
            return frozenset(
                (
                    ToolName.EXTRACT_RESOURCES,
                    ToolName.GET_EXTRACTION,
                    ToolName.GET_EXTRACTION_RESULT,
                )
            )
        case Namespace.MARKET:
            return frozenset(
                (ToolName.LIST_INSTRUMENTS, ToolName.GET_PRICE, ToolName.GET_TREND)
            )


async def smoke(defaults: Defaults) -> None:
    import httpx
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    settings = Settings()
    async with httpx.AsyncClient(timeout=defaults.probe_timeout_seconds) as client:
        for url in (
            f"{defaults.backend_url}/health/live",
            f"{defaults.backend_url}/health/ready",
            f"{defaults.gateway_url}/health/live",
        ):
            response = await client.get(url)
            response.raise_for_status()
            print(f"Healthy: {url}")
        for namespace in Namespace:
            response = await client.post(f"{defaults.gateway_url}/mcp/{namespace}/")
            if response.status_code != httpx.codes.UNAUTHORIZED:
                raise RuntimeError(f"MCP authentication bypass: {namespace}")
    async with httpx2.AsyncClient(
        headers=[
            ("Authorization", f"Bearer {settings.service_token.get_secret_value()}")
        ],
        timeout=defaults.probe_timeout_seconds,
    ) as client:
        for namespace in Namespace:
            async with (
                streamable_http_client(
                    f"{defaults.gateway_url}/mcp/{namespace}/", http_client=client
                ) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                result = await session.list_tools()
                actual = frozenset(ToolName(tool.name) for tool in result.tools)
                if actual != expected_tools(namespace):
                    raise RuntimeError(f"Unexpected MCP tools: {namespace}")
                print(f"MCP SDK handshake and tools/list passed: {namespace}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", type=Command, choices=list(Command))
    arguments = parser.parse_args()
    defaults = Defaults()
    match arguments.command:
        case Command.FIXTURES:
            asyncio.run(download_fixtures(defaults))
        case Command.TEST:
            raise SystemExit(pytest.main(["-q"], plugins=[RejectSkippedTests()]))
        case Command.SMOKE:
            asyncio.run(smoke(defaults))


if __name__ == "__main__":
    main()
