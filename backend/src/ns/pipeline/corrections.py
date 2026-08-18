"""Corrections — the core product loop, not a settings screen.

Every fix the user makes is stored and applied to all future receipts
(principle 3). Two things happen on every correction, and the second is the
one that is easy to forget:

1. A `Correction` row, keyed on `(normalized_text, store_id)`, which tier 1 of
   resolution reads on every subsequent receipt.
2. An `EvalExample`, which is how the resolver's accuracy becomes measurable.

Confirmations are recorded too (D6). An eval set built only from corrections is
entirely cases the resolver got wrong — a biased sample of hard cases that can
never demonstrate an improvement. Confirming a right answer is as much a label
as fixing a wrong one, and costs the user one tap.

Corrections store a gram *rule*, never a gram figure (D3). "This 1.2 lb bag of
broccoli weighed 544 g" is true once; "broccoli comes in 340 g bags" is true
every time.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.domain.text import NORMALIZER_VERSION
from ns.logging import get_logger
from ns.models import Correction, EvalExample, Food, LineItem
from ns.models.base import utcnow
from ns.models.enums import EvalSplit, GramsBasis, LabelSource, ResolutionSource
from ns.pipeline.resolve import apply_grams_rule

log = get_logger(__name__)

# One in this many labels is held out. The holdout never feeds the corrections
# table used at inference, so scoring against it is not scoring the resolver on
# answers it was handed.
HOLDOUT_EVERY = 4


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    correction: Correction
    example: EvalExample
    applied_to: int  # line items updated across all receipts, including past ones


def _next_split(existing_count: int) -> EvalSplit:
    """Deterministic round-robin rather than a random draw.

    A random split cannot be reproduced, which makes two eval runs
    incomparable for reasons that have nothing to do with the resolver.
    """
    return EvalSplit.HOLDOUT if (existing_count + 1) % HOLDOUT_EVERY == 0 else EvalSplit.TRAIN


async def _record_example(
    session: AsyncSession,
    line: LineItem,
    *,
    food_id: int | None,
    is_nonfood: bool,
    grams_basis: GramsBasis,
    grams_value: Decimal | None,
    label_source: LabelSource,
    store_id: int | None,
) -> EvalExample:
    existing = (
        (
            await session.execute(
                select(EvalExample).where(
                    col(EvalExample.normalized_text) == line.normalized_text,
                    col(EvalExample.store_id) == store_id,
                )
            )
        )
        .scalars()
        .first()
    )

    if existing is not None:
        # A label revised is the same example with a better answer, not a
        # second example. Duplicates would weight one line more heavily in
        # every future score.
        existing.expected_food_id = food_id
        existing.expected_is_nonfood = is_nonfood
        existing.expected_grams = grams_value
        existing.expected_grams_basis = grams_basis
        existing.label_source = label_source
        existing.raw_text = line.raw_text
        existing.normalizer_version = NORMALIZER_VERSION
        existing.source_line_item_id = line.id
        return existing

    total = (await session.execute(select(EvalExample))).scalars().all()
    example = EvalExample(
        raw_text=line.raw_text,
        normalized_text=line.normalized_text,
        normalizer_version=NORMALIZER_VERSION,
        store_id=store_id,
        expected_food_id=food_id,
        expected_is_nonfood=is_nonfood,
        expected_grams=grams_value,
        expected_grams_basis=grams_basis,
        label_source=label_source,
        split=_next_split(len(total)),
        source_line_item_id=line.id,
    )
    session.add(example)
    await session.flush()
    return example


async def _apply_everywhere(session: AsyncSession, correction: Correction) -> int:
    """Replay a correction across every line it matches, past receipts included.

    A correction that only affected future receipts would leave the user's own
    history disagreeing with their own fix, and would make backfilled receipts
    (D18) permanently worse than new ones.
    """
    conditions = [col(LineItem.normalized_text) == correction.normalized_text]
    if correction.store_id is not None:
        from ns.models import Receipt

        receipt_ids = (
            (
                await session.execute(
                    select(col(Receipt.id)).where(col(Receipt.store_id) == correction.store_id)
                )
            )
            .scalars()
            .all()
        )
        if not receipt_ids:
            return 0
        conditions.append(col(LineItem.receipt_id).in_(receipt_ids))

    lines = (await session.execute(select(LineItem).where(*conditions))).scalars().all()
    food = await session.get(Food, correction.food_id) if correction.food_id else None

    for line in lines:
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
        apply_grams_rule(
            line, correction.grams_basis, correction.grams_value, food=food, override=True
        )

    correction.applied_count += len(lines)
    correction.last_applied_at = utcnow()
    return len(lines)


async def record_correction(
    session: AsyncSession,
    line: LineItem,
    *,
    food_id: int | None = None,
    is_nonfood: bool = False,
    grams_basis: GramsBasis = GramsBasis.UNKNOWN,
    grams_value: Decimal | None = None,
    store_id: int | None = None,
    global_scope: bool = False,
) -> CorrectionResult:
    """Store a user's fix, apply it everywhere, and label it for evaluation.

    `store_id` scopes the correction to one chain, which is the default:
    receipt abbreviations collide across stores, and `KS DICED TOM` means
    something at Costco that it does not mean elsewhere. `global_scope=True`
    records the fallback that applies at every store.
    """
    scope = None if global_scope else store_id

    existing = (
        await session.execute(
            select(Correction).where(
                col(Correction.normalized_text) == line.normalized_text,
                col(Correction.store_id) == scope,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = Correction(normalized_text=line.normalized_text, store_id=scope)
        session.add(existing)

    existing.food_id = food_id
    existing.is_nonfood = is_nonfood
    existing.grams_basis = grams_basis
    existing.grams_value = grams_value
    await session.flush()

    applied = await _apply_everywhere(session, existing)
    example = await _record_example(
        session,
        line,
        food_id=food_id,
        is_nonfood=is_nonfood,
        grams_basis=grams_basis,
        grams_value=grams_value,
        label_source=LabelSource.CORRECTED,
        store_id=scope,
    )
    await session.flush()

    log.info(
        "correction.recorded",
        normalized_text=line.normalized_text,
        store_id=scope,
        food_id=food_id,
        is_nonfood=is_nonfood,
        applied_to=applied,
        split=example.split.value,
    )
    return CorrectionResult(correction=existing, example=example, applied_to=applied)


async def confirm_resolution(
    session: AsyncSession, line: LineItem, *, store_id: int | None = None
) -> EvalExample:
    """Record that the resolver got this line right (D6).

    No Correction is written — there is nothing to override. What this creates
    is a label, and labels of correct answers are what stop the eval set from
    being a curated collection of the resolver's failures.
    """
    if line.food_id is None and line.resolution_source is not ResolutionSource.NONFOOD:
        raise ValueError(
            f"Line {line.id} has no resolution to confirm. Record a correction instead."
        )

    example = await _record_example(
        session,
        line,
        food_id=line.food_id,
        is_nonfood=line.resolution_source is ResolutionSource.NONFOOD,
        grams_basis=line.grams_basis,
        grams_value=line.grams_as_purchased,
        label_source=LabelSource.CONFIRMED,
        store_id=store_id,
    )
    await session.flush()

    log.info(
        "correction.confirmed",
        normalized_text=line.normalized_text,
        food_id=line.food_id,
        split=example.split.value,
    )
    return example
