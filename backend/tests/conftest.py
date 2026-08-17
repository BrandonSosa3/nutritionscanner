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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ns.api.app import create_app
from ns.config import get_settings

FIXTURES = Path(__file__).parent / "fixtures"
RECEIPT_FIXTURES = FIXTURES / "receipts"


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """HTTP client bound directly to the ASGI app — no network, no server."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """A database session that rolls back everything on teardown.

    Each test runs inside a transaction that is never committed, so tests are
    order-independent and leave no residue — including the tests that
    deliberately trigger integrity errors.
    """
    engine = create_async_engine(str(get_settings().database_url), poolclass=None)
    connection = await engine.connect()
    transaction = await connection.begin()
    maker = async_sessionmaker(bind=connection, class_=AsyncSession, expire_on_commit=False)

    async with maker() as s:
        try:
            yield s
        finally:
            await s.close()
            if transaction.is_active:
                await transaction.rollback()
            await connection.close()
            await engine.dispose()
