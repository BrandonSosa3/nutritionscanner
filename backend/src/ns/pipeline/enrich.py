"""Attaching nutrition to a resolved food.

Resolution establishes *which food* a line refers to. This attaches what that
food contains, from USDA FoodData Central, per 100 g of edible portion.

Separate from resolution on purpose. Identity is a judgement about receipt
text and costs a model call; nutrition is a lookup against a public database
and costs nothing but an HTTP request that is cached forever afterwards. A
food whose identity is settled should not have to be re-identified because its
nutrition arrived later, and a USDA outage should not block a receipt from
being processed.

Nothing here fabricates. A food with no acceptable USDA match keeps its
identity, gets no nutrients, and contributes visible uncovered mass to every
summary that includes it. A basket that says "82% of this basket's weight has
nutrition data" is telling the truth; one that quietly treats the other 18% as
zero is not.
"""

from dataclasses import dataclass, field

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.logging import get_logger
from ns.models import Food, FoodNutrient
from ns.models.base import utcnow
from ns.models.enums import FoodCategory
from ns.providers.usda.client import UsdaError, get_food, search_foods
from ns.providers.usda.matching import Candidate, best_match, rank_candidates
from ns.providers.usda.parsing import ParsedFood, parse_food

log = get_logger(__name__)

# How many candidates to consider. Enough that the right answer is in the
# shortlist when it is not the top hit, few enough to keep the stored
# provenance readable.
SEARCH_PAGE_SIZE = 10


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    food: Food
    matched: Candidate | None
    nutrients_written: int
    considered: list[Candidate] = field(default_factory=list)

    @property
    def enriched(self) -> bool:
        return self.matched is not None


def _provenance(
    canonical_name: str, considered: list[Candidate], chosen: Candidate | None
) -> dict[str, object]:
    """What was considered and why it won or lost.

    Stored so a match that looks wrong months later is explainable rather than
    mysterious, and so a change to the scoring can be evaluated against
    decisions it has already made.
    """
    return {
        "queried": canonical_name,
        "chosen_fdc_id": chosen.food.fdc_id if chosen else None,
        "min_score": None if chosen is None else chosen.score,
        "considered": [
            {
                "fdc_id": c.food.fdc_id,
                "description": c.food.description,
                "data_type": c.food.data_type,
                "score": c.score,
                "recall": c.recall,
                "precision": c.precision,
                "rejected_reason": c.rejected_reason,
            }
            for c in considered
        ],
    }


async def _write_nutrients(session: AsyncSession, food: Food, parsed: ParsedFood) -> int:
    """Replace this food's nutrient rows with the ones USDA publishes.

    Replaced rather than merged: a stale nutrient from an earlier match would
    otherwise survive a re-match and quietly contribute to totals.
    """
    await session.execute(delete(FoodNutrient).where(col(FoodNutrient.food_id) == food.id))
    session.add_all(
        FoodNutrient(
            food_id=food.id,
            nutrient_code=nutrient.code,
            amount_per_100g=nutrient.amount_per_100g,
            unit=nutrient.unit,
        )
        for nutrient in parsed.nutrients
    )
    await session.flush()
    return len(parsed.nutrients)


