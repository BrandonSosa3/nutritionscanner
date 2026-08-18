"""The application-side spend guard, against recorded LlmCall costs."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ns.models import LlmCall
from ns.models.enums import LlmStage
from ns.providers.anthropic.budget import (
    BudgetExceededError,
    assert_within_budget,
    get_budget_status,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


async def _record(session: AsyncSession, cost: str, *, when: datetime = NOW) -> None:
    session.add(
        LlmCall(
            stage=LlmStage.EXTRACT,
            model="claude-opus-5",
            called_at=when,
            input_tokens=6000,
            output_tokens=2500,
            latency_ms=4200,
            cost_usd=Decimal(cost),
        )
    )
    await session.flush()


async def test_status_sums_only_the_current_month(session: AsyncSession) -> None:
    """Spending resets on the first of the month, like the Console limit."""
    await _record(session, "0.09")
    await _record(session, "0.09", when=NOW - timedelta(days=45))  # previous month

    status = await get_budget_status(session, now=NOW)

    assert status.spent_usd == Decimal("0.09")
    assert status.month.isoformat() == "2026-08-01"


async def test_call_within_budget_is_allowed(session: AsyncSession) -> None:
    await _record(session, "1.00")
    status = await assert_within_budget(session, Decimal("0.09"), now=NOW)
    assert status.remaining_usd is not None
    assert status.remaining_usd > 0


async def test_call_that_would_cross_the_ceiling_is_refused(
    session: AsyncSession,
) -> None:
    """Default limit is $10.00."""
    await _record(session, "9.98")

    with pytest.raises(BudgetExceededError) as exc:
        await assert_within_budget(session, Decimal("0.09"), now=NOW)

    message = str(exc.value)
    # The message is shown to a user, so it must say what to do and reassure
    # them that nothing was lost.
    assert "MONTHLY_BUDGET_USD" in message
    assert "receipt is saved" in message


async def test_guard_blocks_before_the_limit_is_actually_reached(
    session: AsyncSession,
) -> None:
    """Checked against an estimate, because the real cost is only known after
    the call has already been paid for."""
    await _record(session, "9.95")

    # A small call still fits.
    await assert_within_budget(session, Decimal("0.04"), now=NOW)

    # A large one does not, even though nothing has been spent in between.
    with pytest.raises(BudgetExceededError):
        await assert_within_budget(session, Decimal("1.00"), now=NOW)


async def test_exhausted_status_is_reported(session: AsyncSession) -> None:
    await _record(session, "10.50")
    status = await get_budget_status(session, now=NOW)
    assert status.is_exhausted is True
    assert status.remaining_usd == Decimal("-0.50")


async def test_no_limit_configured_disables_the_guard(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "monthly_budget_usd", None, raising=False)

    await _record(session, "9999.00")

    status = await assert_within_budget(session, Decimal("500.00"), now=NOW)
    assert status.limit_usd is None
    assert status.remaining_usd is None
    assert status.is_exhausted is False
