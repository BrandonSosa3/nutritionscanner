"""Reconciliation arithmetic, in isolation.

Every scenario here is drawn from a real receipt in `tests/fixtures/receipts`
and its documented sums. Reconciliation is pure integer arithmetic, so it can
be tested without a database, a network call, or an image.
"""

from decimal import Decimal

import pytest

from ns.models import LineItem
from ns.models.enums import LineItemKind, ReconciliationStatus
from ns.pipeline.reconcile import (
    TAX_EXCLUSIVE,
    TAX_INCLUSIVE,
    TAX_NOT_APPLICABLE,
    TAX_UNDETERMINED,
    reconcile_basket,
)

_INDEX = iter(range(1, 10_000))


def line(
    kind: LineItemKind,
    cents: int,
    *,
    text: str = "ITEM",
    quantity: str | None = None,
    unit: str | None = None,
) -> LineItem:
    return LineItem(
        receipt_id=1,
        line_index=next(_INDEX),
        raw_text=text,
        normalized_text=text.lower(),
        normalizer_version="v1",
        kind=kind,
        price_cents=cents,
        quantity=Decimal(quantity) if quantity is not None else None,
        unit=unit,
    )


def product(cents: int, **kwargs: object) -> LineItem:
    return line(LineItemKind.PRODUCT, cents, **kwargs)  # type: ignore[arg-type]


def check(result: object, name: str) -> dict[str, object]:
    report = result.report  # type: ignore[attr-defined]
    checks: list[dict[str, object]] = report["checks"]
    return next(c for c in checks if c["name"] == name)


# ── 04-us-costco: the straightforward case ────────────────────────────────
# 85.61 products + 3.52 tax = 89.13 total. Two tax rates, combined in the
# header. `3 @ 4.29` eggs is why 9 printed lines are 11 items sold.


def test_costco_balances_exactly() -> None:
    items = [
        product(2399),
        product(649),
        product(1287, quantity="3"),  # 3 @ 4.29
        product(445),
        product(3781),
        line(LineItemKind.TAX, 253, text="A 8.50%"),
        line(LineItemKind.TAX, 99, text="E 3.75%"),
        line(LineItemKind.SUBTOTAL, 8561, text="SUBTOTAL"),
        line(LineItemKind.TOTAL, 8913, text="**** TOTAL"),
    ]

    result = reconcile_basket(
        items,
        stated_subtotal_cents=8561,
        stated_tax_cents=352,
        stated_total_cents=8913,
        stated_item_count=7,
    )

    assert result.status is ReconciliationStatus.BALANCED
    assert result.delta_cents == 0
    assert result.tax_model == TAX_EXCLUSIVE
    assert result.report["computed_total_cents"] == 8913


def test_subtotal_and_total_lines_are_not_double_counted() -> None:
    """A SUBTOTAL line is a summary of the basket, not a member of it."""
    without = reconcile_basket(
        [product(1000), product(500)],
        stated_subtotal_cents=1500,
        stated_tax_cents=None,
        stated_total_cents=1500,
    )
    with_summaries = reconcile_basket(
        [
            product(1000),
            product(500),
            line(LineItemKind.SUBTOTAL, 1500),
            line(LineItemKind.TOTAL, 1500),
        ],
        stated_subtotal_cents=1500,
        stated_tax_cents=None,
        stated_total_cents=1500,
    )

    assert without.delta_cents == with_summaries.delta_cents == 0
    assert with_summaries.status is ReconciliationStatus.BALANCED


def test_multiplier_line_is_counted_as_several_items() -> None:
    """Costco prints 11 items sold against 9 product lines; the `3 @` explains it."""
    items = [product(1287, quantity="3")] + [product(100) for _ in range(8)]

    result = reconcile_basket(
        items,
        stated_subtotal_cents=None,
        stated_tax_cents=None,
        stated_total_cents=2087,
        stated_item_count=11,
    )

    assert result.report["counted_items"] == 11
    assert check(result, "item_count")["passed"] is True


