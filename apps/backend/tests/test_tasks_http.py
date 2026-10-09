from uuid import uuid4

import httpx
import pytest
from apps.backend.tests.test_tasks_recovery import Input, service
from mining_contracts.domain.core import ErrorCode
from mining_contracts.domain.tasks import TaskKind

from mining_server.api.app import create_app
from mining_server.api.dependencies import ApplicationState
from mining_server.application.health import HealthService

__all__ = ["service"]


async def test_admin_runtime_settings_and_service_task_query(service):
    app = create_app(service.settings, start_publisher=False)
    app.state.runtime = ApplicationState(
        settings=service.settings,
        database=service.database,
        health_service=HealthService(
            service.database, service.settings, None, lambda: False
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/settings")
        assert response.status_code == 401
        assert response.json()["code"] == ErrorCode.UNAUTHORIZED.value
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        service_headers = {
            "Authorization": f"Bearer {service.settings.service_token.get_secret_value()}"
        }
        assert (
            await client.get("/api/v1/settings", headers=service_headers)
        ).status_code == 401
        admin_headers = {
            "Authorization": f"Bearer {service.settings.admin_token.get_secret_value()}"
        }
        response = await client.get("/api/v1/settings", headers=admin_headers)
        assert response.status_code == 200
        original = response.json()
        revision = original.pop("revision")
        response = await client.put(
            "/api/v1/settings",
            headers=admin_headers,
            json={"expected_revision": revision, "settings": original},
        )
        assert response.status_code == 200
        assert response.json()["revision"] == revision + 1
        response = await client.put(
            "/api/v1/settings",
            headers=admin_headers,
            json={"expected_revision": revision, "settings": original},
        )
        assert response.status_code == 409
        assert response.json()["code"] == ErrorCode.CONFLICT.value
        original["document_model_rounds"] = 0
        response = await client.put(
            "/api/v1/settings",
            headers=admin_headers,
            json={"expected_revision": revision + 1, "settings": original},
        )
        assert response.status_code == 422
        assert response.json()["code"] == ErrorCode.INVALID_INPUT.value
        original["document_model_rounds"] = 20
        for name in ("", "bad model", " gpt-6.1-sol"):
            original["model_name"] = name
            response = await client.put(
                "/api/v1/settings",
                headers=admin_headers,
                json={"expected_revision": revision + 1, "settings": original},
            )
            assert response.status_code == 422
        original["model_name"] = "another-model"
        response = await client.put(
            "/api/v1/settings",
            headers=admin_headers,
            json={"expected_revision": revision + 1, "settings": original},
        )
        assert response.status_code == 200
        assert response.json()["model_name"] == "another-model"
        task = await service.submit(TaskKind.FETCH_ARTICLE, Input(value=1), "http")
        response = await client.get(f"/api/v1/tasks/{task.id}", headers=service_headers)
        assert response.status_code == 200
        assert response.json()["id"] == str(task.id)
        response = await client.get(f"/api/v1/tasks/{uuid4()}", headers=service_headers)
        assert response.status_code == 404
        response = await client.post(
            f"/api/v1/tasks/{task.id}/actions",
            headers=service_headers,
            json={"operation": "cancel"},
        )
        assert response.status_code == 401


def test_invalid_explicit_configuration_fails(service):
    from pydantic import ValidationError

    from mining_server.infrastructure.settings import Settings

    valid = service.settings.model_dump()
    for field, value in [
        ("database_url", "sqlite+aiosqlite://"),
        ("database_url", "postgresql+asyncpg://"),
        ("database_url", "postgresql+asyncpg://host"),
        ("database_url", "postgresql+asyncpg://host:70000/db"),
        ("broker_url", "redis://localhost"),
        ("broker_url", "amqp://"),
        ("broker_url", "amqp://host:invalid//"),
        ("oauth_host_id", "fixed-host"),
        ("master_key", "invalid-key"),
        ("lease_seconds", 0),
        ("model_name", ""),
        ("model_name", "   "),
        ("service_token", valid["admin_token"]),
    ]:
        with pytest.raises(ValidationError):
            Settings(**(valid | {field: value}))
    assert Settings(
        **(valid | {"broker_url": "amqp://guest:guest@localhost:5672//"})
    ).broker_url.endswith("//")
