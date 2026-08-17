"""Liveness and readiness endpoints.

Deployment platforms need these distinct: liveness answers "is the process
alive" (restart me if not), readiness answers "can I serve traffic" (hold the
load balancer back if not). Conflating them causes restart loops when a
dependency is briefly unavailable.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ns.db import get_session
from ns.logging import get_logger

router = APIRouter(tags=["health"])
log = get_logger(__name__)


@router.get("/health")
async def health() -> dict[str, Literal["ok"]]:
    """Liveness. Touches nothing external — if the process answers, it is alive."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Readiness. Verifies the database is actually reachable."""
    checks: dict[str, str] = {}

    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        log.error("readiness.database_failed", error=str(exc))
        checks["database"] = "unavailable"

    ready = all(v == "ok" for v in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"ready": ready, "checks": checks}
