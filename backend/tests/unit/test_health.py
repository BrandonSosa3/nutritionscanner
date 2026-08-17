"""Health endpoint and configuration smoke tests.

These are deliberately the first tests in the repo: they prove the app boots,
settings resolve from the environment, and the ASGI wiring works — before any
domain logic exists to test.
"""

import pytest
from httpx import AsyncClient

from ns.config import Environment, Settings


async def test_liveness_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_settings_reject_unknown_variables() -> None:
    """`extra="forbid"` means a mistyped env var fails loudly instead of
    being silently ignored."""
    with pytest.raises(ValueError, match="totally_made_up_setting"):
        Settings(
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
            redis_url="redis://localhost:6379/0",  # type: ignore[arg-type]
            totally_made_up_setting="x",
        )


async def test_cors_origins_accept_comma_separated_string() -> None:
    """PaaS platforms supply list-valued env vars as comma-separated strings."""
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
        redis_url="redis://localhost:6379/0",  # type: ignore[arg-type]
        cors_origins="https://a.example, https://b.example",
    )
    assert settings.cors_origin_list == ["https://a.example", "https://b.example"]


async def test_production_flag_and_sync_url() -> None:
    settings = Settings(
        environment=Environment.PRODUCTION,
        database_url="postgresql+asyncpg://u:p@db.example:5432/ns",  # type: ignore[arg-type]
        redis_url="redis://localhost:6379/0",  # type: ignore[arg-type]
    )
    assert settings.is_production is True
    assert settings.sync_database_url.startswith("postgresql://")
    assert "asyncpg" not in settings.sync_database_url


@pytest.mark.integration
async def test_readiness_reports_database(client: AsyncClient) -> None:
    """Requires a live Postgres. Deselect with -m 'not integration'."""
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["checks"]["database"] == "ok"
