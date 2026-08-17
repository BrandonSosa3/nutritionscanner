"""arq worker.

Extraction is slow and network-dependent, so it runs here rather than in a
request handler. Pipeline stages get registered as they are built; the worker
process is wired up from the start so deployment topology never changes later.
"""

from typing import Any, ClassVar

from arq.connections import RedisSettings

from ns.config import get_settings
from ns.jobs.tasks import ping
from ns.logging import configure_logging, get_logger

log = get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    log.info("worker.startup")


async def shutdown(ctx: dict[str, Any]) -> None:
    from ns.db import dispose_engine

    await dispose_engine()
    log.info("worker.shutdown")


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(str(get_settings().redis_url))


class WorkerSettings:
    """arq entry point: `arq ns.jobs.worker.WorkerSettings`."""

    functions: ClassVar[list[Any]] = [ping]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()

    # A receipt that fails is retried, never dropped. After the final attempt
    # the receipt row keeps its failed status and stays replayable by hand.
    max_tries = 3
    job_timeout = 300
    keep_result = 3600