def test_weighted_item_counts_as_one_however_much_it_weighs() -> None:
    result = reconcile_basket(
        [product(466, quantity="0.778", unit="kg")],
        stated_subtotal_cents=None,
        stated_tax_cents=None,
        stated_total_cents=466,
        stated_item_count=1,
    )
    assert result.report["counted_items"] == 1


# ── 01-au-produce: the trap ───────────────────────────────────────────────
# Items sum to exactly 39.20 — including standalone `SPECIAL` lines that carry
# a positive amount and no item name. 39.20 - 15.00 loyalty = 24.20. No tax
# line anywhere.


def au_produce_basket() -> list[LineItem]:
    return [
        product(3724, text="ZUCHINNI GREEN"),
        line(LineItemKind.UNKNOWN, 196, text="SPECIAL"),
        line(LineItemKind.DISCOUNT, -1500, text="LOYALTY"),
    ]


def test_au_produce_balances_with_no_tax_line() -> None:
    result = reconcile_basket(
        au_produce_basket(),
        stated_subtotal_cents=3920,
        stated_tax_cents=None,
        stated_total_cents=2420,
    )

    assert result.status is ReconciliationStatus.BALANCED
    assert result.delta_cents == 0
    # No tax was stated, so no claim is made about how this receipt treats it.
    assert result.tax_model == TAX_NOT_APPLICABLE


def test_special_lines_count_at_face_value_not_as_discounts() -> None:
    """The documented failure mode: treating `SPECIAL` as negative gives 35.28.

    Reconciliation uses the printed sign. Reading the sign the other way would
    condemn a receipt that is arithmetically perfect.
    """
    flipped = [
        product(3724),
        line(LineItemKind.UNKNOWN, -196, text="SPECIAL"),
        line(LineItemKind.DISCOUNT, -1500),
    ]
    result = reconcile_basket(
        flipped, stated_subtotal_cents=3920, stated_tax_cents=None, stated_total_cents=2420
    )

    assert result.status is ReconciliationStatus.SUSPECT
    assert result.delta_cents == -392  # 24.20 - 20.28


def test_subtotal_before_discounts_is_accepted() -> None:
    """Fixture 01 prints three SUBTOTAL lines, before and after the discount.

    Matching either candidate is enough; the subtotal check never flags.
    """
    result = reconcile_basket(
        au_produce_basket(),
        stated_subtotal_cents=3920,  # before the loyalty discount
        stated_tax_cents=None,
        stated_total_cents=2420,
    )
    subtotal = check(result, "subtotal")
    assert subtotal["passed"] is True
    assert subtotal["actual"] == 3920


def test_a_subtotal_matching_nothing_does_not_flag_the_receipt() -> None:
    result = reconcile_basket(
        au_produce_basket(),
        stated_subtotal_cents=9999,
        stated_tax_cents=None,
        stated_total_cents=2420,
    )

    assert check(result, "subtotal")["passed"] is False
    # The total still closes, so the receipt is clean.
    assert result.status is ReconciliationStatus.BALANCED


# ── 05-us-sprouts: deposits are charges ───────────────────────────────────
# 3.99 + 0.05 + 3.00 + 3.99 + 0.05 = 11.08 = BALANCE DUE.


def test_sprouts_deposits_are_part_of_the_total() -> None:
    items = [
        product(399),
        line(LineItemKind.FEE, 5, text="*CRV FS/TX 05"),
        product(300, text="1 @ 2 FOR 6.00"),
        product(399),
        line(LineItemKind.FEE, 5, text="*CRV FS/TX 05"),
        line(LineItemKind.TAX, 0, text="TAX 1"),
    ]

    result = reconcile_basket(
        items, stated_subtotal_cents=None, stated_tax_cents=0, stated_total_cents=1108
    )

    assert result.status is ReconciliationStatus.BALANCED
    assert result.report["sums_cents"]["fees"] == 10


