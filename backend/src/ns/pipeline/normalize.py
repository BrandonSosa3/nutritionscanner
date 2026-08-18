"""Stage 3 — normalize.

Turns the stored `raw_extraction` into `LineItem` rows: parsed prices in
integer cents, a stable matching key, and grams wherever the receipt states
enough to compute them honestly.

Replays entirely from stored data, so it costs nothing and can be re-run after
any change to the normaliser. Re-running replaces this receipt's line items
rather than appending to them.

Grams are derived here only when the receipt itself says enough. A stated
weight is used directly; a package size printed in the item name is used when
it is a mass. Anything else — counts, volumes without a density, bare
descriptions — is left for resolution to answer, with `grams_basis` recording
which case applied.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ns.domain.money import MoneyParseError, parse_money_to_cents
from ns.domain.text import NORMALIZER_VERSION, normalise
from ns.domain.units import extract_package_size, parse_quantity, to_grams
from ns.logging import get_logger
from ns.models import LineItem, Receipt
from ns.models.base import utcnow
from ns.models.enums import GramsBasis, LineItemKind, PipelineStatus
from ns.providers.anthropic.schemas import ExtractedLineItem, ExtractedReceipt

log = get_logger(__name__)

# Extraction's vocabulary maps onto the persisted enum. `section_header` and
# `payment` lines carry no basket value and are dropped rather than stored.
_KIND_MAP: dict[str, LineItemKind] = {
    "product": LineItemKind.PRODUCT,
    "discount": LineItemKind.DISCOUNT,
    "fee": LineItemKind.FEE,
    "tax": LineItemKind.TAX,
    "subtotal": LineItemKind.SUBTOTAL,
    "total": LineItemKind.TOTAL,
    "unknown": LineItemKind.UNKNOWN,
}

_DROPPED_KINDS = {"section_header", "payment"}


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    receipt: Receipt
    line_items: list[LineItem]
    dropped: int
    unparseable_amounts: list[str]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        # Extraction is instructed to emit ISO or null. Anything else is a
        # defect worth surfacing rather than coercing.
        log.warning("normalize.unparseable_date", value=value)
        return None


def _money_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return parse_money_to_cents(value)
    except MoneyParseError:
        return None


def _parse_count(text: str | None) -> Decimal | None:
    """A bare multiplier like `3` from Costco's `3 @ 4.29`.

    `parse_quantity` deliberately refuses text with no unit, because a bare
    number is not a measurement. It is still a count, and dropping it loses
    the only evidence that one printed line covers three cartons of eggs —
    which is exactly what the item-count cross-check reads.
    """
    if not text:
        return None
    try:
        value = Decimal(str(text).strip())
    except (ArithmeticError, ValueError):
        return None
    return value if value > 0 else None


def _derive_grams(item: ExtractedLineItem) -> tuple[Decimal | None, GramsBasis]:
    """Grams from what the receipt states, or nothing.

    Precedence matters: a weight the scale measured beats a size printed in
    the item name. Fixture 03 prints `BANANAS LOOSE 17KG` — a bin code — with
    the real 0.596 kg on a continuation line.
    """
    stated = parse_quantity(item.weight_text)
    if stated is not None:
        grams = to_grams(stated)
        if grams is not None:
            return grams, GramsBasis.FROM_RECEIPT

    package = extract_package_size(item.raw_text)
    if package is not None:
        grams = to_grams(package)
        if grams is not None:
            # Multiply through by quantity when the receipt states one, so
            # "3 @ 4.29" of 340g packs is 1020g rather than 340g.
            quantity = parse_quantity(item.quantity) if item.quantity else None
            multiplier = quantity.value if quantity is not None else Decimal(1)
            try:
                count = Decimal(item.quantity) if item.quantity else multiplier
            except (ValueError, ArithmeticError):
                count = Decimal(1)
            return (grams * count).quantize(Decimal("0.001")), GramsBasis.PER_PACKAGE

    # Volume without a density, a bare count, or nothing at all. Resolution
    # answers this; guessing here would be inventing data.
    return None, GramsBasis.UNKNOWN


async def normalize_receipt(session: AsyncSession, receipt: Receipt) -> NormalizationResult:
    """Build LineItem rows from the receipt's stored extraction."""
    if receipt.raw_extraction is None:
        raise ValueError(
            f"Receipt {receipt.id} has no extraction to normalise. Run extraction first."
        )

    extraction = ExtractedReceipt.model_validate(receipt.raw_extraction)

    # Idempotent: replace rather than append, so re-running after a normaliser
    # change leaves exactly one set of line items.
    await session.execute(delete(LineItem).where(col(LineItem.receipt_id) == receipt.id))

    line_items: list[LineItem] = []
    dropped = 0
    unparseable: list[str] = []

    for item in extraction.line_items:
        if item.kind in _DROPPED_KINDS:
            dropped += 1
            continue

        price_cents = _money_or_none(item.amount)
        if price_cents is None:
            # A line with no readable amount cannot participate in
            # reconciliation. Record it so the receipt can explain itself.
            if item.amount is not None:
                unparseable.append(item.amount)
            dropped += 1
            continue

        grams, basis = _derive_grams(item)
        quantity = parse_quantity(item.quantity)
        # A measured quantity carries its unit; a bare count carries none.
        count = _parse_count(item.quantity) if quantity is None else None

        line_items.append(
            LineItem(
                receipt_id=receipt.id,
                line_index=item.line_index,
                raw_text=item.raw_text[:500],
                normalized_text=normalise(item.raw_text)[:300],
                normalizer_version=NORMALIZER_VERSION,
                kind=_KIND_MAP.get(item.kind, LineItemKind.UNKNOWN),
                price_cents=price_cents,
                quantity=quantity.value if quantity else count,
                unit=quantity.unit if quantity else None,
                grams_as_purchased=grams,
                grams_basis=basis,
            )
        )

    session.add_all(line_items)

    receipt.purchased_at = _parse_date(extraction.purchased_at)
    receipt.subtotal_cents = _money_or_none(extraction.subtotal)
    receipt.tax_cents = _money_or_none(extraction.tax_total)
    receipt.total_cents = _money_or_none(extraction.total)
    receipt.status = PipelineStatus.NORMALIZED
    receipt.updated_at = utcnow()

    await session.flush()

    with_grams = sum(1 for item in line_items if item.grams_as_purchased is not None)
    log.info(
        "normalize.completed",
        receipt_id=receipt.id,
        line_items=len(line_items),
        dropped=dropped,
        with_grams=with_grams,
        unparseable_amounts=len(unparseable),
        normalizer_version=NORMALIZER_VERSION,
    )

    return NormalizationResult(
        receipt=receipt,
        line_items=line_items,
        dropped=dropped,
        unparseable_amounts=unparseable,
    )
