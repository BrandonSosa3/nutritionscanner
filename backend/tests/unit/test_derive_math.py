"""Price-per-100 g arithmetic. Pure."""

from decimal import Decimal

from ns.pipeline.derive import price_per_100g


def test_a_straightforward_ratio() -> None:
    # $4.45 for 907.185 g of cheese.
    assert price_per_100g(445, Decimal("907.185")) == Decimal("49.0528")


def test_exactly_one_hundred_grams_is_the_price_itself() -> None:
    assert price_per_100g(299, Decimal("100")) == Decimal("299.0000")


def test_a_kilogram_is_a_tenth_of_the_price() -> None:
    assert price_per_100g(1000, Decimal("1000")) == Decimal("100.0000")


def test_zero_weight_has_no_answer() -> None:
    """Not a small weight — an absent one. Dividing would raise, or produce a
    number with no meaning."""
    assert price_per_100g(500, Decimal("0")) is None


def test_negative_weight_has_no_answer() -> None:
    assert price_per_100g(500, Decimal("-100")) is None


def test_a_free_item_costs_nothing_per_gram() -> None:
    assert price_per_100g(0, Decimal("500")) == Decimal("0.0000")