async def enrich_food(
    session: AsyncSession, food: Food, *, force: bool = False
) -> EnrichmentResult:
    """Find this food in FoodData Central and store what it contains.

    Idempotent: a food that already has an `fdc_id` is left alone unless
    `force` is set, so re-running over a whole catalogue only fetches what is
    missing.
    """
    if food.fdc_id is not None and not force:
        existing = (
            (
                await session.execute(
                    select(FoodNutrient).where(col(FoodNutrient.food_id) == food.id)
                )
            )
            .scalars()
            .all()
        )
        return EnrichmentResult(food=food, matched=None, nutrients_written=len(existing))

    payload = await search_foods(food.canonical_name, page_size=SEARCH_PAGE_SIZE)
    hits = payload.get("foods") or []
    parsed_hits = [p for hit in hits if (p := parse_food(hit)) is not None]

    considered = rank_candidates(food.canonical_name, parsed_hits)
    chosen = best_match(food.canonical_name, parsed_hits)

    if chosen is None:
        # No acceptable match. The food keeps its identity and gets no
        # nutrition — recorded, so the correction UI can show what was tried.
        food.usda_payload = _provenance(food.canonical_name, considered, None)
        food.fetched_at = utcnow()
        await session.flush()
        log.info(
            "enrich.no_match",
            food_id=food.id,
            canonical_name=food.canonical_name,
            considered=len(considered),
        )
        return EnrichmentResult(food=food, matched=None, nutrients_written=0, considered=considered)

    # The detail record is fetched for the chosen candidate only. It is the
    # authoritative source and carries fields search omits.
    #
    # Its failure is not the match's failure. FoodData Central returns 404 on
    # the detail endpoint for some ids its own search returns — observed for
    # 333281 and 321360 on 2026-08-18 — and an exception here was throwing
    # away a good match whose nutrients search had already supplied. The
    # search hit stands in whenever detail cannot.
    detail: ParsedFood | None = None
    try:
        detail = parse_food(await get_food(chosen.food.fdc_id))
    except UsdaError as exc:
        log.warning(
            "enrich.detail_unavailable",
            fdc_id=chosen.food.fdc_id,
            error=str(exc),
        )
    full = detail if detail is not None and detail.nutrients else chosen.food

    # `Food.fdc_id` is unique — one row per USDA entry — so a second food
    # matching the same entry is evidence that two canonical names describe
    # one food. The resolver produced exactly that: `salsa, organic` and
    # `salsa, jarred, organic`.
    #
    # Left unmatched with the duplicate named, rather than allowed to raise an
    # IntegrityError that would abort the whole catalogue run. The uniqueness
    # is doing its job here: it is the only thing that catches name drift.
    claimed = (
        await session.execute(select(Food).where(col(Food.fdc_id) == full.fdc_id))
    ).scalar_one_or_none()
    if claimed is not None and claimed.id != food.id:
        provenance = _provenance(food.canonical_name, considered, chosen)
        provenance["duplicate_of_food_id"] = claimed.id
        provenance["duplicate_of_name"] = claimed.canonical_name
        food.usda_payload = provenance
        food.fetched_at = utcnow()
        await session.flush()
        log.info(
            "enrich.duplicate_food",
            food_id=food.id,
            canonical_name=food.canonical_name,
            duplicate_of=claimed.canonical_name,
            fdc_id=full.fdc_id,
        )
        return EnrichmentResult(food=food, matched=None, nutrients_written=0, considered=considered)

    food.fdc_id = full.fdc_id
    food.fdc_data_type = full.data_type
    food.usda_payload = _provenance(food.canonical_name, considered, chosen)
    food.fetched_at = utcnow()
    written = await _write_nutrients(session, food, full)

    log.info(
        "enrich.matched",
        food_id=food.id,
        canonical_name=food.canonical_name,
        fdc_id=full.fdc_id,
        description=full.description,
        score=chosen.score,
        nutrients=written,
    )
    return EnrichmentResult(
        food=food, matched=chosen, nutrients_written=written, considered=considered
    )


@dataclass(frozen=True, slots=True)
class CatalogueResult:
    attempted: int
    enriched: int
    unmatched: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.enriched / self.attempted if self.attempted else 0.0


async def enrich_catalogue(
    session: AsyncSession, *, limit: int = 50, force: bool = False
) -> CatalogueResult:
    """Enrich foods that have no USDA match yet.

    A single food failing does not stop the run: FoodData Central is rate
    limited and occasionally unavailable, and losing forty-nine successful
    lookups to one failure would be its own bug. Failures are named in the
    result and the food is left exactly as it was, ready for the next run.
    """
    # Household items are skipped. A foil pan has no nutrition to look up, and
    # searching for one spends rate-limited quota to learn nothing.
    query = select(Food).where(col(Food.category) != FoodCategory.HOUSEHOLD).order_by(col(Food.id))
    if not force:
        query = query.where(col(Food.fdc_id).is_(None))

    foods = list((await session.execute(query.limit(limit))).scalars().all())

    enriched = 0
    unmatched: list[str] = []
    failed: list[str] = []

    for food in foods:
        try:
            result = await enrich_food(session, food, force=force)
        except UsdaError as exc:
            log.warning("enrich.failed", food_id=food.id, error=str(exc))
            failed.append(food.canonical_name)
            continue
        if result.enriched:
            enriched += 1
        else:
            unmatched.append(food.canonical_name)

    log.info(
        "enrich.catalogue_completed",
        attempted=len(foods),
        enriched=enriched,
        unmatched=len(unmatched),
        failed=len(failed),
    )
    return CatalogueResult(
        attempted=len(foods), enriched=enriched, unmatched=unmatched, failed=failed
    )


async def set_food_usda(session: AsyncSession, food: Food, fdc_id: int) -> EnrichmentResult:
    """Attach a specific FoodData Central entry, chosen by the user.

    The automatic matcher requires every term of the canonical name to appear
    in the candidate, which is strict enough that real foods go unmatched —
    `cheese, monterey jack` against USDA's `Cheese, monterey`. This is how that
    gets resolved: the user picks from the candidates already recorded, and the
    choice is stored with its own provenance so it is never mistaken for an
    automatic match.
    """
    parsed = parse_food(await get_food(fdc_id))
    if parsed is None:
        raise UsdaError(f"FoodData Central returned nothing usable for fdcId {fdc_id}.")

    food.fdc_id = parsed.fdc_id
    food.fdc_data_type = parsed.data_type
    food.usda_payload = {
        "queried": food.canonical_name,
        "chosen_fdc_id": parsed.fdc_id,
        "chosen_by": "user",
        "chosen_description": parsed.description,
    }
    food.fetched_at = utcnow()
    written = await _write_nutrients(session, food, parsed)

    log.info(
        "enrich.set_by_user",
        food_id=food.id,
        fdc_id=parsed.fdc_id,
        description=parsed.description,
        nutrients=written,
    )
    return EnrichmentResult(food=food, matched=None, nutrients_written=written)
