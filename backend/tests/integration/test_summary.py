"""Derivation, basket totals, and the cost-per-nutrient ranking.

Built on controlled data rather than a real receipt, because the point is the
arithmetic: a nutrient total is a chain of scalings from per-100 g figures
through edible weight, and every link needs to be exact.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.analysis.summary import rank_by_nutrient_cost, summarise_receipts
from ns.models import Food, FoodNutrient, LineItem, PriceObservation, Receipt, Store
from ns.models.enums import GramsBasis, LineItemKind, ResolutionSource
from ns.pipeline.derive import derive_receipt
from ns.pipeline.ingest import ingest_receipt
from ns.providers.storage import LocalReceiptStorage
from tests.unit.test_images import make_image

pytestmark = pytest.mark.integration


@pytest.fixture
def storage(tmp_path: Path) -> LocalReceiptStorage:
    return LocalReceiptStorage(root=tmp_path / "receipts")


async def a_food(
    session: AsyncSession,
    name: str,
    *,
    protein_per_100g: str | None = None,
    energy_per_100g: str | None = None,
) -> Food:
    existing = (
        await session.execute(select(Food).where(col(Food.canonical_name) == name))
    ).scalar_one_or_none()
    food = existing or Food(canonical_name=name)
    if existing is None:
        session.add(food)
        await session.flush()

    if protein_per_100g is not None:
        session.add(
            FoodNutrient(
                food_id=food.id,
                nutrient_code="protein_g",
                amount_per_100g=Decimal(protein_per_100g),
                unit="g",
            )
        )
    if energy_per_100g is not None:
        session.add(
            FoodNutrient(
                food_id=food.id,
                nutrient_code="energy_kcal",
                amount_per_100g=Decimal(energy_per_100g),
                unit="kcal",
            )
        )
    await session.flush()
    return food


async def a_receipt(
    session: AsyncSession,
    storage: LocalReceiptStorage,
    *,
    color: str = "white",
    purchased_at: date = date(2026, 8, 15),
    store: Store | None = None,
) -> Receipt:
    receipt = (await ingest_receipt(session, make_image(color=color), storage=storage)).receipt
    receipt.purchased_at = purchased_at
    receipt.currency = "USD"
    if store is not None:
        receipt.store_id = store.id
    await session.flush()
    return receipt


async def a_line(
    session: AsyncSession,
    receipt: Receipt,
    *,
    index: int,
    price_cents: int,
    food: Food | None = None,
    grams: str | None = None,
    basis: GramsBasis = GramsBasis.FROM_RECEIPT,
    kind: LineItemKind = LineItemKind.PRODUCT,
    source: ResolutionSource = ResolutionSource.LLM,
    discount_cents: int = 0,
) -> LineItem:
    line = LineItem(
        receipt_id=receipt.id,
        line_index=index,
        raw_text=f"ITEM {index}",
        normalized_text=f"item {index}",
        normalizer_version="v1",
        kind=kind,
        price_cents=price_cents,
        food_id=food.id if food else None,
        resolution_source=source if food else ResolutionSource.UNRESOLVED,
        grams_as_purchased=Decimal(grams) if grams else None,
        grams_edible=Decimal(grams) if grams else None,
        grams_basis=basis,
        discount_cents=discount_cents,
    )
    session.add(line)
    await session.flush()
    return line


# ── Derivation ────────────────────────────────────────────────────────────


async def test_a_resolved_weighed_line_becomes_an_observation(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await a_receipt(session, storage)
    cheese = await a_food(session, "cheese, monterey jack, for derive")
    await a_line(session, receipt, index=1, price_cents=445, food=cheese, grams="907.185")

    result = await derive_receipt(session, receipt)

    assert result.observed == 1
    observation = result.observations[0]
    assert observation.price_cents_per_100g == Decimal("49.0528")
    assert observation.observed_at == date(2026, 8, 15)
    assert observation.grams_basis is GramsBasis.FROM_RECEIPT


async def test_a_line_with_no_weight_is_skipped_not_guessed(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Price per 100 g is undefined without a weight."""
    receipt = await a_receipt(session, storage)
    food = await a_food(session, "a food with no weight")
    await a_line(session, receipt, index=1, price_cents=500, food=food, grams=None)

    result = await derive_receipt(session, receipt)

    assert result.observed == 0
    assert result.skipped_no_grams == 1


