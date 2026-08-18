"""The gram arithmetic resolution applies. Pure — no database, no model.

This is one of the three places the brief names as where silent wrongness
lives, and the rules here are the ones a correction replays onto every future
receipt. Getting them wrong corrupts history quietly.
"""

from decimal import Decimal

import pytest

from ns.models import Food, LineItem
from ns.models.enums import GramsBasis, LineItemKind
from ns.pipeline.resolve import _line_count, apply_grams_rule


def line(
    *,
    grams: str | None = None,
    basis: GramsBasis = GramsBasis.UNKNOWN,
    quantity: str | None = None,
    unit: str | None = None,
) -> LineItem:
    return LineItem(
        receipt_id=1,
        line_index=0,
        raw_text="ITEM",
        normalized_text="item",
        normalizer_version="v1",
        kind=LineItemKind.PRODUCT,
        price_cents=100,
        quantity=Decimal(quantity) if quantity is not None else None,
        unit=unit,
        grams_as_purchased=Decimal(grams) if grams is not None else None,
        grams_basis=basis,
    )


def food(**kwargs: object) -> Food:
    defaults: dict[str, object] = {"canonical_name": "test food"}
    return Food(**{**defaults, **kwargs})  # type: ignore[arg-type]


# ── What counts as "how many" ─────────────────────────────────────────────


def test_a_bare_count_is_a_multiplier() -> None:
    assert _line_count(line(quantity="3")) == Decimal(3)


def test_a_measured_quantity_is_one_item() -> None:
    """0.778 kg is one item that weighs that much, not 0.778 of an item."""
    assert _line_count(line(quantity="0.778", unit="kg")) == Decimal(1)


def test_no_quantity_is_one() -> None:
    assert _line_count(line()) == Decimal(1)


# ── The rule, not the figure (D3) ─────────────────────────────────────────


def test_a_per_package_rule_multiplies_by_this_line_s_own_count() -> None:
    """ "Eggs come in 900 g boxes" times three boxes, not "that box weighed 900 g"."""
    item = line(quantity="3")

    apply_grams_rule(item, GramsBasis.PER_PACKAGE, Decimal("900"))

    assert item.grams_as_purchased == Decimal("2700.000")
    assert item.grams_basis is GramsBasis.PER_PACKAGE


def test_a_per_unit_rule_multiplies_the_same_way() -> None:
    item = line(quantity="4")
    apply_grams_rule(item, GramsBasis.PER_UNIT_ESTIMATE, Decimal("150"))
    assert item.grams_as_purchased == Decimal("600.000")


def test_a_weight_the_receipt_stated_is_never_overwritten_by_an_estimate() -> None:
    """The paper said it. A model's guess does not get to replace it."""
    item = line(grams="743.891", basis=GramsBasis.FROM_RECEIPT)

    apply_grams_rule(item, GramsBasis.PER_PACKAGE, Decimal("500"))

    assert item.grams_as_purchased == Decimal("743.891")
    assert item.grams_basis is GramsBasis.FROM_RECEIPT


def test_a_correction_does_outrank_the_receipt() -> None:
    """`BANANAS LOOSE 17KG` is a bin code. The user has to be able to say so.

    Authority runs correction, then receipt, then estimate — so `override` is
    set by the correction path and by nothing else.
    """
    item = line(grams="17000", basis=GramsBasis.FROM_RECEIPT)

    apply_grams_rule(item, GramsBasis.PER_PACKAGE, Decimal("596"), override=True)

    assert item.grams_as_purchased == Decimal("596.000")
    assert item.grams_basis is GramsBasis.PER_PACKAGE


def test_a_correction_with_no_gram_rule_leaves_the_weight_alone() -> None:
    """Fixing an identity is not a claim about the weight."""
    item = line(grams="743.891", basis=GramsBasis.FROM_RECEIPT)

    apply_grams_rule(item, GramsBasis.UNKNOWN, None, override=True)

    assert item.grams_as_purchased == Decimal("743.891")


def test_an_unknown_rule_leaves_grams_alone() -> None:
    item = line()
    apply_grams_rule(item, GramsBasis.UNKNOWN, None)
    assert item.grams_as_purchased is None
    assert item.grams_basis is GramsBasis.UNKNOWN


def test_a_rule_with_no_value_changes_nothing() -> None:
    item = line()
    apply_grams_rule(item, GramsBasis.PER_PACKAGE, None)
    assert item.grams_as_purchased is None


# ── Density ───────────────────────────────────────────────────────────────


def test_volume_converts_only_once_the_food_s_density_is_known() -> None:
    item = line()
    apply_grams_rule(
        item, GramsBasis.DENSITY, Decimal("375"), food=food(density_g_per_ml=Decimal("0.92"))
    )
    assert item.grams_as_purchased == Decimal("345.000")
    assert item.grams_basis is GramsBasis.DENSITY


def test_volume_without_a_density_stays_unconverted() -> None:
    """Never assume water. 375 ml of oil is 345 g, and that 8% is the point."""
    item = line()
    apply_grams_rule(item, GramsBasis.DENSITY, Decimal("375"), food=food())
    assert item.grams_as_purchased is None


# ── Edible portion ────────────────────────────────────────────────────────


def test_edible_portion_is_applied_and_snapshotted() -> None:
    """A banana line is peel-inclusive weight."""
    item = line()

    apply_grams_rule(
        item,
        GramsBasis.PER_UNIT_ESTIMATE,
        Decimal("120"),
        food=food(edible_portion_pct=Decimal("64")),
    )

    assert item.grams_as_purchased == Decimal("120.000")
    assert item.grams_edible == Decimal("76.800")
    # Snapshotted, so editing the Food later cannot silently rewrite history.
    assert item.edible_portion_pct_applied == Decimal("64")


def test_edible_portion_applies_to_a_receipt_stated_weight_too() -> None:
    """The weight is not overwritten, but the edible share still has to be taken."""
    item = line(grams="1000", basis=GramsBasis.FROM_RECEIPT)

    apply_grams_rule(item, GramsBasis.UNKNOWN, None, food=food(edible_portion_pct=Decimal("75")))

    assert item.grams_as_purchased == Decimal("1000")
    assert item.grams_edible == Decimal("750.000")


def test_a_food_with_no_waste_is_fully_edible() -> None:
    item = line()
    apply_grams_rule(item, GramsBasis.PER_PACKAGE, Decimal("500"), food=food())
    assert item.grams_edible == Decimal("500.000")


def test_no_food_means_no_edible_figure() -> None:
    """Nonfood and unresolved lines have no edible portion to speak of."""
    item = line()
    apply_grams_rule(item, GramsBasis.PER_PACKAGE, Decimal("500"))
    assert item.grams_as_purchased == Decimal("500.000")
    assert item.grams_edible is None


@pytest.mark.parametrize("basis", list(GramsBasis))
def test_every_basis_is_handled_without_raising(basis: GramsBasis) -> None:
    apply_grams_rule(line(), basis, Decimal("100"), food=food())
