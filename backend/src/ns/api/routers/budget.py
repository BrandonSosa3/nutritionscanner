"""Spend visibility.

The Console shows account-wide usage; this shows what this project has spent,
from the costs recorded against every call it made.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ns.api.schemas import BudgetStatusResponse
from ns.db import get_session
from ns.models import LlmCall
from ns.providers.anthropic.budget import get_budget_status

router = APIRouter(prefix="/budget", tags=["budget"])


@router.get("", response_model=BudgetStatusResponse, summary="This month's LLM spend")
async def budget_status(session: AsyncSession = Depends(get_session)) -> BudgetStatusResponse:
    status = await get_budget_status(session)

    call_count = (
        await session.execute(
            select(func.count()).select_from(LlmCall).where(col(LlmCall.called_at) >= status.month)
        )
    ).scalar_one()

    return BudgetStatusResponse(
        month=status.month,
        limit_usd=str(status.limit_usd) if status.limit_usd is not None else None,
        spent_usd=str(status.spent_usd),
        remaining_usd=str(status.remaining_usd) if status.remaining_usd is not None else None,
        is_exhausted=status.is_exhausted,
        call_count=call_count,
    )