# ── 03-za-spar: VAT-inclusive pricing ─────────────────────────────────────


def test_vat_inclusive_receipt_does_not_double_count_tax() -> None:
    """SPAR prints VAT-inclusive prices and restates the VAT as information.

    Adding it again would put this receipt 13.04 over its own total.
    """
    items = [product(6000), product(4000), line(LineItemKind.FEE, 75, text="CARRIER BAG 24L")]

    result = reconcile_basket(
        items, stated_subtotal_cents=None, stated_tax_cents=1314, stated_total_cents=10075
    )

    assert result.status is ReconciliationStatus.BALANCED
    assert result.tax_model == TAX_INCLUSIVE
    assert result.delta_cents == 0
    # The rejected model is kept, so the choice is auditable.
    assert result.report["delta_if_tax_exclusive_cents"] == 1314


def test_tax_exclusive_is_preferred_when_both_models_would_close() -> None:
    """With zero tax the two models coincide and no claim is made."""
    result = reconcile_basket(
        [product(1000)], stated_subtotal_cents=1000, stated_tax_cents=0, stated_total_cents=1000
    )
    assert result.tax_model == TAX_NOT_APPLICABLE


def test_neither_tax_model_closing_is_reported_as_such() -> None:
    result = reconcile_basket(
        [product(1000)], stated_subtotal_cents=None, stated_tax_cents=100, stated_total_cents=5000
    )

    assert result.status is ReconciliationStatus.SUSPECT
    assert result.tax_model == TAX_UNDETERMINED
    # The smaller of the two deltas is the honest description of the gap.
    assert result.delta_cents == -3900
    assert any("Neither tax model" in h for h in result.report["hypotheses"])


# ── Tolerance ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (1000, ReconciliationStatus.BALANCED),
        (1002, ReconciliationStatus.BALANCED),  # exactly at tolerance
        (998, ReconciliationStatus.BALANCED),
        (1003, ReconciliationStatus.SUSPECT),  # one cent past it
        (997, ReconciliationStatus.SUSPECT),
    ],
)
def test_tolerance_boundary(total: int, expected: ReconciliationStatus) -> None:
    result = reconcile_basket(
        [product(1000)],
        stated_subtotal_cents=None,
        stated_tax_cents=None,
        stated_total_cents=total,
        tolerance_cents=2,
    )
    assert result.status is expected


# ── Nothing to reconcile ──────────────────────────────────────────────────


def test_a_receipt_with_neither_total_nor_subtotal_is_unreconcilable() -> None:
    result = reconcile_basket(
        [product(1000)], stated_subtotal_cents=None, stated_tax_cents=None, stated_total_cents=None
    )

    assert result.status is ReconciliationStatus.UNRECONCILABLE
    assert result.delta_cents is None
    assert "neither a total nor a subtotal" in str(result.report["reason"])


# ── An unreadable total, with a readable subtotal ─────────────────────────
# The Costco fixture's own redaction bar covers its total. Creases and torn
# corners do the same on real receipts.


def test_an_unreadable_total_falls_back_to_the_subtotal() -> None:
    result = reconcile_basket(
        [product(8561)],
        stated_subtotal_cents=8561,
        stated_tax_cents=352,
        stated_total_cents=None,
    )

    assert result.status is ReconciliationStatus.BALANCED
    assert result.delta_cents == 0
    assert result.report["checked_against"] == "subtotal"
    # The claim made is narrower than a full reconciliation, and says so.
    detail = str(result.report["checks"][0]["detail"])  # type: ignore[index]
    assert "not the tax or the amount paid" in detail


def test_a_full_check_records_that_it_used_the_total() -> None:
    result = reconcile_basket(
        [product(1000)], stated_subtotal_cents=1000, stated_tax_cents=None, stated_total_cents=1000
    )
    assert result.report["checked_against"] == "total"