async def test_an_unresolved_line_is_skipped(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await a_receipt(session, storage)
    await a_line(session, receipt, index=1, price_cents=500, grams="200")

    result = await derive_receipt(session, receipt)

    assert result.observed == 0
    assert result.skipped_unresolved == 1


async def test_nonfood_produces_no_observation(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """A carrier bag has a price and no nutritional meaning."""
    receipt = await a_receipt(session, storage)
    bag = await a_food(session, "carrier bag, for derive")
    await a_line(
        session,
        receipt,
        index=1,
        price_cents=75,
        food=bag,
        grams="50",
        source=ResolutionSource.NONFOOD,
    )

    result = await derive_receipt(session, receipt)

    assert result.observed == 0


async def test_deriving_twice_rebuilds_rather_than_duplicates(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """A correction changes both price and weight, so a stale observation
    would keep feeding the ranking with the old numbers."""
    receipt = await a_receipt(session, storage)
    food = await a_food(session, "a food that gets corrected")
    line = await a_line(session, receipt, index=1, price_cents=400, food=food, grams="200")

    await derive_receipt(session, receipt)
    line.grams_edible = Decimal("400")
    await session.flush()
    result = await derive_receipt(session, receipt)

    assert result.observed == 1
    stored = (
        (
            await session.execute(
                select(PriceObservation).where(col(PriceObservation.line_item_id) == line.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(stored) == 1
    assert stored[0].price_cents_per_100g == Decimal("100.0000")


async def test_a_receipt_with_no_date_cannot_be_derived(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Prices have to be placeable in time; that is the whole point of the row."""
    receipt = await a_receipt(session, storage)
    receipt.purchased_at = None
    await session.flush()

    with pytest.raises(ValueError, match="no purchase date"):
        await derive_receipt(session, receipt)


# ── Basket totals ─────────────────────────────────────────────────────────


async def test_nutrients_scale_by_edible_weight(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """31 g of protein per 100 g, over 200 g, is 62 g."""
    receipt = await a_receipt(session, storage)
    chicken = await a_food(session, "chicken, for summary", protein_per_100g="31")
    await a_line(session, receipt, index=1, price_cents=1000, food=chicken, grams="200")

    summary = await summarise_receipts(session, [receipt])

    assert summary.nutrients["protein_g"] == Decimal("62.0000")
    assert summary.units["protein_g"] == "g"
    assert summary.coverage.weight_share == 1.0
    assert not summary.coverage.is_partial


async def test_two_lines_of_the_same_nutrient_add_up(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await a_receipt(session, storage)
    chicken = await a_food(session, "chicken, adding", protein_per_100g="31")
    beans = await a_food(session, "beans, adding", protein_per_100g="9")
    await a_line(session, receipt, index=1, price_cents=1000, food=chicken, grams="200")
    await a_line(session, receipt, index=2, price_cents=300, food=beans, grams="400")

    summary = await summarise_receipts(session, [receipt])

    assert summary.nutrients["protein_g"] == Decimal("98.0000")  # 62 + 36


async def test_coverage_separates_the_three_reasons_a_line_contributes_nothing(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Each needs a different fix: a correction, a USDA match, or a weight."""
    receipt = await a_receipt(session, storage)
    known = await a_food(session, "a known food", protein_per_100g="10")
    no_nutrition = await a_food(session, "a food awaiting usda")

    await a_line(session, receipt, index=1, price_cents=100, food=known, grams="100")
    await a_line(session, receipt, index=2, price_cents=200, food=no_nutrition, grams="100")
    await a_line(session, receipt, index=3, price_cents=300, food=known, grams=None)
    await a_line(session, receipt, index=4, price_cents=400, grams="100")

    coverage = (await summarise_receipts(session, [receipt])).coverage

    assert coverage.lines_total == 4
    assert coverage.lines_with_nutrition == 1
    assert coverage.unresolved_lines == 1
    assert coverage.lines_without_nutrition == 1
    assert coverage.lines_without_weight == 1
    assert coverage.is_partial


async def test_spend_and_weight_coverage_differ(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """An unresolved bag of rice is a lot of weight and little money. A single
    coverage number would hide that."""
    receipt = await a_receipt(session, storage)
    known = await a_food(session, "an expensive light food", protein_per_100g="10")
    await a_line(session, receipt, index=1, price_cents=900, food=known, grams="100")
    await a_line(session, receipt, index=2, price_cents=100, grams="900")

    coverage = (await summarise_receipts(session, [receipt])).coverage

    assert coverage.spend_share == 0.9
    assert coverage.weight_share == 0.1


async def test_summarising_nothing_is_empty_not_an_error(session: AsyncSession) -> None:
    summary = await summarise_receipts(session, [])
    assert summary.total_spend_cents == 0
    assert summary.nutrients == {}


async def test_mixed_currencies_are_not_silently_summed(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    first = await a_receipt(session, storage, color="white")
    second = await a_receipt(session, storage, color="black")
    second.currency = "ZAR"
    await session.flush()

    summary = await summarise_receipts(session, [first, second])

    assert summary.currency == "MIXED"


# ── The flagship ranking ──────────────────────────────────────────────────


async def test_foods_rank_by_cost_per_gram_of_protein(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """The headline view: cheapest protein first."""
    receipt = await a_receipt(session, storage)
    chicken = await a_food(session, "chicken, ranked", protein_per_100g="31")
    beans = await a_food(session, "beans, ranked", protein_per_100g="9")
    # $1.00/100g of chicken -> 3.23c per g of protein.
    await a_line(session, receipt, index=1, price_cents=100, food=chicken, grams="100")
    # $0.50/100g of beans -> 5.56c per g of protein.
    await a_line(session, receipt, index=2, price_cents=50, food=beans, grams="100")
    await derive_receipt(session, receipt)

    ranking = await rank_by_nutrient_cost(session, "protein_g")

    assert [r.canonical_name for r in ranking] == ["chicken, ranked", "beans, ranked"]
    assert ranking[0].cost_cents_per_unit == Decimal("3.2258")
    assert ranking[1].cost_cents_per_unit == Decimal("5.5556")


async def test_a_food_without_the_nutrient_is_absent_not_last(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """A food with no protein is not infinitely expensive protein; it is not a
    protein source, and ranking it last with a huge number is a category error."""
    receipt = await a_receipt(session, storage)
    oil = await a_food(session, "oil, no protein", energy_per_100g="884")
    await a_line(session, receipt, index=1, price_cents=500, food=oil, grams="100")
    await derive_receipt(session, receipt)

    ranking = await rank_by_nutrient_cost(session, "protein_g")

    assert all(r.canonical_name != "oil, no protein" for r in ranking)
    assert any(
        r.canonical_name == "oil, no protein"
        for r in await rank_by_nutrient_cost(session, "energy_kcal")
    )


async def test_the_baseline_is_the_median_not_the_mean(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """One outlier buy must not define a food's price forever."""
    chicken = await a_food(session, "chicken, median", protein_per_100g="31")
    # Visibly different colours, not near-identical ones: JPEG is lossy, and
    # two nearly-black images compress to the same bytes — which ingestion
    # correctly treats as one receipt.
    for price, day, colour in [(100, 1, "white"), (110, 2, "black"), (900, 3, "red")]:
        receipt = await a_receipt(session, storage, color=colour, purchased_at=date(2026, 8, day))
        await a_line(session, receipt, index=1, price_cents=price, food=chicken, grams="100")
        await derive_receipt(session, receipt)

    ranking = await rank_by_nutrient_cost(session, "protein_g")

    assert ranking[0].observations == 3
    assert ranking[0].median_price_cents_per_100g == Decimal("110.0000")


async def test_sale_prices_are_excluded_from_the_baseline(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """A sale is not what a food costs (D11)."""
    chicken = await a_food(session, "chicken, discounted", protein_per_100g="31")
    full = await a_receipt(session, storage, color="white")
    await a_line(session, full, index=1, price_cents=1000, food=chicken, grams="100")
    await derive_receipt(session, full)

    sale = await a_receipt(session, storage, color="black")
    await a_line(
        session, sale, index=1, price_cents=200, food=chicken, grams="100", discount_cents=800
    )
    await derive_receipt(session, sale)

    ranking = await rank_by_nutrient_cost(session, "protein_g")

    assert ranking[0].observations == 1
    assert ranking[0].median_price_cents_per_100g == Decimal("1000.0000")


async def test_the_ranking_says_how_many_weights_came_from_receipts(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """A ranking built on stated weights is much stronger evidence than one
    built on estimates, and the UI has to be able to say so."""
    receipt = await a_receipt(session, storage)
    chicken = await a_food(session, "chicken, weighed", protein_per_100g="31")
    await a_line(
        session,
        receipt,
        index=1,
        price_cents=100,
        food=chicken,
        grams="100",
        basis=GramsBasis.PER_PACKAGE,
    )
    await derive_receipt(session, receipt)

    ranking = await rank_by_nutrient_cost(session, "protein_g")

    assert ranking[0].observations == 1
    assert ranking[0].from_receipt_weights == 0


# ── The endpoints ─────────────────────────────────────────────────────────


async def test_the_summary_endpoint_leads_with_coverage(
    client: AsyncClient, session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await a_receipt(session, storage)
    known = await a_food(session, "an endpoint food", protein_per_100g="20")
    await a_line(session, receipt, index=1, price_cents=100, food=known, grams="100")
    await a_line(session, receipt, index=2, price_cents=900, grams="100")
    await session.commit()

    response = await client.get(f"/summary?receipt_id={receipt.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["is_partial"] is True
    assert "lower bound" in body["headline"]
    assert "10%" in body["headline"]  # spend share
    assert body["nutrients"]["protein_g"] == "20.0000"


async def test_a_full_basket_says_so(
    client: AsyncClient, session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await a_receipt(session, storage)
    known = await a_food(session, "a fully covered food", protein_per_100g="20")
    await a_line(session, receipt, index=1, price_cents=100, food=known, grams="100")
    await session.commit()

    body = (await client.get(f"/summary?receipt_id={receipt.id}")).json()

    assert body["coverage"]["is_partial"] is False
    assert "Every line" in body["headline"]


async def test_the_ranking_endpoint(
    client: AsyncClient, session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await a_receipt(session, storage)
    chicken = await a_food(session, "chicken, endpoint", protein_per_100g="31")
    await a_line(session, receipt, index=1, price_cents=100, food=chicken, grams="100")
    await derive_receipt(session, receipt)
    await session.commit()

    response = await client.get("/summary/cost-per-nutrient?nutrient=protein_g")

    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "Protein"
    assert body["unit"] == "g"
    assert body["items"][0]["cost_cents_per_unit"] == "3.2258"


async def test_an_unknown_nutrient_is_refused_with_the_known_list(
    client: AsyncClient,
) -> None:
    response = await client.get("/summary/cost-per-nutrient?nutrient=vibes")
    assert response.status_code == 422
    assert "protein_g" in response.json()["detail"]
