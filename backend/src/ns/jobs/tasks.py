"""Background tasks.

Pipeline stages are registered here as they are built. `ping` exists so the
queue is verifiable end to end — enqueue it and confirm the worker executed it —
and because arq refuses to start a worker with no registered functions.
"""

from datetime import UTC, datetime
from typing import Any

from ns.logging import get_logger

log = get_logger(__name__)


async def ping(ctx: dict[str, Any]) -> dict[str, str]:
    """Round-trip check for the queue. Returns the time the worker ran it."""
    now = datetime.now(UTC).isoformat()
    log.info("task.ping", job_id=ctx.get("job_id"), at=now)
    return {"pong": now}
