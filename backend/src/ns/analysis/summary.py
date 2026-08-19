"""What a basket contained, and what each nutrient cost.

Two rules govern everything here, and they are the reason this is a separate
module rather than a query in a route.

**Supply, not intake.** These numbers describe what was *bought*. Nothing here
is "consumed", "eaten", or "your intake" — a week's groceries are not a week's
meals, and copy implying otherwise is a bug, not a wording nit.

**Coverage travels with every total.** A protein figure computed from 60% of a
basket's weight is not a protein figure; it is a lower bound with a caveat
attached, and the caveat has to arrive at the same time as the number. Every
total returned here carries the share of spend and weight it was computed
from, so a caller cannot render the number without the qualifier being right
there.

Uncovered mass has three distinct causes and they are counted separately,
because they need different fixes: a line that never resolved to a food, a
food with no nutrition data yet, and a line with no weight.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import median

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.models import Food, FoodNutrient, LineItem, PriceObservation, Receipt
from ns.models.enums import ResolutionSource
from ns.pipeline.derive import OBSERVABLE_KINDS

_AMOUNT_PRECISION = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class Coverage:
    """How much of a basket a total actually accounts for.

    Weight coverage is the honest denominator for a nutrient total; spend
    coverage is the honest one for a cost. They differ, often sharply — an
    unresolved bag of rice is a lot of weight and little money.
    """

    lines_total: int = 0
    lines_resolved: int = 0
    lines_with_nutrition: int = 0

    spend_cents_total: int = 0
    spend_cents_with_nutrition: int = 0

    grams_total: Decimal = Decimal(0)
    grams_with_nutrition: Decimal = Decimal(0)

    # Why the rest is missing. Separate counts, because each needs a different
    # fix: a correction, a USDA match, or a weight.
    unresolved_lines: int = 0
    lines_without_nutrition: int = 0
    lines_without_weight: int = 0

    @property
    def spend_share(self) -> float:
        return (
            self.spend_cents_with_nutrition / self.spend_cents_total
            if self.spend_cents_total
            else 0.0
        )

    @property
    def weight_share(self) -> float:
        return float(self.grams_with_nutrition / self.grams_total) if self.grams_total else 0.0

    @property
    def is_partial(self) -> bool:
        """Whether the headline has to lead with the caveat."""
        return self.spend_share < 0.999 or self.weight_share < 0.999


@dataclass(frozen=True, slots=True)
class BasketSummary:
    """What a set of receipts contained. Never what was eaten."""

    receipt_ids: list[int]
    starts_on: date | None
    ends_on: date | None
    currency: str
    total_spend_cents: int
    coverage: Coverage
    # nutrient code to total amount, in that nutrient's own unit.
    nutrients: dict[str, Decimal] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NutrientCost:
    """One food's cost per gram of a nutrient — the flagship ranking's row."""

    food_id: int
    canonical_name: str
    observations: int
    median_price_cents_per_100g: Decimal
    nutrient_per_100g: Decimal
    nutrient_unit: str
    cost_cents_per_unit: Decimal
    # Whether the underlying weights were read off receipts or estimated. A
    # ranking built on stated weights is much stronger evidence.
    from_receipt_weights: int


async def _nutrients_by_food(
    session: AsyncSession, food_ids: set[int]
) -> dict[int, dict[str, FoodNutrient]]:
    if not food_ids:
        return {}
    rows = await session.execute(
        select(FoodNutrient).where(col(FoodNutrient.food_id).in_(food_ids))
    )
    out: dict[int, dict[str, FoodNutrient]] = {}
    for nutrient in rows.scalars().all():
        out.setdefault(nutrient.food_id, {})[nutrient.nutrient_code] = nutrient
    return out


async def summarise_receipts(session: AsyncSession, receipts: list[Receipt]) -> BasketSummary:
    """Total what these receipts' groceries contained, with coverage.

    Nutrient amounts are scaled from each food's per-100 g figures by the
    line's *edible* weight — a banana line is peel-inclusive, and 100 g of
    banana is not 100 g of peel and fruit.
    """
    receipt_ids = [r.id for r in receipts if r.id is not None]
    if not receipt_ids:
        return BasketSummary(
            receipt_ids=[],
            starts_on=None,
            ends_on=None,
            currency="USD",
            total_spend_cents=0,
            coverage=Coverage(),
        )

    rows = await session.execute(select(LineItem).where(col(LineItem.receipt_id).in_(receipt_ids)))
    lines = [line for line in rows.scalars().all() if line.kind in OBSERVABLE_KINDS]

    food_ids = {line.food_id for line in lines if line.food_id is not None}
    nutrients_by_food = await _nutrients_by_food(session, food_ids)

    totals: dict[str, Decimal] = {}
    units: dict[str, str] = {}

    lines_resolved = 0
    lines_with_nutrition = 0
    spend_total = 0
    spend_with_nutrition = 0
    grams_total = Decimal(0)
    grams_with_nutrition = Decimal(0)
    unresolved = 0
    without_nutrition = 0
    without_weight = 0

    for line in lines:
        spend_total += line.price_cents
        grams = line.grams_edible or line.grams_as_purchased
        if grams is not None:
            grams_total += grams

        if line.food_id is None or line.resolution_source is ResolutionSource.NONFOOD:
            unresolved += 1
            continue
        lines_resolved += 1

        food_nutrients = nutrients_by_food.get(line.food_id)
        if not food_nutrients:
            without_nutrition += 1
            continue
        if grams is None:
            without_weight += 1
            continue

        lines_with_nutrition += 1
        spend_with_nutrition += line.price_cents
        grams_with_nutrition += grams

        scale = grams / Decimal(100)
        for code, nutrient in food_nutrients.items():
            amount = (nutrient.amount_per_100g * scale).quantize(_AMOUNT_PRECISION)
            totals[code] = totals.get(code, Decimal(0)) + amount
            units.setdefault(code, nutrient.unit)

    dates = [r.purchased_at for r in receipts if r.purchased_at is not None]
    currencies = {r.currency for r in receipts if r.currency}

    return BasketSummary(
        receipt_ids=receipt_ids,
        starts_on=min(dates) if dates else None,
        ends_on=max(dates) if dates else None,
        # Mixed currencies would make a single total meaningless; the caller
        # is told which rather than being handed a silently summed number.
        currency=currencies.pop() if len(currencies) == 1 else "MIXED",
        total_spend_cents=spend_total,
        coverage=Coverage(
            lines_total=len(lines),
            lines_resolved=lines_resolved,
            lines_with_nutrition=lines_with_nutrition,
            spend_cents_total=spend_total,
            spend_cents_with_nutrition=spend_with_nutrition,
            grams_total=grams_total,
            grams_with_nutrition=grams_with_nutrition,
            unresolved_lines=unresolved,
            lines_without_nutrition=without_nutrition,
            lines_without_weight=without_weight,
        ),
        nutrients=totals,
        units=units,
    )


async def rank_by_nutrient_cost(
    session: AsyncSession,
    nutrient_code: str,
    *,
    store_id: int | None = None,
    limit: int = 25,
) -> list[NutrientCost]:
    """Foods ranked by what a gram of one nutrient costs. Cheapest first.

    The baseline uses the **median of non-discounted observations** per food
    (D11). A sale price is not what a food costs, and a mean would let one
    clearance buy define a food's price forever.

    Only foods that actually carry the nutrient appear. A food with no protein
    is not infinitely expensive protein; it is not a protein source, and
    putting it last with a huge number would be a category error.
    """
    query = select(PriceObservation).where(col(PriceObservation.was_discounted).is_(False))
    if store_id is not None:
        query = query.where(col(PriceObservation.store_id) == store_id)
    observations = list((await session.execute(query)).scalars().all())
    if not observations:
        return []

    by_food: dict[int, list[PriceObservation]] = {}
    for observation in observations:
        by_food.setdefault(observation.food_id, []).append(observation)

    nutrients_by_food = await _nutrients_by_food(session, set(by_food))
    foods = (
        (await session.execute(select(Food).where(col(Food.id).in_(set(by_food))))).scalars().all()
    )
    names = {food.id: food.canonical_name for food in foods if food.id is not None}

    rows: list[NutrientCost] = []
    for food_id, food_observations in by_food.items():
        nutrient = (nutrients_by_food.get(food_id) or {}).get(nutrient_code)
        if nutrient is None or nutrient.amount_per_100g <= 0:
            continue

        baseline = median(o.price_cents_per_100g for o in food_observations)
        rows.append(
            NutrientCost(
                food_id=food_id,
                canonical_name=names.get(food_id, "unknown"),
                observations=len(food_observations),
                median_price_cents_per_100g=Decimal(baseline).quantize(_AMOUNT_PRECISION),
                nutrient_per_100g=nutrient.amount_per_100g,
                nutrient_unit=nutrient.unit,
                cost_cents_per_unit=(Decimal(baseline) / nutrient.amount_per_100g).quantize(
                    _AMOUNT_PRECISION
                ),
                from_receipt_weights=sum(
                    1 for o in food_observations if o.grams_basis.value == "from_receipt"
                ),
            )
        )

    rows.sort(key=lambda r: r.cost_cents_per_unit)
    return rows[:limit]


__all__ = [
    "BasketSummary",
    "Coverage",
    "NutrientCost",
    "rank_by_nutrient_cost",
    "summarise_receipts",
]
