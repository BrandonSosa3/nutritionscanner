"""Stage 5 — resolve.

Turns a normalised line into a food identity. Three tiers, in order of trust:

1. **Corrections.** A store-specific correction wins over a global one. These
   are free, instant, and permanent — a fix made once is applied to every
   future receipt (principle 3). This is the core product loop.
2. **The model.** Everything a correction does not cover goes in a single
   batched call. Batching matters: one call for a basket rather than one per
   line is roughly an order of magnitude cheaper and is the only place in the
   pipeline that costs money per receipt.
3. **Unresolved.** A real, visible, honest state — not a failure. It is what
   the correction queue reads, and it is how tier 1 gets its data.

This is the one component that can be confidently wrong, so it is measured
rather than trusted: see `ns.eval`, which scores it against a held-out labeled
set and records the number on every prompt revision.

Nothing here invents nutrition. It establishes *which food* a line refers to;
the nutrient values come from USDA against that identity, and a food with no
nutrients yet contributes visible uncovered mass rather than a silent zero.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.config import get_settings
from ns.logging import get_logger
from ns.models import Correction, Food, LineItem, Receipt
from ns.models.base import utcnow
from ns.models.enums import (
    FoodCategory,
    GramsBasis,
    LineItemKind,
    LlmStage,
    PipelineStatus,
    ResolutionSource,
)
from ns.providers.anthropic.client import CallResult, call_structured, load_prompt
from ns.providers.anthropic.schemas import ResolutionBatch, ResolvedLine

log = get_logger(__name__)

PROMPT_NAME = "resolve_v1"

# Lines that name something bought. Tax, subtotal, total, and discount lines
# are basket arithmetic, not food, and are left alone.
RESOLVABLE_KINDS = frozenset({LineItemKind.PRODUCT, LineItemKind.UNKNOWN})

_GRAMS_PRECISION = Decimal("0.001")
_PCT_PRECISION = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    receipt: Receipt
    by_source: dict[str, int] = field(default_factory=dict)
    call: CallResult[ResolutionBatch] | None = None
    unresolved_texts: list[str] = field(default_factory=list)

    @property
    def resolved_count(self) -> int:
        return sum(
            count
            for source, count in self.by_source.items()
            if source != ResolutionSource.UNRESOLVED.value
        )

    @property
    def total_count(self) -> int:
        return sum(self.by_source.values())

    @property
    def coverage(self) -> float:
        """The number every summary has to state (principle 6)."""
        return self.resolved_count / self.total_count if self.total_count else 0.0


# ── Grams ─────────────────────────────────────────────────────────────────


def _line_count(line: LineItem) -> Decimal:
    """How many of the thing this line covers.

    A quantity carrying a unit is a measurement, not a count: `0.778 kg` is one
    item that happens to weigh that much.
    """
    quantity = line.quantity
    if line.unit is None and quantity is not None and quantity > 0:
        return quantity
    return Decimal(1)


def apply_grams_rule(
    line: LineItem,
    basis: GramsBasis,
    value: Decimal | None,
    *,
    food: Food | None = None,
    override: bool = False,
) -> None:
    """Apply a gram *rule* to a line — never a stored gram figure (D3).

    Food identity is stable across purchases; weight usually is not. Replaying
    "1.2 lb of broccoli = 544 g" onto every future broccoli line corrupts every
    later basket, which is why a correction stores a basis and a value and the
    arithmetic happens here, against this line's own quantity.

    Authority runs: user correction, then the receipt, then an estimate. A
    figure the receipt printed is never replaced by a model's guess, which is
    what `override` distinguishes — a correction sets it, the resolver does
    not.
    """
    if (
        not override
        and line.grams_basis is GramsBasis.FROM_RECEIPT
        and line.grams_as_purchased is not None
    ):
        _apply_edible_portion(line, food)
        return

    if basis in (GramsBasis.PER_PACKAGE, GramsBasis.PER_UNIT_ESTIMATE) and value is not None:
        line.grams_as_purchased = (value * _line_count(line)).quantize(_GRAMS_PRECISION)
        line.grams_basis = basis
    elif basis is GramsBasis.DENSITY and food is not None and food.density_g_per_ml:
        # Volume is only convertible once the food's own density is known.
        # Without one the line stays without grams rather than assuming water.
        if value is not None:
            line.grams_as_purchased = (value * food.density_g_per_ml * _line_count(line)).quantize(
                _GRAMS_PRECISION
            )
            line.grams_basis = GramsBasis.DENSITY

    _apply_edible_portion(line, food)


def _apply_edible_portion(line: LineItem, food: Food | None) -> None:
    """Purchased weight to edible weight, snapshotting the factor applied.

    A banana line is peel-inclusive. Bone-in chicken, melon, and avocado all
    differ sharply between purchased and edible weight. The factor is stored on
    the line so a later edit to the Food cannot silently rewrite history — that
    takes an explicit re-run.
    """
    if line.grams_as_purchased is None or food is None:
        return
    pct = food.edible_portion_pct
    line.edible_portion_pct_applied = pct
    line.grams_edible = (line.grams_as_purchased * pct / Decimal(100)).quantize(_GRAMS_PRECISION)


# ── Tier 1: corrections ───────────────────────────────────────────────────


async def _load_corrections(
    session: AsyncSession, texts: set[str], store_id: int | None
) -> dict[str, Correction]:
    """The best correction for each text: store-specific beats global (D2)."""
    if not texts:
        return {}

    rows = await session.execute(
        select(Correction).where(
            col(Correction.normalized_text).in_(texts),
            or_(col(Correction.store_id) == store_id, col(Correction.store_id).is_(None)),
        )
    )

    best: dict[str, Correction] = {}
    for correction in rows.scalars().all():
        existing = best.get(correction.normalized_text)
        if existing is None or (existing.store_id is None and correction.store_id is not None):
            best[correction.normalized_text] = correction
    return best


async def _apply_correction(
    session: AsyncSession, line: LineItem, correction: Correction
) -> ResolutionSource:
    food = await session.get(Food, correction.food_id) if correction.food_id else None

    line.food_id = correction.food_id
    line.confidence = 1.0
    line.resolved_at = utcnow()
    line.resolution_source = (
        ResolutionSource.NONFOOD
        if correction.is_nonfood
        else (
            ResolutionSource.CORRECTION_STORE
            if correction.store_id is not None
            else ResolutionSource.CORRECTION_GLOBAL
        )
    )
    apply_grams_rule(line, correction.grams_basis, correction.grams_value, food=food, override=True)

    correction.applied_count += 1
    correction.last_applied_at = utcnow()
    return line.resolution_source


# ── Tier 2: the model ─────────────────────────────────────────────────────


def _describe(line: LineItem) -> str:
    """One line of context for the batch call.

    `text` is the normalised form — the same key a correction is stored
    against, so anything the model gets wrong is fixable by a correction that
    then applies everywhere. `printed` carries the signal normalisation strips
    (package sizes, PLU digits) without letting identity depend on it.
    """
    parts = [f"{line.line_index} | text: {line.normalized_text}"]
    if line.raw_text and line.raw_text.lower() != line.normalized_text:
        parts.append(f"printed: {line.raw_text}")
    if line.quantity is not None:
        parts.append(f"quantity: {line.quantity}{' ' + line.unit if line.unit else ''}")
    if line.grams_as_purchased is not None:
        parts.append(f"weighed: {line.grams_as_purchased} g")
    parts.append(f"price: {line.price_cents / 100:.2f}")
    return " | ".join(parts)


def _parse_grams(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        grams = Decimal(value.strip())
    except (InvalidOperation, ArithmeticError, AttributeError):
        log.warning("resolve.unparseable_grams", value=value)
        return None
    return grams if grams > 0 else None


async def _get_or_create_food(session: AsyncSession, name: str, category: str) -> Food:
    """One Food row per canonical name.

    Uniqueness is enforced in the database. Two rows for one food would split
    its price history in half and make cost per gram of protein quietly wrong
    for both.
    """
    canonical = " ".join(name.strip().lower().split())
    existing = (
        await session.execute(select(Food).where(col(Food.canonical_name) == canonical))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    food = Food(
        canonical_name=canonical,
        category=FoodCategory(category)
        if category in set(FoodCategory)
        else FoodCategory.UNCATEGORIZED,
    )
    session.add(food)
    await session.flush()
    return food


async def _apply_resolution(
    session: AsyncSession, line: LineItem, resolved: ResolvedLine, *, min_confidence: float
) -> ResolutionSource:
    """Record what the model said, or leave the line unresolved.

    A name with no confidence behind it is not an answer. Below the floor the
    line stays unresolved and goes to the correction queue, which is where a
    real label comes from.
    """
    if resolved.canonical_name is None or resolved.confidence < min_confidence:
        line.resolution_source = ResolutionSource.UNRESOLVED
        line.confidence = resolved.confidence
        return ResolutionSource.UNRESOLVED

    food = await _get_or_create_food(session, resolved.canonical_name, resolved.category)

    line.food_id = food.id
    line.confidence = resolved.confidence
    line.resolved_at = utcnow()
    line.resolution_source = (
        ResolutionSource.NONFOOD if resolved.is_nonfood else ResolutionSource.LLM
    )
    apply_grams_rule(
        line,
        GramsBasis(resolved.grams_basis),
        _parse_grams(resolved.grams_estimate),
        food=food,
    )
    return line.resolution_source


async def resolve_lines(
    session: AsyncSession,
    lines: list[LineItem],
    *,
    store_name: str | None = None,
    receipt_id: int | None = None,
    min_confidence: float | None = None,
    stage: LlmStage = LlmStage.RESOLVE,
) -> tuple[dict[int, ResolvedLine], CallResult[ResolutionBatch]]:
    """One batched model call for a list of lines. Returns results by line index.

    Separated from `resolve_receipt` so the eval harness can drive exactly this
    path against labeled examples without touching a receipt or the corrections
    table.

    `stage` is what the recorded LlmCall is filed under. The harness passes
    `EVAL`, so the cost of *measuring* the resolver stays separable from the
    cost of *running* it — without that, cost per receipt silently includes
    eval runs and means nothing.
    """
    _ = min_confidence  # applied by the caller; kept out of the call itself
    prompt = load_prompt(PROMPT_NAME)

    header = f"Store: {store_name}\n" if store_name else ""
    listing = "\n".join(_describe(line) for line in lines)
    content = [
        {
            "type": "text",
            "text": (
                f"{header}Identify the food on each line.\n\n{listing}\n\n"
                "Return one entry per line, with the same line_index. "
                "Where you cannot tell, return a null name rather than a guess."
            ),
        }
    ]

    call = await call_structured(
        session,
        stage=stage,
        prompt=prompt,
        content=content,
        output_model=ResolutionBatch,
        receipt_id=receipt_id,
    )
    return {item.line_index: item for item in call.parsed.items}, call


# ── The stage ─────────────────────────────────────────────────────────────


async def resolve_receipt(
    session: AsyncSession, receipt: Receipt, *, force: bool = False
) -> ResolutionResult:
    """Resolve a receipt's line items to foods.

    Idempotent by default: lines already resolved are left alone, so re-running
    after adding a correction fixes the unresolved ones without paying to
    re-answer the rest. `force` re-resolves everything, which is how a prompt
    revision gets applied to a receipt already on file.
    """
    settings = get_settings()
    min_confidence = settings.resolution_min_confidence

    rows = await session.execute(
        select(LineItem)
        .where(col(LineItem.receipt_id) == receipt.id)
        .order_by(col(LineItem.line_index))
    )
    line_items = list(rows.scalars().all())

    counts: dict[str, int] = {}

    def record(source: ResolutionSource) -> None:
        counts[source.value] = counts.get(source.value, 0) + 1

    pending: list[LineItem] = []
    for line in line_items:
        if line.kind is LineItemKind.FEE:
            # A bag fee or a bottle deposit is definitionally not food. Sending
            # it to the model would be paying to be told so.
            line.resolution_source = ResolutionSource.NONFOOD
            line.resolved_at = utcnow()
            line.confidence = 1.0
            record(ResolutionSource.NONFOOD)
            continue
        if line.kind not in RESOLVABLE_KINDS:
            continue
        if line.food_id is not None and not force:
            record(line.resolution_source)
            continue
        pending.append(line)

    # Tier 1.
    corrections = await _load_corrections(
        session, {line.normalized_text for line in pending}, receipt.store_id
    )
    remaining: list[LineItem] = []
    for line in pending:
        correction = corrections.get(line.normalized_text)
        if correction is None:
            remaining.append(line)
            continue
        record(await _apply_correction(session, line, correction))

    # Tier 2.
    call: CallResult[ResolutionBatch] | None = None
    if remaining:
        store_name = None
        if receipt.store_id is not None:
            from ns.models import Store

            store = await session.get(Store, receipt.store_id)
            store_name = store.name if store else None

        resolved, call = await resolve_lines(
            session, remaining, store_name=store_name, receipt_id=receipt.id
        )

        for line in remaining:
            answer = resolved.get(line.line_index)
            if answer is None:
                # The model dropped a line. Silence is not an answer; the line
                # stays unresolved rather than being quietly skipped.
                log.warning(
                    "resolve.line_missing_from_batch",
                    receipt_id=receipt.id,
                    line_index=line.line_index,
                )
                line.resolution_source = ResolutionSource.UNRESOLVED
                record(ResolutionSource.UNRESOLVED)
                continue
            record(await _apply_resolution(session, line, answer, min_confidence=min_confidence))

    unresolved_texts = [
        line.normalized_text
        for line in line_items
        if line.kind in RESOLVABLE_KINDS and line.resolution_source is ResolutionSource.UNRESOLVED
    ]

    receipt.status = (
        PipelineStatus.COMPLETE if not unresolved_texts else PipelineStatus.NEEDS_REVIEW
    )
    receipt.updated_at = utcnow()
    await session.flush()

    result = ResolutionResult(
        receipt=receipt,
        by_source=counts,
        call=call,
        unresolved_texts=unresolved_texts,
    )
    log.info(
        "resolve.completed",
        receipt_id=receipt.id,
        by_source=counts,
        coverage=round(result.coverage, 3),
        unresolved=len(unresolved_texts),
        cost_usd=str(call.cost_usd) if call else "0",
    )
    return result
