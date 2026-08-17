"""Shared test fixtures.

Environment defaults are set before anything imports `ns.config`, so unit tests
run with no `.env` file and no live services. Tests that need a real database
are marked `integration` and read `DATABASE_URL` from the environment.

Two isolation guarantees hold for every test:

1. Nothing is committed. Each test runs inside a transaction that is rolled
   back on teardown, so tests are order-independent and the development
   database is never polluted by a test run.
2. No test writes to the real receipt storage directory. `RECEIPT_STORAGE_PATH`
   points at a temporary directory that is removed afterwards.
"""

import os
import shutil
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

_TEST_STORAGE = tempfile.mkdtemp(prefix="ns-test-receipts-")

os.environ.setdefault("ENVIRONMENT", "ci")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://ns:ns_local_dev@localhost:5433/nutritionscanner",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ["RECEIPT_STORAGE_PATH"] = _TEST_STORAGE

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ns.api.app import create_app
from ns.config import get_settings
from ns.db import get_session

FIXTURES = Path(__file__).parent / "fixtures"
RECEIPT_FIXTURES = FIXTURES / "receipts"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    shutil.rmtree(_TEST_STORAGE, ignore_errors=True)


@pytest.fixture
async def db_connection() -> AsyncGenerator[tuple[AsyncSession, object], None]:
    """A connection held open in a transaction that is always rolled back.

    A fresh engine per test is deliberate: the application caches one engine
    at module level, and reusing it across tests binds asyncpg connections to
    an event loop that pytest-asyncio has already closed.

    `join_transaction_mode="create_savepoint"` makes any `commit()` inside the
    application code release a savepoint rather than end the outer
    transaction, so request handlers can commit normally and the whole test
    still rolls back.
    """
    engine = create_async_engine(str(get_settings().database_url))
    connection = await engine.connect()
    transaction = await connection.begin()
    maker = async_sessionmaker(
        bind=connection,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    session = maker()
    try:
        yield session, maker
    finally:
        await session.close()
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
async def session(
    db_connection: tuple[AsyncSession, object],
) -> AsyncGenerator[AsyncSession, None]:
    yield db_connection[0]


@pytest.fixture
async def client(
    db_connection: tuple[AsyncSession, object],
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client bound directly to the ASGI app — no network, no server.

    The database dependency is overridden so requests share the test's
    rolled-back transaction instead of committing to the real database.
    """
    _, maker = db_connection
    app = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as request_session:  # type: ignore[operator]
            try:
                yield request_session
                await request_session.commit()
            except Exception:
                await request_session.rollback()
                raise

    app.dependency_overrides[get_session] = override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
