"""Evaluation endpoints.

The resolver's accuracy is a number the product shows, not an internal
metric — principle 4 says measure it, and principle 6 says surface it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ns.api.schemas import EvalRunListResponse, EvalRunResponse
from ns.db import get_session
from ns.eval.harness import run_eval
from ns.models import ResolverRun
from ns.models.enums import EvalSplit
from ns.providers.anthropic.budget import BudgetExceededError
from ns.providers.anthropic.client import MissingApiKeyError

router = APIRouter(prefix="/eval", tags=["eval"])


@router.post("/runs", response_model=EvalRunResponse, summary="Score the resolver")
async def create_run(
    split: Annotated[EvalSplit, Query()] = EvalSplit.HOLDOUT,
    threshold: Annotated[float | None, Query(ge=0, le=1)] = None,
    notes: Annotated[str | None, Query(max_length=2000)] = None,
    session: AsyncSession = Depends(get_session),
) -> EvalRunResponse:
    """Run the resolver against a labeled split and record the score.

    Costs money — it is a batch of real model calls. Run it deliberately after
    a prompt change, not on every receipt.
    """
    try:
        report = await run_eval(session, split=split, threshold=threshold, notes=notes)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MissingApiKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except BudgetExceededError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc

    return EvalRunResponse.model_validate(report.run)


@router.get("/runs", response_model=EvalRunListResponse, summary="Score history")
async def list_runs(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    session: AsyncSession = Depends(get_session),
) -> EvalRunListResponse:
    """Newest first. The trend line, which is the point of storing runs at all.

    A run is only comparable to another with the same model, prompt version,
    and normaliser version; all three are on every row for exactly that reason.
    """
    total = (await session.execute(select(func.count()).select_from(ResolverRun))).scalar_one()
    rows = await session.execute(
        select(ResolverRun).order_by(col(ResolverRun.run_at).desc()).limit(limit)
    )
    return EvalRunListResponse(
        items=[EvalRunResponse.model_validate(run) for run in rows.scalars().all()],
        total=total,
    )
