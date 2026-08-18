"""Correction endpoints — the core product loop.

A correction is not a settings change. It is stored, applied to every line it
matches including on receipts already processed, and recorded as a label the
resolver is scored against. All three happen on one request.
"""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ns.api.schemas import CorrectionRequest, CorrectionResponse
from ns.db import get_session
from ns.logging import get_logger
from ns.models import Food, LineItem, Receipt
from ns.models.enums import LabelSource
from ns.pipeline.corrections import confirm_resolution, record_correction

router = APIRouter(prefix="/line-items", tags=["corrections"])
log = get_logger(__name__)


async def _line_and_store(session: AsyncSession, line_item_id: int) -> tuple[LineItem, int | None]:
    line = await session.get(LineItem, line_item_id)
    if line is None:
        raise HTTPException(status_code=404, detail=f"No line item with id {line_item_id}.")
    receipt = await session.get(Receipt, line.receipt_id)
    return line, receipt.store_id if receipt else None


@router.post(
    "/{line_item_id}/correct",
    response_model=CorrectionResponse,
    summary="Fix what a line resolved to",
)
async def correct(
    line_item_id: int,
    body: Annotated[CorrectionRequest, Body()],
    session: AsyncSession = Depends(get_session),
) -> CorrectionResponse:
    """Record a fix, apply it everywhere, and label it for evaluation.

    The fix is permanent and compounds: every future receipt with this text at
    this store resolves from the correction without a model call, and every
    past receipt is updated too — a correction that only affected new receipts
    would leave the user's own history disagreeing with their own fix.
    """
    line, store_id = await _line_and_store(session, line_item_id)

    if body.food_id is not None:
        if await session.get(Food, body.food_id) is None:
            raise HTTPException(status_code=422, detail=f"No food with id {body.food_id}.")
    elif not body.is_nonfood:
        raise HTTPException(
            status_code=422,
            detail="A correction needs either a food to point at or is_nonfood set.",
        )

    result = await record_correction(
        session,
        line,
        food_id=body.food_id,
        is_nonfood=body.is_nonfood,
        grams_basis=body.grams_basis,
        grams_value=body.grams_value,
        store_id=store_id,
        global_scope=body.global_scope,
    )

    assert result.correction.id is not None and result.example.id is not None
    return CorrectionResponse(
        line_item_id=line_item_id,
        correction_id=result.correction.id,
        applied_to_line_items=result.applied_to,
        eval_example_id=result.example.id,
        split=result.example.split,
        label_source=LabelSource.CORRECTED,
    )


@router.post(
    "/{line_item_id}/confirm",
    response_model=CorrectionResponse,
    summary="Confirm that a line resolved correctly",
)
async def confirm(
    line_item_id: int,
    session: AsyncSession = Depends(get_session),
) -> CorrectionResponse:
    """Record that the resolver got this one right.

    No correction is written — there is nothing to override. What this creates
    is a label. Without confirmations the eval set is entirely cases the
    resolver failed, a biased sample that can never show an improvement (D6).
    """
    line, store_id = await _line_and_store(session, line_item_id)

    try:
        example = await confirm_resolution(session, line, store_id=store_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    assert example.id is not None
    return CorrectionResponse(
        line_item_id=line_item_id,
        correction_id=0,  # a confirmation writes no correction
        applied_to_line_items=0,
        eval_example_id=example.id,
        split=example.split,
        label_source=LabelSource.CONFIRMED,
    )
