from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from mining_server.domain.documents import (
    DocumentAcceptanceMode,
    DocumentAcceptanceRequest,
    DocumentResponse,
    DocumentStatus,
    ExtractionSubmission,
    GoldenReports,
)
from mining_server.domain.task_views import TaskView
from mining_server.domain.tasks import TaskKind, TaskState
from mining_server.infrastructure.documents.acceptance import run_live


def test_live_acceptance_requires_explicit_authorization_and_run_identity(tmp_path):
    with pytest.raises(ValidationError):
        DocumentAcceptanceRequest(
            mode=DocumentAcceptanceMode.LIVE,
            golden=tmp_path / "golden.json",
            fixture_dir=tmp_path,
            output_dir=tmp_path,
        )


@pytest.mark.parametrize(
    "state", [TaskState.UNKNOWN, TaskState.WAITING_AUTH, TaskState.WAITING_QUOTA]
)
async def test_live_acceptance_stops_without_replaying_uncertain_or_waiting_tasks(
    state,
):
    report = GoldenReports.model_validate_json(
        (Path(__file__).parent / "fixtures/document-golden.json").read_text(
            encoding="utf-8"
        )
    ).reports[0]
    task_id = uuid4()
    extraction_id = uuid4()
    now = datetime.now(UTC)
    requests: list[httpx.Request] = []

    def respond(request):
        requests.append(request)
        match request.url.path:
            case "/api/v1/documents/upload":
                model = DocumentResponse(
                    id=uuid4(),
                    sha256=report.sha256,
                    size_bytes=10,
                    status=DocumentStatus.READY,
                    created_at=now,
                )
            case "/api/v1/resource-extractions":
                model = ExtractionSubmission(
                    extraction_id=extraction_id, task_id=task_id
                )
            case _:
                model = TaskView(
                    id=task_id,
                    kind=TaskKind.RESOURCE_EXTRACTION,
                    state=state,
                    generation=1,
                    workflow_version="1",
                    input_revision="1",
                    checkpoint_seq=0,
                    next_step=None,
                    result=None,
                    error_code=None,
                    error_message=None,
                    created_at=now,
                    updated_at=now,
                )
        return httpx.Response(200, json=model.model_dump(mode="json"))

    async with httpx.AsyncClient(
        base_url="http://localhost/api/v1/", transport=httpx.MockTransport(respond)
    ) as client:
        result = await run_live(client, report, b"%PDF-test", "stable-test-run", 5)
    assert result.state == state
    assert result.verification is None
    assert len(requests) == 3
    assert all(not request.url.path.endswith("/actions") for request in requests)
