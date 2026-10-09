import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mining_server.domain.health import HealthEvidence, HealthOrigin, HealthProbe
from mining_server.infrastructure.settings import Settings


def evidence_path(settings: Settings, origin: HealthOrigin) -> Path:
    match origin:
        case HealthOrigin.API:
            return settings.data_dir / "health" / "worker.json"
        case HealthOrigin.BEAT:
            return settings.data_dir / "health" / "beat.json"


def save_evidence(settings: Settings, probe: HealthProbe) -> None:
    path = evidence_path(settings, probe.origin)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    evidence = HealthEvidence(
        origin=probe.origin,
        nonce=probe.nonce,
        requested_at=probe.requested_at,
        observed_at=datetime.now(UTC),
    )
    try:
        temporary.write_text(evidence.model_dump_json(), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
