import argparse
import asyncio
import hashlib
import os
import time
from pathlib import Path

import httpx
from mining_contracts.domain.core import ErrorCode
from mining_contracts.domain.documents import (
    DocumentResponse,
    ExtractionRequest,
    ExtractionSubmission,
    ResourceExtractionResult,
)
from mining_contracts.domain.task_views import TaskView
from mining_contracts.domain.tasks import TaskState

from mining_server.domain.core import fail
from mining_server.domain.documents import (
    DocumentAcceptanceMode,
    DocumentAcceptanceOutcome,
    DocumentAcceptanceRequest,
    GoldenReport,
    GoldenReports,
)
from mining_server.infrastructure.documents.golden import verify_golden


async def validate_fixture(report: GoldenReport, fixture_dir: Path) -> bytes:
    path = fixture_dir / report.filename
    data = await asyncio.to_thread(path.read_bytes)
    if hashlib.sha256(data).hexdigest() != report.sha256:
        raise fail(
            ErrorCode.INVALID_INPUT, f"Report fixture hash mismatch: {report.filename}"
        )
    return data


async def run_live(
    client: httpx.AsyncClient,
    report: GoldenReport,
    data: bytes,
    run_id: str,
    timeout_seconds: int,
) -> DocumentAcceptanceOutcome:
    uploaded = await client.post(
        "documents/upload", files=[("file", (report.filename, data, "application/pdf"))]
    )
    uploaded.raise_for_status()
    document = DocumentResponse.model_validate_json(uploaded.content)
    request = ExtractionRequest(
        document_id=document.id, standard=report.reporting_standard
    )
    submitted = await client.post(
        "resource-extractions",
        json=request.model_dump(mode="json"),
        headers=[
            (
                "Idempotency-Key",
                "golden:"
                + hashlib.sha256(f"{run_id}:{report.filename}".encode()).hexdigest(),
            )
        ],
    )
    submitted.raise_for_status()
    extraction = ExtractionSubmission.model_validate_json(submitted.content)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = await client.get(f"tasks/{extraction.task_id}")
        response.raise_for_status()
        task = TaskView.model_validate_json(response.content)
        match task.state:
            case TaskState.QUEUED | TaskState.RUNNING | TaskState.RETRY_WAIT:
                await asyncio.sleep(2)
            case TaskState.SUCCEEDED | TaskState.PARTIAL:
                response = await client.get(
                    f"resource-extractions/{extraction.extraction_id}/result"
                )
                response.raise_for_status()
                result = ResourceExtractionResult.model_validate_json(response.content)
                return DocumentAcceptanceOutcome(
                    filename=report.filename,
                    task_id=task.id,
                    extraction_id=extraction.extraction_id,
                    state=task.state,
                    verification=verify_golden(result, report),
                )
            case _:
                return DocumentAcceptanceOutcome(
                    filename=report.filename,
                    task_id=task.id,
                    extraction_id=extraction.extraction_id,
                    state=task.state,
                )
    return DocumentAcceptanceOutcome(
        filename=report.filename,
        task_id=extraction.task_id,
        extraction_id=extraction.extraction_id,
        state=task.state,
    )


async def run(arguments: DocumentAcceptanceRequest) -> bool:
    golden = GoldenReports.model_validate_json(
        await asyncio.to_thread(arguments.golden.read_text, encoding="utf-8")
    )
    await asyncio.to_thread(arguments.output_dir.mkdir, parents=True, exist_ok=True)
    mode = DocumentAcceptanceMode(arguments.mode)
    outcomes: list[DocumentAcceptanceOutcome] = []
    match mode:
        case DocumentAcceptanceMode.LIVE:
            token = None
            if "MINING_ACCEPTANCE_SERVICE_TOKEN" in os.environ:
                token = os.environ["MINING_ACCEPTANCE_SERVICE_TOKEN"]
            if not token:
                raise fail(
                    ErrorCode.INVALID_INPUT,
                    "Live acceptance requires --authorize-model-calls, --run-id and MINING_ACCEPTANCE_SERVICE_TOKEN",
                )
            async with httpx.AsyncClient(
                base_url=str(arguments.backend_url).rstrip("/") + "/api/v1/",
                headers=[("Authorization", f"Bearer {token}")],
                timeout=60,
                trust_env=False,
            ) as client:
                for report in golden.reports:
                    data = await validate_fixture(report, arguments.fixture_dir)
                    outcome = await run_live(
                        client,
                        report,
                        data,
                        arguments.run_id,
                        arguments.timeout_seconds,
                    )
                    outcomes.append(outcome)
                    if outcome.state in (TaskState.SUCCEEDED, TaskState.PARTIAL):
                        response = await client.get(
                            f"resource-extractions/{outcome.extraction_id}/result"
                        )
                        response.raise_for_status()
                        result = ResourceExtractionResult.model_validate_json(
                            response.content
                        )
                        await asyncio.to_thread(
                            (
                                arguments.output_dir / f"{report.filename}.result.json"
                            ).write_text,
                            result.model_dump_json(indent=2),
                            encoding="utf-8",
                        )
                    if outcome.state not in (TaskState.SUCCEEDED, TaskState.PARTIAL):
                        break
        case DocumentAcceptanceMode.VERIFY:
            for report in golden.reports:
                await validate_fixture(report, arguments.fixture_dir)
                result = ResourceExtractionResult.model_validate_json(
                    await asyncio.to_thread(
                        (
                            arguments.result_dir / f"{report.filename}.result.json"
                        ).read_text,
                        encoding="utf-8",
                    )
                )
                outcomes.append(
                    DocumentAcceptanceOutcome(
                        filename=report.filename,
                        extraction_id=result.extraction_id,
                        verification=verify_golden(result, report),
                    )
                )
    for outcome in outcomes:
        await asyncio.to_thread(
            (arguments.output_dir / f"{outcome.filename}.acceptance.json").write_text,
            outcome.model_dump_json(indent=2),
            encoding="utf-8",
        )
        print(outcome.model_dump_json())
    return len(outcomes) == len(golden.reports) and all(
        outcome.verification is not None and outcome.verification.passed
        for outcome in outcomes
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=list(DocumentAcceptanceMode), required=True)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--result-dir", type=Path)
    parser.add_argument("--backend-url", default="http://127.0.0.1:28110")
    parser.add_argument("--authorize-model-calls", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    namespace = parser.parse_args()
    arguments = DocumentAcceptanceRequest(
        mode=namespace.mode,
        golden=namespace.golden,
        fixture_dir=namespace.fixture_dir,
        output_dir=namespace.output_dir,
        result_dir=namespace.result_dir,
        backend_url=namespace.backend_url,
        authorize_model_calls=namespace.authorize_model_calls,
        run_id=namespace.run_id,
        timeout_seconds=namespace.timeout_seconds,
    )
    if (
        Path.cwd() / "AGENTS.md"
    ).exists() and arguments.output_dir.resolve().is_relative_to(Path.cwd().resolve()):
        parser.error("Acceptance output must be outside the repository")
    raise SystemExit(0 if asyncio.run(run(arguments)) else 1)


if __name__ == "__main__":
    main()
