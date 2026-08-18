"""Attaching nutrition to a resolved food.

The USDA client is stubbed with the real captured payloads, so nothing here
touches the network or needs a key, while still exercising the actual response
shapes.
"""

import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.models import Food, FoodNutrient
from ns.pipeline.enrich import enrich_catalogue, enrich_food, set_food_usda
from ns.providers.usda.client import UsdaError
from tests.conftest import FIXTURES

pytestmark = pytest.mark.integration


def payload(name: str) -> dict[str, Any]:
    path = FIXTURES / "usda" / f"{name}.json"
    if not path.is_file():
        pytest.skip(f"USDA fixture {name} not present")
    return json.loads(path.read_text())


def patch_usda(
    *, search: dict[str, Any] | None = None, detail: dict[str, Any] | None = None
) -> Any:
    """Stub both USDA endpoints with real payloads."""
    search_payload = search if search is not None else payload("search-chicken-breast")
    detail_payload = detail if detail is not None else payload("food-2646170-chicken-breast")

    return patch.multiple(
        "ns.pipeline.enrich",
        search_foods=AsyncMock(return_value=search_payload),
        get_food=AsyncMock(return_value=detail_payload),
    )


async def make_food(session: AsyncSession, name: str) -> Food:
    existing = (
        await session.execute(select(Food).where(col(Food.canonical_name) == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    food = Food(canonical_name=name)
    session.add(food)
    await session.flush()
    return food


async def nutrients_of(session: AsyncSession, food: Food) -> dict[str, FoodNutrient]:
    rows = await session.execute(select(FoodNutrient).where(col(FoodNutrient.food_id) == food.id))
    return {n.nutrient_code: n for n in rows.scalars().all()}


# ── A match ───────────────────────────────────────────────────────────────


async def test_a_matched_food_gets_its_nutrients(session: AsyncSession) -> None:
    food = await make_food(session, "chicken breast, boneless skinless, raw")

    with patch_usda():
        result = await enrich_food(session, food)

    assert result.enriched
    assert food.fdc_id == 2646170
    assert food.fdc_data_type == "Foundation"

    nutrients = await nutrients_of(session, food)
    assert nutrients["protein_g"].amount_per_100g == Decimal("22.525")
    assert nutrients["protein_g"].unit == "g"
    assert result.nutrients_written == len(nutrients)


async def test_the_provenance_records_what_was_considered(session: AsyncSession) -> None:
    """So a match that looks wrong months later is explainable rather than
    mysterious, and a scoring change can be judged against past decisions."""
    food = await make_food(session, "chicken breast, boneless skinless, raw")

    with patch_usda():
        await enrich_food(session, food)

    assert food.usda_payload is not None
    considered = food.usda_payload["considered"]
    assert isinstance(considered, list)
    assert len(considered) == 10
    rejected = [c for c in considered if c["rejected_reason"]]
    assert any("states disagree" in str(c["rejected_reason"]) for c in rejected)
    assert food.usda_payload["chosen_fdc_id"] == 2646170


async def test_re_running_does_not_re_fetch(session: AsyncSession) -> None:
    """So enriching a whole catalogue only fetches what is missing."""
    food = await make_food(session, "chicken breast, boneless skinless, raw")

    with patch_usda():
        await enrich_food(session, food)

    search = AsyncMock(return_value=payload("search-chicken-breast"))
    with patch("ns.pipeline.enrich.search_foods", new=search):
        result = await enrich_food(session, food)

    assert search.await_count == 0
    assert result.nutrients_written > 0  # reports what is already stored


async def test_force_refetches_and_replaces(session: AsyncSession) -> None:
    """Replaced rather than merged: a stale nutrient from an earlier match
    would otherwise survive and quietly contribute to totals."""
    food = await make_food(session, "chicken breast, boneless skinless, raw")
    with patch_usda():
        await enrich_food(session, food)

    session.add(
        FoodNutrient(
            food_id=food.id, nutrient_code="stale_g", amount_per_100g=Decimal("99"), unit="g"
        )
    )
    await session.flush()

    with patch_usda():
        await enrich_food(session, food, force=True)

    assert "stale_g" not in await nutrients_of(session, food)


# ── No match ──────────────────────────────────────────────────────────────


async def test_an_unmatched_food_keeps_its_identity_and_gets_no_nutrition(
    session: AsyncSession,
) -> None:
    """Principle 2. Uncovered mass is visible; invented nutrition is not."""
    food = await make_food(session, "cheese, monterey jack")

    with patch_usda():  # the chicken shortlist matches nothing here
        result = await enrich_food(session, food)

    assert not result.enriched
    assert food.fdc_id is None
    assert await nutrients_of(session, food) == {}
    # What was tried is still recorded, for the review screen.
    assert food.usda_payload is not None
    assert food.usda_payload["chosen_fdc_id"] is None
    assert food.fetched_at is not None


async def test_an_empty_search_result_is_handled(session: AsyncSession) -> None:
    food = await make_food(session, "something not in the database")

    with patch_usda(search={"foods": []}):
        result = await enrich_food(session, food)

    assert not result.enriched
    assert food.fdc_id is None


# ── The user's override ───────────────────────────────────────────────────


async def test_a_user_can_attach_a_specific_entry(session: AsyncSession) -> None:
    """The one-tap fix for a food the matcher would not claim."""
    food = await make_food(session, "cheese, monterey jack")

    with patch_usda():
        result = await set_food_usda(session, food, 2646170)

    assert food.fdc_id == 2646170
    assert result.nutrients_written > 0
    assert food.usda_payload is not None
    # Recorded as the user's, so it is never mistaken for an automatic match.
    assert food.usda_payload["chosen_by"] == "user"


async def test_an_unusable_entry_is_refused(session: AsyncSession) -> None:
    food = await make_food(session, "cheese, monterey jack")

    with patch_usda(detail={"nothing": "usable"}), pytest.raises(UsdaError):
        await set_food_usda(session, food, 999999)


# ── The catalogue ─────────────────────────────────────────────────────────


async def test_the_catalogue_run_reports_coverage(session: AsyncSession) -> None:
    await make_food(session, "chicken breast, boneless skinless, raw")
    await make_food(session, "cheese, monterey jack")

    with patch_usda():
        result = await enrich_catalogue(session, limit=100)

    assert result.attempted >= 2
    assert result.enriched >= 1
    assert "cheese, monterey jack" in result.unmatched


async def test_one_failure_does_not_lose_the_rest(session: AsyncSession) -> None:
    """FoodData Central is rate limited and occasionally down. Losing
    forty-nine successful lookups to one failure would be its own bug."""
    await make_food(session, "chicken breast, boneless skinless, raw")
    await make_food(session, "a food that will fail")

    calls = {"n": 0}

    async def flaky(query: str, **kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        if query == "a food that will fail":
            raise UsdaError("rate limited")
        return payload("search-chicken-breast")

    with patch.multiple(
        "ns.pipeline.enrich",
        search_foods=AsyncMock(side_effect=flaky),
        get_food=AsyncMock(return_value=payload("food-2646170-chicken-breast")),
    ):
        result = await enrich_catalogue(session, limit=100)

    assert result.enriched >= 1
    assert "a food that will fail" in result.failed


# ── The endpoints ─────────────────────────────────────────────────────────


async def test_the_food_endpoints(client: AsyncClient, session: AsyncSession) -> None:
    food = await make_food(session, "chicken breast, boneless skinless, raw")
    await make_food(session, "cheese, monterey jack")
    await session.commit()

    with patch_usda():
        enriched = await client.post("/foods/enrich?limit=100")
    assert enriched.status_code == 200
    assert enriched.json()["enriched"] >= 1

    listing = await client.get("/foods")
    assert listing.status_code == 200
    body = listing.json()
    assert body["without_nutrition"] >= 1
    named = {i["canonical_name"]: i for i in body["items"]}
    assert named["chicken breast, boneless skinless, raw"]["has_nutrition"] is True

    detail = await client.get(f"/foods/{food.id}")
    assert detail.status_code == 200
    payload_out = detail.json()
    assert payload_out["nutrient_count"] > 0
    codes = {n["nutrient_code"] for n in payload_out["nutrients"]}
    assert "protein_g" in codes


async def test_the_review_queue_lists_only_unmatched_foods(
    client: AsyncClient, session: AsyncSession
) -> None:
    await make_food(session, "a food with no match at all")
    await session.commit()

    response = await client.get("/foods?without_nutrition=true")

    assert response.status_code == 200
    assert all(i["fdc_id"] is None for i in response.json()["items"])


async def test_the_override_endpoint(client: AsyncClient, session: AsyncSession) -> None:
    food = await make_food(session, "cheese, monterey jack")
    await session.commit()

    with patch_usda():
        response = await client.post(f"/foods/{food.id}/usda", json={"fdc_id": 2646170})

    assert response.status_code == 200
    body = response.json()
    assert body["fdc_id"] == 2646170
    assert body["chosen_by"] == "user"
    assert body["has_nutrition"] is True


async def test_an_unknown_food_is_a_404(client: AsyncClient) -> None:
    assert (await client.get("/foods/98765432")).status_code == 404
    assert (await client.post("/foods/98765432/enrich")).status_code == 404


# ── When the detail endpoint fails ────────────────────────────────────────


async def test_a_detail_failure_does_not_lose_the_match(session: AsyncSession) -> None:
    """FoodData Central 404s on the detail endpoint for some ids its own
    search returns — observed for 333281 and 321360. Losing a good match
    whose nutrients search already supplied would be throwing away real data.
    """
    food = await make_food(session, "chicken breast, boneless skinless, raw")

    with patch.multiple(
        "ns.pipeline.enrich",
        search_foods=AsyncMock(return_value=payload("search-chicken-breast")),
        get_food=AsyncMock(side_effect=UsdaError("returned 404")),
    ):
        result = await enrich_food(session, food)

    assert result.enriched
    assert food.fdc_id == 2646170
    nutrients = await nutrients_of(session, food)
    assert nutrients["protein_g"].amount_per_100g == Decimal("22.5")  # the search figure


async def test_household_items_are_not_looked_up(session: AsyncSession) -> None:
    """A foil pan has no nutrition, and searching for one spends rate-limited
    quota to learn nothing."""
    from ns.models.enums import FoodCategory

    pan = await make_food(session, "disposable aluminum half pan")
    pan.category = FoodCategory.HOUSEHOLD
    await make_food(session, "chicken breast, boneless skinless, raw")
    await session.flush()

    search = AsyncMock(return_value=payload("search-chicken-breast"))
    with patch.multiple(
        "ns.pipeline.enrich",
        search_foods=search,
        get_food=AsyncMock(return_value=payload("food-2646170-chicken-breast")),
    ):
        result = await enrich_catalogue(session, limit=100)

    queried = {call.args[0] for call in search.await_args_list}
    assert "disposable aluminum half pan" not in queried
    assert "disposable aluminum half pan" not in result.unmatched


async def test_two_names_for_one_food_are_surfaced_not_crashed_into(
    session: AsyncSession,
) -> None:
    """`Food.fdc_id` is unique — one row per USDA entry — so the resolver's
    name drift (`salsa, organic` and `salsa, jarred, organic`) collides here.

    That collision is useful: it is the only thing that catches drift. What it
    must not do is raise an IntegrityError that aborts a whole catalogue run.
    """
    first = await make_food(session, "chicken breast, boneless skinless, raw")
    second = await make_food(session, "chicken breast, boneless skinless, raw ")
    second.canonical_name = "chicken breast, raw, boneless skinless"
    await session.flush()

    with patch_usda():
        await enrich_food(session, first)
        result = await enrich_food(session, second)

    assert not result.enriched
    assert second.fdc_id is None
    assert second.usda_payload is not None
    assert second.usda_payload["duplicate_of_food_id"] == first.id
    assert second.usda_payload["duplicate_of_name"] == first.canonical_name