def test_an_unreadable_total_still_catches_wrong_line_items() -> None:
    result = reconcile_basket(
        [product(9000)],
        stated_subtotal_cents=8561,
        stated_tax_cents=None,
        stated_total_cents=None,
    )

    assert result.status is ReconciliationStatus.SUSPECT
    assert result.delta_cents == 439


def test_the_subtotal_fallback_makes_no_claim_about_tax_treatment() -> None:
    """A subtotal says nothing about whether prices include tax."""
    result = reconcile_basket(
        [product(8561)],
        stated_subtotal_cents=8561,
        stated_tax_cents=352,
        stated_total_cents=None,
    )
    assert result.tax_model == TAX_UNDETERMINED


def test_a_receipt_with_no_line_items_is_unreconcilable() -> None:
    result = reconcile_basket(
        [], stated_subtotal_cents=None, stated_tax_cents=None, stated_total_cents=1000
    )

    assert result.status is ReconciliationStatus.UNRECONCILABLE
    assert "nothing to add up" in str(result.report["reason"])


# ── Hypotheses: explaining a failure without repairing it ─────────────────


def test_a_stray_unclassified_line_is_named_as_the_likely_cause() -> None:
    """The real defect this caught: an extraction turned `TOTAL NUMBER OF
    ITEMS SOLD = 11` into an $11.00 line. The arithmetic points straight at it.
    """
    items = [product(8561), line(LineItemKind.UNKNOWN, 1100, text="TOTAL NUMBER OF ITEMS SOLD")]

    result = reconcile_basket(
        items, stated_subtotal_cents=8561, stated_tax_cents=352, stated_total_cents=8913
    )

    assert result.status is ReconciliationStatus.SUSPECT
    assert result.delta_cents == 1100
    assert any("unclassified" in h for h in result.report["hypotheses"])
    # Named, not applied. The stored sums still include the line.
    assert result.report["sums_cents"]["unclassified"] == 1100


def test_a_discount_printed_without_a_minus_is_named_as_the_likely_cause() -> None:
    items = [product(3724), line(LineItemKind.DISCOUNT, 1500, text="LOYALTY")]

    result = reconcile_basket(
        items, stated_subtotal_cents=None, stated_tax_cents=None, stated_total_cents=2224
    )

    assert result.status is ReconciliationStatus.SUSPECT
    assert any("Subtracting rather than adding" in h for h in result.report["hypotheses"])


def test_a_balanced_receipt_offers_no_hypotheses() -> None:
    result = reconcile_basket(
        [product(1000)], stated_subtotal_cents=None, stated_tax_cents=None, stated_total_cents=1000
    )
    assert result.report["hypotheses"] == []


# ── Tax line cross-check ──────────────────────────────────────────────────


def test_missing_tax_rate_line_is_surfaced() -> None:
    """Costco prints two rates and a combined total; only one rate captured."""
    items = [product(8561), line(LineItemKind.TAX, 253, text="A 8.50%")]

    result = reconcile_basket(
        items, stated_subtotal_cents=8561, stated_tax_cents=352, stated_total_cents=8913
    )

    tax_check = check(result, "tax_lines")
    assert tax_check["passed"] is False
    assert tax_check["delta"] == -99
    # The header figure is what the total was built from, so the receipt is
    # still clean — the missing rate line is a transcription gap, not an error
    # in the money.
    assert result.status is ReconciliationStatus.BALANCED


def test_tax_cross_check_is_skipped_when_there_is_nothing_to_compare() -> None:
    result = reconcile_basket(
        [product(1000)], stated_subtotal_cents=None, stated_tax_cents=None, stated_total_cents=1000
    )
    assert check(result, "tax_lines")["passed"] is None
    assert check(result, "item_count")["passed"] is None
    assert check(result, "subtotal")["passed"] is None
