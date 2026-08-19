"""Basket summary and the cost-per-nutrient ranking.

Every response here leads with coverage. A nutrient total computed from 60% of
a basket is a lower bound, and the caveat has to arrive with the number rather
than in a footnote a client can drop.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ns.analysis.summary import Coverage, rank_by_nutrient_cost, summarise_receipts
from ns.api.schemas import BasketSummaryResponse, CoverageOut, NutrientCostResponse, NutrientCostRow
from ns.db import get_session
from ns.domain.nutrition import BY_CODE, PROTEIN
from ns.models import Receipt

router = APIRouter(prefix="/summary", tags=["summary"])


@router.get("", response_model=BasketSummaryResponse, summary="What these groceries contained")
async def basket_summary(
    receipt_id: Annotated[
        list[int] | None, Query(description="Restrict to these receipts.")
    ] = None,
    starts_on: Annotated[date | None, Query()] = None,
    ends_on: Annotated[date | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> BasketSummaryResponse:
    """Total what a set of receipts *contained*.

    Supply, not intake: these are groceries bought, not food eaten. The
    framing is deliberate and the response carries it, because a week's shop
    is not a week's meals.
    """
    query = select(Receipt)
    if receipt_id:
        query = query.where(col(Receipt.id).in_(receipt_id))
    if starts_on:
        query = query.where(col(Receipt.purchased_at) >= starts_on)
    if ends_on:
        query = query.where(col(Receipt.purchased_at) <= ends_on)

    receipts = list((await session.execute(query)).scalars().all())
    summary = await summarise_receipts(session, receipts)
    coverage = summary.coverage

    return BasketSummaryResponse(
        receipt_ids=summary.receipt_ids,
        starts_on=summary.starts_on,
        ends_on=summary.ends_on,
        currency=summary.currency,
        total_spend_cents=summary.total_spend_cents,
        nutrients={code: str(amount) for code, amount in sorted(summary.nutrients.items())},
        units=summary.units,
        coverage=CoverageOut(
            lines_total=coverage.lines_total,
            lines_resolved=coverage.lines_resolved,
            lines_with_nutrition=coverage.lines_with_nutrition,
            spend_share=round(coverage.spend_share, 4),
            weight_share=round(coverage.weight_share, 4),
            grams_total=str(coverage.grams_total),
            grams_with_nutrition=str(coverage.grams_with_nutrition),
            unresolved_lines=coverage.unresolved_lines,
            lines_without_nutrition=coverage.lines_without_nutrition,
            lines_without_weight=coverage.lines_without_weight,
            is_partial=coverage.is_partial,
        ),
        headline=_headline(summary.coverage),
    )


def _headline(coverage: Coverage) -> str:
    """The sentence a partial basket has to lead with (principle 6).

    Built server-side rather than in the client so every caller says the same
    thing, and so a client cannot render the totals without it.
    """
    if coverage.lines_total == 0:
        return "No receipts match."
    if not coverage.is_partial:
        return "Every line in this basket has nutrition data behind it."

    reasons = []
    if coverage.unresolved_lines:
        reasons.append(f"{coverage.unresolved_lines} not yet identified")
    if coverage.lines_without_nutrition:
        reasons.append(f"{coverage.lines_without_nutrition} without nutrition data")
    if coverage.lines_without_weight:
        reasons.append(f"{coverage.lines_without_weight} without a weight")
    detail = f" ({', '.join(reasons)})" if reasons else ""

    return (
        f"These totals cover {coverage.weight_share:.0%} of this basket's weight "
        f"and {coverage.spend_share:.0%} of its spend"
        f"{detail}. Everything below is a lower bound."
    )


@router.get(
    "/cost-per-nutrient",
    response_model=NutrientCostResponse,
    summary="Foods ranked by what a nutrient costs",
)
async def cost_per_nutrient(
    nutrient: Annotated[str, Query(description="A nutrient code, e.g. protein_g.")] = PROTEIN,
    store_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    session: AsyncSession = Depends(get_session),
) -> NutrientCostResponse:
    """Cheapest source of a nutrient first.

    The baseline for each food is the **median of its non-discounted** price
    observations: a sale is not what a food costs, and a mean would let one
    clearance buy define a food's price forever.
    """
    if nutrient not in BY_CODE:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown nutrient {nutrient!r}. Known: {', '.join(sorted(BY_CODE))}.",
        )

    rows = await rank_by_nutrient_cost(session, nutrient, store_id=store_id, limit=limit)
    spec = BY_CODE[nutrient]

    return NutrientCostResponse(
        nutrient=nutrient,
        label=spec.label,
        unit=spec.unit,
        items=[
            NutrientCostRow(
                food_id=row.food_id,
                canonical_name=row.canonical_name,
                observations=row.observations,
                median_price_cents_per_100g=str(row.median_price_cents_per_100g),
                nutrient_per_100g=str(row.nutrient_per_100g),
                cost_cents_per_unit=str(row.cost_cents_per_unit),
                from_receipt_weights=row.from_receipt_weights,
            )
            for row in rows
        ],
    )
