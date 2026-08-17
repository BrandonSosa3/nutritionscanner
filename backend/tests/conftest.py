"""Shared test fixtures.

Environment defaults are set before anything imports `ns.config`, so unit tests
run with no `.env` file and no live services. Tests that need a real database
are marked `integration` and read `DATABASE_URL` from the environment.
"""

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

os.environ.setdefault("ENVIRONMENT", "ci")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://ns:ns_local_dev@localhost:5433/nutritionscanner",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from httpx import ASGITransport, AsyncClient

from ns.api.app import create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """HTTP client bound directly to the ASGI app — no network, no server."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
