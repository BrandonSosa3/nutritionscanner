"""Food catalogue and nutrition endpoints.

The catalogue is where resolution and nutrition meet: resolution creates a
food, enrichment attaches what it contains. A food listed here without
nutrition is not a bug to hide — it is the honest state that makes a basket
summary able to say what share of its weight it can account for.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ns.api.schemas import (
    EnrichmentResponse,
    FoodCreateRequest,
    FoodDetail,
    FoodListResponse,
    FoodSummary,
    NutrientOut,
    UsdaCandidateOut,
    UsdaOverrideRequest,
)
from ns.db import get_session
from ns.models import Food, FoodNutrient
from ns.pipeline.enrich import enrich_catalogue, enrich_food, set_food_usda
from ns.providers.usda.client import MissingUsdaKeyError, UsdaError

router = APIRouter(prefix="/foods", tags=["foods"])


async def _nutrient_counts(session: AsyncSession, food_ids: list[int]) -> dict[int, int]:
    if not food_ids:
        return {}
    rows = await session.execute(
        select(col(FoodNutrient.food_id), func.count())
        .where(col(FoodNutrient.food_id).in_(food_ids))
        .group_by(col(FoodNutrient.food_id))
    )
    return {food_id: count for food_id, count in rows.all()}  # noqa: C416


@router.get("", response_model=FoodListResponse, summary="The food catalogue")
async def list_foods(
    q: Annotated[
        str | None, Query(description="Substring of the food name, case-insensitive.")
    ] = None,
    without_nutrition: Annotated[
        bool, Query(description="Only foods with no nutrition attached yet.")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: AsyncSession = Depends(get_session),
) -> FoodListResponse:
    """Every food resolution has created, and whether nutrition is behind it.

    `q` is what the correction screen searches with — a plain case-insensitive
    substring, not a ranked search. The catalogue is the foods this user has
    actually bought, so it stays small enough that substring matching finds
    things and stays predictable, which matters more here than cleverness:
    picking the wrong food from a fuzzy list writes a permanent correction.

    `without_nutrition=true` is the review queue: foods the matcher would not
    claim automatically, each with candidates recorded for a one-tap fix.
    """
    total = (await session.execute(select(func.count()).select_from(Food))).scalar_one()
    missing = (
        await session.execute(
            select(func.count()).select_from(Food).where(col(Food.fdc_id).is_(None))
        )
    ).scalar_one()

    query = select(Food).order_by(col(Food.canonical_name))
    if without_nutrition:
        query = query.where(col(Food.fdc_id).is_(None))
    if q and q.strip():
        query = query.where(col(Food.canonical_name).ilike(f"%{q.strip()}%"))

    foods = list((await session.execute(query.limit(limit).offset(offset))).scalars().all())
    counts = await _nutrient_counts(session, [f.id for f in foods if f.id is not None])

    items = []
    for food in foods:
        summary = FoodSummary.model_validate(food)
        summary.nutrient_count = counts.get(food.id or 0, 0)
        summary.has_nutrition = summary.nutrient_count > 0
        items.append(summary)

    return FoodListResponse(items=items, total=total, without_nutrition=missing)


def _candidates_from(payload: Any) -> tuple[list[UsdaCandidateOut], str | None]:
    if not isinstance(payload, dict):
        return [], None
    raw = payload.get("considered")
    chosen_by = payload.get("chosen_by")
    if not isinstance(raw, list):
        return [], chosen_by if isinstance(chosen_by, str) else None
    out = []
    for entry in raw:
        if isinstance(entry, dict):
            try:
                out.append(UsdaCandidateOut.model_validate(entry))
            except ValueError:
                continue
    return out, chosen_by if isinstance(chosen_by, str) else None


@router.post(
    "",
    response_model=FoodDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Add a food the catalogue doesn't have",
)
async def create_food(
    body: Annotated[FoodCreateRequest, Body()],
    session: AsyncSession = Depends(get_session),
) -> FoodDetail:
    """Create a food by name, or return the one that already has that name.

    Get-or-create rather than a hard conflict: the name is the identity, and
    two rows sharing one would split that food's price history in half and make
    cost per gram of protein quietly wrong for both. A user typing a name that
    already exists means "this one", not "make another".

    Nutrition is not fetched here. That is a separate lookup, and a food is
    useful the moment it has an identity — it contributes visible uncovered
    mass until USDA data arrives, never a silent zero.
    """
    canonical = " ".join(body.canonical_name.strip().lower().split())
    if not canonical:
        raise HTTPException(status_code=422, detail="A food needs a name.")

    existing = (
        await session.execute(select(Food).where(col(Food.canonical_name) == canonical))
    ).scalar_one_or_none()

    if existing is None:
        existing = Food(canonical_name=canonical, category=body.category)
        session.add(existing)
        await session.flush()

    assert existing.id is not None
    return await get_food_detail(existing.id, session)


@router.get("/{food_id}", response_model=FoodDetail, summary="One food and its nutrition")
async def get_food_detail(food_id: int, session: AsyncSession = Depends(get_session)) -> FoodDetail:
    food = await session.get(Food, food_id)
    if food is None:
        raise HTTPException(status_code=404, detail=f"No food with id {food_id}.")

    rows = await session.execute(
        select(FoodNutrient)
        .where(col(FoodNutrient.food_id) == food_id)
        .order_by(col(FoodNutrient.nutrient_code))
    )
    nutrients = [NutrientOut.model_validate(n) for n in rows.scalars().all()]
    candidates, chosen_by = _candidates_from(food.usda_payload)

    detail = FoodDetail.model_validate(food)
    detail.nutrients = nutrients
    detail.nutrient_count = len(nutrients)
    detail.has_nutrition = bool(nutrients)
    detail.candidates = candidates
    detail.chosen_by = chosen_by
    return detail


@router.post("/enrich", response_model=EnrichmentResponse, summary="Fetch missing nutrition")
async def enrich(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    force: Annotated[bool, Query(description="Re-fetch foods already matched.")] = False,
    session: AsyncSession = Depends(get_session),
) -> EnrichmentResponse:
    """Look up foods in FoodData Central and store what they contain.

    Free apart from an HTTP request, and every response is cached on disk, so
    re-running costs no quota. One food failing does not stop the run.
    """
    try:
        result = await enrich_catalogue(session, limit=limit, force=force)
    except MissingUsdaKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return EnrichmentResponse(
        attempted=result.attempted,
        enriched=result.enriched,
        unmatched=result.unmatched,
        failed=result.failed,
        coverage=round(result.coverage, 4),
    )


@router.post(
    "/{food_id}/usda",
    response_model=FoodDetail,
    summary="Attach a specific FoodData Central entry",
)
async def override_usda(
    food_id: int,
    body: Annotated[UsdaOverrideRequest, Body()],
    session: AsyncSession = Depends(get_session),
) -> FoodDetail:
    """Choose the USDA entry for a food the matcher would not claim.

    The automatic matcher requires every term of the canonical name to appear
    in the candidate, which leaves real foods unmatched rather than risking the
    wrong ones. This is the one-tap fix, and the choice is recorded as the
    user's so it is never mistaken for an automatic match.
    """
    food = await session.get(Food, food_id)
    if food is None:
        raise HTTPException(status_code=404, detail=f"No food with id {food_id}.")

    try:
        await set_food_usda(session, food, body.fdc_id)
    except MissingUsdaKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except UsdaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return await get_food_detail(food_id, session)


@router.post("/{food_id}/enrich", response_model=FoodDetail, summary="Fetch nutrition for one food")
async def enrich_one(
    food_id: int,
    force: Annotated[bool, Query()] = False,
    session: AsyncSession = Depends(get_session),
) -> FoodDetail:
    food = await session.get(Food, food_id)
    if food is None:
        raise HTTPException(status_code=404, detail=f"No food with id {food_id}.")

    try:
        await enrich_food(session, food, force=force)
    except MissingUsdaKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except UsdaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return await get_food_detail(food_id, session)
