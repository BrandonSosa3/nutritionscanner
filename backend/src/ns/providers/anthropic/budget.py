"""Application-side spend guard.

The Console's spend limit protects the account; this protects the project. It
checks the cost already recorded in `llm_call` for the current calendar month
against a configured ceiling, and refuses to make a call that would exceed it.

Failing here rather than at the provider matters: the error is ours, the
message says what to do, and the receipt stays safely stored and queued
instead of half-processed.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ns.config import get_settings
from ns.logging import get_logger
from ns.models import LlmCall

log = get_logger(__name__)


class BudgetExceededError(RuntimeError):
    """The configured monthly spend ceiling would be exceeded by this call."""


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    limit_usd: Decimal | None
    spent_usd: Decimal
    month: date

    @property
    def remaining_usd(self) -> Decimal | None:
        if self.limit_usd is None:
            return None
        return self.limit_usd - self.spent_usd

    @property
    def is_exhausted(self) -> bool:
        remaining = self.remaining_usd
        return remaining is not None and remaining <= 0


def _month_start(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def get_budget_status(session: AsyncSession, *, now: datetime | None = None) -> BudgetStatus:
    """Spend recorded so far this calendar month, against the configured cap."""
    start = _month_start(now)
    spent = (
        await session.execute(
            select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(
                col(LlmCall.called_at) >= start
            )
        )
    ).scalar_one()

    return BudgetStatus(
        limit_usd=get_settings().monthly_budget_usd,
        spent_usd=Decimal(spent),
        month=start.date(),
    )


async def assert_within_budget(
    session: AsyncSession,
    estimated_cost_usd: Decimal,
    *,
    now: datetime | None = None,
) -> BudgetStatus:
    """Raise BudgetExceededError if this call would cross the ceiling.

    Uses an estimate rather than the actual cost, because the actual cost is
    only known after the call has already been paid for.
    """
    status = await get_budget_status(session, now=now)

    if status.limit_usd is None:
        return status

    projected = status.spent_usd + estimated_cost_usd
    if projected > status.limit_usd:
        log.warning(
            "budget.exceeded",
            limit_usd=str(status.limit_usd),
            spent_usd=str(status.spent_usd),
            estimated_usd=str(estimated_cost_usd),
            month=status.month.isoformat(),
        )
        raise BudgetExceededError(
            f"This call is estimated at ${estimated_cost_usd:.4f}, which would take "
            f"{status.month:%B} spending to ${projected:.4f}, above the "
            f"${status.limit_usd:.2f} monthly limit. "
            f"Raise MONTHLY_BUDGET_USD or wait until next month. "
            f"The receipt is saved and will be processed when the budget allows."
        )

    return status
