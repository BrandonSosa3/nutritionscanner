"""Stage 6 — derive.

Turns resolved line items into `PriceObservation` rows: what a food cost, per
100 g, at a store, on a date. This is the table the flagship ranking reads and
the one Phase 2 price history is built from.

Rebuilt, never patched (D9). Both inputs — price and grams — change when a
line is corrected, so a correction that fixed a weight would otherwise leave
stale price history behind, in exactly the data the ranking sorts by. Deriving
drops and regenerates a receipt's observations every time.

Two things are deliberately excluded:

**Lines with no grams.** Price per 100 g is undefined without a weight, and a
line resolved to a food but never weighed contributes nothing here. It still
counts as uncovered mass in the summary, which is where the gap belongs.

**Nonfood.** A carrier bag has a price and no nutritional meaning.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.logging import get_logger
from ns.models import LineItem, PriceObservation, Receipt
from ns.models.base import utcnow
from ns.models.enums import LineItemKind, ResolutionSource

log = get_logger(__name__)

_RATIO_PRECISION = Decimal("0.0001")

# Lines a price can be observed from. A discount line is basket arithmetic, and
# tax and totals are not foods.
OBSERVABLE_KINDS = frozenset({LineItemKind.PRODUCT, LineItemKind.UNKNOWN})


@dataclass(frozen=True, slots=True)
class DerivationResult:
    receipt: Receipt
    observations: list[PriceObservation]
    skipped_no_grams: int
    skipped_unresolved: int

    @property
    def observed(self) -> int:
        return len(self.observations)


def price_per_100g(price_cents: int, grams: Decimal) -> Decimal | None:
    """Cents per 100 g, or None when the question has no answer.

    A zero or negative weight is not a small weight — it is an absent one, and
    dividing by it would either raise or produce a number with no meaning.
    """
    if grams <= 0:
        return None
    return (Decimal(price_cents) * Decimal(100) / grams).quantize(_RATIO_PRECISION)


def _was_discounted(line: LineItem) -> bool:
    """Whether this line's price reflects a sale.

    A sale price is not what a food costs (D11), so observations carry the flag
    and the baseline ranking excludes them rather than averaging them in.
    """
    return line.discount_cents > 0


async def derive_receipt(session: AsyncSession, receipt: Receipt) -> DerivationResult:
    """Rebuild this receipt's price observations from its resolved lines."""
    if receipt.purchased_at is None:
        raise ValueError(
            f"Receipt {receipt.id} has no purchase date, so its prices cannot be "
            "placed in time. Normalise it first, or set the date by hand."
        )

    rows = await session.execute(
        select(LineItem)
        .where(col(LineItem.receipt_id) == receipt.id)
        .order_by(col(LineItem.line_index))
    )
    line_items = list(rows.scalars().all())

    # Rebuilt, not merged. A corrected weight changes the ratio, and a stale
    # row would keep contributing to the ranking with the old one.
    await session.execute(
        delete(PriceObservation).where(
            col(PriceObservation.line_item_id).in_([i.id for i in line_items] or [0])
        )
    )

    observations: list[PriceObservation] = []
    skipped_no_grams = 0
    skipped_unresolved = 0

    for line in line_items:
        if line.kind not in OBSERVABLE_KINDS:
            continue
        if line.food_id is None or line.resolution_source is ResolutionSource.NONFOOD:
            skipped_unresolved += 1
            continue

        # Edible weight is what the price should be spread over: paying for a
        # melon's rind is real, but 100 g of melon means 100 g of melon.
        grams = line.grams_edible or line.grams_as_purchased
        ratio = price_per_100g(line.price_cents, grams) if grams is not None else None
        if grams is None or ratio is None:
            skipped_no_grams += 1
            continue

        observations.append(
            PriceObservation(
                line_item_id=line.id,
                food_id=line.food_id,
                store_id=receipt.store_id,
                observed_at=receipt.purchased_at,
                price_cents=line.price_cents,
                grams=grams,
                price_cents_per_100g=ratio,
                grams_basis=line.grams_basis,
                was_discounted=_was_discounted(line),
            )
        )

    session.add_all(observations)
    receipt.updated_at = utcnow()
    await session.flush()

    log.info(
        "derive.completed",
        receipt_id=receipt.id,
        observations=len(observations),
        skipped_no_grams=skipped_no_grams,
        skipped_unresolved=skipped_unresolved,
    )
    return DerivationResult(
        receipt=receipt,
        observations=observations,
        skipped_no_grams=skipped_no_grams,
        skipped_unresolved=skipped_unresolved,
    )
