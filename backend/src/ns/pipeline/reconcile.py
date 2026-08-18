"""Stage 4 — reconcile.

Pure arithmetic: does what the receipt charged actually add up to what it
says the total was? Nothing here knows what a food is, which is why it runs
before resolution (DECISIONS.md D4) — an arithmetically broken receipt should
be caught without spending a cent on an LLM call.

A receipt that does not balance is never quietly persisted as clean. It is
marked suspect together with a report saying what was summed, what was
expected, and — where the arithmetic points at an obvious cause — what would
have made it balance. The report is evidence for the user, not a repair: this
stage never edits a line to force a total to close.

## Two ways receipts handle tax

US receipts print prices excluding sales tax and add it at the end. South
African, Australian, and European receipts print VAT-inclusive prices and
then restate the tax as information — adding it again double-counts.

Rather than keep a table of which country does what, both models are
evaluated and the one that closes is the one the receipt uses. That is a
reading of the evidence, not a guess, and the chosen model is recorded in the
report along with the delta the other model would have produced. When tax is
zero or absent the two models are identical and no claim is made.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.config import get_settings
from ns.logging import get_logger
from ns.models import LineItem, Receipt
from ns.models.base import utcnow
from ns.models.enums import LineItemKind, PipelineStatus, ReconciliationStatus

log = get_logger(__name__)

# How the receipt treats tax. Plain strings rather than a database enum:
# these live only inside the reconciliation report's JSON.
TAX_EXCLUSIVE = "exclusive"  # printed prices exclude tax; total adds it
TAX_INCLUSIVE = "inclusive"  # printed prices already include tax
TAX_NOT_APPLICABLE = "not_applicable"  # no tax stated, so the models coincide
TAX_UNDETERMINED = "undetermined"  # neither model closed

# Lines that carry basket value and therefore belong in the sum. SUBTOTAL and
# TOTAL are summaries of these lines, and TAX is handled by the tax model.
#
# UNKNOWN is included deliberately. Fixture 01 prints bare `SPECIAL` lines
# with positive amounts and no item name; they are part of that basket, and
# excluding them puts the sum 3.92 short on a receipt that is in fact clean.
# An unclassified line with a printed amount is treated at face value, with
# its printed sign, because that is what the paper says.
BASKET_KINDS = frozenset(
    {LineItemKind.PRODUCT, LineItemKind.FEE, LineItemKind.DISCOUNT, LineItemKind.UNKNOWN}
)


@dataclass(frozen=True, slots=True)
class Check:
    """One comparison, in a form the UI can render directly.

    `unit` is carried because not every check is about money — the item-count
    cross-check compares counts, and a renderer that formatted 11 items as
    $0.11 would be the sort of quiet nonsense this project exists to avoid.
    """

    name: str
    expected: int | None
    actual: int | None
    delta: int | None
    passed: bool | None  # None when the receipt states nothing to check against
    detail: str
    unit: str = "cents"

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "expected": self.expected,
            "actual": self.actual,
            "delta": self.delta,
            "unit": self.unit,
            "passed": self.passed,
            "detail": self.detail,
        }


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@dataclass(frozen=True, slots=True)
class Reconciliation:
    status: ReconciliationStatus
    delta_cents: int | None
    tax_model: str
    report: dict[str, object] = field(default_factory=dict)

    @property
    def balanced(self) -> bool:
        return self.status is ReconciliationStatus.BALANCED


def _sum(items: Sequence[LineItem], *kinds: LineItemKind) -> int:
    wanted = set(kinds)
    return sum(item.price_cents for item in items if item.kind in wanted)


def _count_items(items: Sequence[LineItem]) -> int:
    """Items sold, as the receipt would count them.

    A multiplier line means one printed line covers several items — Costco's
    `3 @ 4.29` above the eggs is why the paper says 11 items sold against 9
    printed product lines. A weighted item is one item however much it weighs,
    so quantities that carry a unit count as one.
    """
    total = 0
    for item in items:
        if item.kind is not LineItemKind.PRODUCT:
            continue
        quantity = item.quantity
        if item.unit is None and quantity is not None and quantity == quantity.to_integral_value():
            total += int(quantity)
        else:
            total += 1
    return total


def reconcile_basket(
    line_items: Sequence[LineItem],
    *,
    stated_subtotal_cents: int | None,
    stated_tax_cents: int | None,
    stated_total_cents: int | None,
    stated_item_count: int | None = None,
    tolerance_cents: int | None = None,
) -> Reconciliation:
    """Check a basket's arithmetic against its printed totals.

    Pure: no database, no I/O. `stated_*` values come from the receipt header
    as transcribed; `line_items` are the normalised lines.
    """
    tolerance = (
        tolerance_cents
        if tolerance_cents is not None
        else get_settings().reconciliation_tolerance_cents
    )

    products = _sum(line_items, LineItemKind.PRODUCT)
    fees = _sum(line_items, LineItemKind.FEE)
    discounts = _sum(line_items, LineItemKind.DISCOUNT)
    unclassified = _sum(line_items, LineItemKind.UNKNOWN)
    basket = products + fees + discounts + unclassified

    tax_line_cents = [i.price_cents for i in line_items if i.kind is LineItemKind.TAX]
    tax_lines_cents = sum(tax_line_cents)
    # The header figure wins when the receipt prints one: Costco states a
    # combined tax having listed two rates separately, and the combined figure
    # is the one the total was built from.
    tax = stated_tax_cents if stated_tax_cents is not None else tax_lines_cents

    counted_items = _count_items(line_items)

    base_report: dict[str, object] = {
        "tolerance_cents": tolerance,
        "line_item_count": len(line_items),
        "sums_cents": {
            "products": products,
            "fees": fees,
            "discounts": discounts,
            "unclassified": unclassified,
            "basket": basket,
            "tax_lines": tax_lines_cents,
            "tax_applied": tax,
        },
        "stated_cents": {
            "subtotal": stated_subtotal_cents,
            "tax": stated_tax_cents,
            "total": stated_total_cents,
        },
        "counted_items": counted_items,
    }

    def unreconcilable(reason: str) -> Reconciliation:
        return Reconciliation(
            status=ReconciliationStatus.UNRECONCILABLE,
            delta_cents=None,
            tax_model=TAX_UNDETERMINED,
            report={**base_report, "reason": reason, "checks": [], "hypotheses": []},
        )

    if not line_items:
        return unreconcilable("No line items were extracted, so there is nothing to add up.")

    if stated_total_cents is None:
        if stated_subtotal_cents is None:
            return unreconcilable(
                "The receipt shows neither a total nor a subtotal to check against."
            )
        # The total is unreadable — a crease, a torn corner, or in the case of
        # the Costco fixture our own redaction bar. The subtotal is still
        # printed, and the line items matching it exactly is a real result: it
        # verifies the item prices, which is what cost-per-nutrient is built
        # from. It does not verify the tax or the amount actually paid, and
        # the report says so rather than letting a partial check read as a
        # full one.
        return _reconcile_against_subtotal(
            stated_subtotal_cents=stated_subtotal_cents,
            before_discounts=products + fees + unclassified,
            after_discounts=basket,
            tolerance=tolerance,
            base_report=base_report,
        )

    delta_exclusive = basket + tax - stated_total_cents
    delta_inclusive = basket - stated_total_cents

    if tax == 0:
        # Both models give the same number; claiming to have identified one
        # would be asserting something the receipt never showed.
        tax_model, delta = TAX_NOT_APPLICABLE, delta_exclusive
    elif abs(delta_exclusive) <= tolerance:
        tax_model, delta = TAX_EXCLUSIVE, delta_exclusive
    elif abs(delta_inclusive) <= tolerance:
        tax_model, delta = TAX_INCLUSIVE, delta_inclusive
    else:
        # Neither closed, so the receipt gave no evidence of which model it
        # uses. The delta is reported under the exclusive reading — the more
        # common one, and the one that uses the printed tax as printed —
        # rather than under whichever happens to be less wrong. Picking the
        # smaller of two failures would dress an arbitrary choice up as a
        # finding, and both deltas are in the report either way.
        tax_model = TAX_UNDETERMINED
        delta = delta_exclusive

    balanced = abs(delta) <= tolerance
    computed_total = basket + tax if tax_model != TAX_INCLUSIVE else basket

    checks = [
        Check(
            name="total",
            expected=stated_total_cents,
            actual=computed_total,
            delta=delta,
            passed=balanced,
            detail=(
                "Line items and tax match the printed total."
                if balanced
                else f"Line items and tax differ from the printed total by {delta} cents."
            ),
        ),
        _subtotal_check(
            stated_subtotal_cents,
            before_discounts=products + fees + unclassified,
            after_discounts=basket,
            tolerance=tolerance,
        ),
        _tax_lines_check(stated_tax_cents, tax_line_cents, tolerance),
        _item_count_check(stated_item_count, counted_items),
    ]

    hypotheses = (
        []
        if balanced
        else _hypotheses(
            line_items,
            basket=basket,
            tax=tax,
            tax_model=tax_model,
            stated_total_cents=stated_total_cents,
            unclassified=unclassified,
            tolerance=tolerance,
        )
    )

    report: dict[str, object] = {
        **base_report,
        "checked_against": "total",
        "tax_model": tax_model,
        "computed_total_cents": computed_total,
        "delta_cents": delta,
        "delta_if_tax_exclusive_cents": delta_exclusive,
        "delta_if_tax_inclusive_cents": delta_inclusive,
        "checks": [check.as_dict() for check in checks],
        "hypotheses": hypotheses,
    }

    return Reconciliation(
        status=ReconciliationStatus.BALANCED if balanced else ReconciliationStatus.SUSPECT,
        delta_cents=delta,
        tax_model=tax_model,
        report=report,
    )


def _reconcile_against_subtotal(
    *,
    stated_subtotal_cents: int,
    before_discounts: int,
    after_discounts: int,
    tolerance: int,
    base_report: dict[str, object],
) -> Reconciliation:
    """Check the line items against a printed subtotal, the total being unreadable.

    Which subtotal a receipt prints is not fixed — fixture 01 prints one
    before its loyalty discount and two after — so both readings are tried and
    the one that matches is recorded.
    """
    candidates = {"before discounts": before_discounts}
    if after_discounts != before_discounts:
        candidates["after discounts"] = after_discounts

    label, value = min(candidates.items(), key=lambda pair: abs(pair[1] - stated_subtotal_cents))
    delta = value - stated_subtotal_cents
    balanced = abs(delta) <= tolerance

    check = Check(
        name="subtotal",
        expected=stated_subtotal_cents,
        actual=value,
        delta=delta,
        passed=balanced,
        detail=(
            (
                f"The printed total is unreadable. The line items"
                f"{' ' + label if len(candidates) > 1 else ''} match the printed "
                f"subtotal, which confirms the item prices but not the tax or the "
                f"amount paid."
            )
            if balanced
            else (
                f"The printed total is unreadable, and the line items differ from "
                f"the printed subtotal by {delta} cents."
            )
        ),
    )

    return Reconciliation(
        status=ReconciliationStatus.BALANCED if balanced else ReconciliationStatus.SUSPECT,
        delta_cents=delta,
        tax_model=TAX_UNDETERMINED,
        report={
            **base_report,
            "checked_against": "subtotal",
            "tax_model": TAX_UNDETERMINED,
            "computed_total_cents": None,
            "delta_cents": delta,
            "checks": [check.as_dict()],
            "hypotheses": [],
        },
    )


def _subtotal_check(
    stated: int | None, *, before_discounts: int, after_discounts: int, tolerance: int
) -> Check:
    """Informational only, never a reason to flag a receipt.

    Fixture 01 prints three `SUBTOTAL` lines with different values — one
    before the loyalty discount and two after — so there is no single figure a
    stated subtotal can be held to. Matching either candidate is enough, and
    matching neither is worth showing without condemning the receipt.
    """
    if stated is None:
        return Check(
            name="subtotal",
            expected=None,
            actual=after_discounts,
            delta=None,
            passed=None,
            detail="The receipt prints no subtotal.",
        )

    candidates = {"before discounts": before_discounts}
    if after_discounts != before_discounts:
        candidates["after discounts"] = after_discounts
    for label, value in candidates.items():
        if abs(value - stated) <= tolerance:
            return Check(
                name="subtotal",
                expected=stated,
                actual=value,
                delta=value - stated,
                passed=True,
                detail=(
                    f"Matches the sum of the line items {label}."
                    if len(candidates) > 1
                    else "Matches the sum of the line items."
                ),
            )

    nearest_label, nearest = min(candidates.items(), key=lambda pair: abs(pair[1] - stated))
    return Check(
        name="subtotal",
        expected=stated,
        actual=nearest,
        delta=nearest - stated,
        passed=False,
        detail=(
            f"The printed subtotal of {stated} matches no sum of the line items; "
            f"the nearest is {nearest} ({nearest_label})."
            if len(candidates) > 1
            else f"The printed subtotal of {stated} does not match the line items ({nearest})."
        ),
    )


def _tax_lines_check(stated: int | None, tax_line_cents: Sequence[int], tolerance: int) -> Check:
    """Whether the printed tax lines account for the tax the header states.

    Summing them naively is wrong on a real receipt. Costco prints
    `A 8.50% TAX 0.55`, `E 3.75% TAX 2.97`, *and* `TOTAL TAX 3.52` — the
    components and their summary, so the naive sum is 4.04 over. Both
    readings are tried, and either matching is enough. Nothing here can flag a
    receipt: the header figure is what the total was built from, so a gap in
    the rate lines is a transcription detail, not an error in the money.
    """
    if stated is None or not tax_line_cents:
        return Check(
            name="tax_lines",
            expected=stated,
            actual=sum(tax_line_cents),
            delta=None,
            passed=None,
            detail="Not enough tax detail printed to cross-check.",
        )

    all_lines = sum(tax_line_cents)
    # A line equal to the stated tax is the receipt restating its own total,
    # not another component of it.
    components = sum(cents for cents in tax_line_cents if abs(cents - stated) > tolerance)

    readings = {"the tax lines": all_lines, "the rate lines, excluding the summary": components}
    for label, value in readings.items():
        if abs(value - stated) <= tolerance:
            return Check(
                name="tax_lines",
                expected=stated,
                actual=value,
                delta=value - stated,
                passed=True,
                detail=f"The stated tax is accounted for by {label}.",
            )

    nearest = min(readings.values(), key=lambda value: abs(value - stated))
    return Check(
        name="tax_lines",
        expected=stated,
        actual=nearest,
        delta=nearest - stated,
        passed=False,
        detail=(
            f"The printed tax lines account for {nearest} of a stated {stated}; "
            "a rate line may be missing."
        ),
    )


def _item_count_check(stated: int | None, counted: int) -> Check:
    """Cross-check against the count the receipt prints, where it prints one.

    Informational: a mismatch means quantity parsing missed a multiplier, not
    that the money is wrong.
    """
    if stated is None:
        return Check(
            name="item_count",
            expected=None,
            actual=counted,
            delta=None,
            passed=None,
            unit="items",
            detail="The receipt prints no item count.",
        )

    passed = stated == counted
    return Check(
        name="item_count",
        expected=stated,
        actual=counted,
        delta=counted - stated,
        passed=passed,
        unit="items",
        detail=(
            f"{counted} items counted, matching the printed count."
            if passed
            else f"{counted} items counted against a printed {stated}; a quantity may be unparsed."
        ),
    )


def _hypotheses(
    line_items: Sequence[LineItem],
    *,
    basket: int,
    tax: int,
    tax_model: str,
    stated_total_cents: int,
    unclassified: int,
    tolerance: int,
) -> list[str]:
    """Explanations for a failure, offered and never applied.

    Each is a claim that one specific misreading, corrected, would make the
    arithmetic close exactly. That is a far more useful thing to show than a
    delta on its own, and it keeps the decision with the user.
    """
    applied_tax = 0 if tax_model == TAX_INCLUSIVE else tax
    out: list[str] = []

    unknown_lines = [item for item in line_items if item.kind is LineItemKind.UNKNOWN]
    if unknown_lines and abs(basket - unclassified + applied_tax - stated_total_cents) <= tolerance:
        out.append(
            f"Excluding the {_plural(len(unknown_lines), 'unclassified line')} "
            f"totalling {unclassified} cents would balance — they may not be "
            f"basket amounts."
        )

    positive_discounts = [
        item for item in line_items if item.kind is LineItemKind.DISCOUNT and item.price_cents > 0
    ]
    if positive_discounts:
        adjustment = 2 * sum(item.price_cents for item in positive_discounts)
        if abs(basket - adjustment + applied_tax - stated_total_cents) <= tolerance:
            out.append(
                f"Subtracting rather than adding the "
                f"{_plural(len(positive_discounts), 'discount line')} would balance — "
                f"printed without a minus sign."
            )

    if tax_model == TAX_UNDETERMINED:
        out.append(
            "Neither tax model closes: the receipt balances neither with tax added "
            "to the line items nor with tax already included in them."
        )

    return out


async def reconcile_receipt(session: AsyncSession, receipt: Receipt) -> Reconciliation:
    """Reconcile a normalised receipt and record the verdict.

    Replays from stored data, so it is free to re-run after any change to the
    reconciliation rules.
    """
    rows = await session.execute(
        select(LineItem)
        .where(col(LineItem.receipt_id) == receipt.id)
        .order_by(col(LineItem.line_index))
    )
    line_items = list(rows.scalars().all())

    stated_count = None
    if isinstance(receipt.raw_extraction, dict):
        raw_count = receipt.raw_extraction.get("item_count_stated")
        if isinstance(raw_count, int):
            stated_count = raw_count

    result = reconcile_basket(
        line_items,
        stated_subtotal_cents=receipt.subtotal_cents,
        stated_tax_cents=receipt.tax_cents,
        stated_total_cents=receipt.total_cents,
        stated_item_count=stated_count,
    )

    receipt.reconciliation_status = result.status
    receipt.reconciliation_delta_cents = result.delta_cents
    receipt.reconciliation_report = result.report
    # The stage ran either way. Whether the arithmetic closed is a separate
    # fact, carried by reconciliation_status — a receipt can be fully
    # processed and still not balance, and the UI has to be able to say so.
    receipt.status = PipelineStatus.RECONCILED
    receipt.updated_at = utcnow()
    await session.flush()

    log.info(
        "reconcile.completed",
        receipt_id=receipt.id,
        status=result.status.value,
        delta_cents=result.delta_cents,
        tax_model=result.tax_model,
        line_items=len(line_items),
        hypotheses=len(result.report.get("hypotheses", [])),  # type: ignore[arg-type]
    )
    return result


__all__ = [
    "BASKET_KINDS",
    "TAX_EXCLUSIVE",
    "TAX_INCLUSIVE",
    "TAX_NOT_APPLICABLE",
    "TAX_UNDETERMINED",
    "Check",
    "Reconciliation",
    "reconcile_basket",
    "reconcile_receipt",
]
