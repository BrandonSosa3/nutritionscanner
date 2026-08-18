"""Background tasks.

Extraction is slow and network-dependent, so it runs here rather than in a
request handler. Each task opens its own session: an arq worker has no request
scope to borrow one from.
"""

from datetime import UTC, datetime
from typing import Any

from ns.db import get_sessionmaker
from ns.logging import get_logger
from ns.models import Receipt
from ns.pipeline.extract import extract_receipt

log = get_logger(__name__)


async def ping(ctx: dict[str, Any]) -> dict[str, str]:
    """Round-trip check for the queue. Returns the time the worker ran it."""
    now = datetime.now(UTC).isoformat()
    log.info("task.ping", job_id=ctx.get("job_id"), at=now)
    return {"pong": now}


async def extract_receipt_task(
    ctx: dict[str, Any], receipt_id: int, *, force: bool = False
) -> dict[str, Any]:
    """Extract one receipt.

    Failures propagate so arq retries them. The receipt keeps its image and
    its `extract_failed` status throughout, so nothing is lost between
    attempts and a permanently failing receipt stays visible for manual retry.
    """
    async with get_sessionmaker()() as session:
        receipt = await session.get(Receipt, receipt_id)
        if receipt is None:
            log.warning("task.extract.receipt_missing", receipt_id=receipt_id)
            return {"receipt_id": receipt_id, "status": "not_found"}

        outcome = await extract_receipt(session, receipt, force=force)
        await session.commit()

        return {
            "receipt_id": receipt_id,
            "status": outcome.receipt.status.value,
            "line_items": len(outcome.extraction.line_items),
            "cost_usd": str(outcome.call.cost_usd),
        }
