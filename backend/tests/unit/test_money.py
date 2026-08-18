"""Money parsing, exercised with strings taken from the fixture receipts.

Every literal here appears on one of the five real receipts. If the parser
handles these, it handles the formats we actually have.
"""

import pytest

from ns.domain.money import (
    MoneyParseError,
    cents_to_decimal,
    format_cents,
    parse_money_to_cents,
)


@pytest.mark.parametrize(
    ("text", "expected", "source"),
    [
        ("4.66", 466, "01 zucchini"),
        ("$24.20", 2420, "01 total"),
        ("-15.00", -1500, "01 LOYALTY discount"),
        ("0.99", 99, "01 SPECIAL"),
        ("6.99", 699, "02 tortillas"),
        ("2.00-", -200, "02 trailing-minus coupon"),
        ("45.44", 4544, "02 balance"),
        ("17.99", 1799, "03 worcester sauce"),
        ("338.16", 33816, "03 total, rand"),
        ("0.75", 75, "03 carrier bag"),
        ("23.99", 2399, "04 chicken breast"),
        ("89.13", 8913, "04 total"),
        ("3.52", 352, "04 tax"),
        ("11.08", 1108, "05 balance due"),
        ("0.05", 5, "05 CRV deposit"),
        ("0.00", 0, "05 change"),
    ],
)
def test_parses_real_receipt_amounts(text: str, expected: int, source: str) -> None:
    assert parse_money_to_cents(text) == expected, source


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("R 17.99", 1799),  # SPAR prints a rand prefix
        ("$ 4.66", 466),
        ("1,234.56", 123456),  # thousands separator
        ("  8.99  ", 899),
        ("5", 500),  # whole units, no decimal point
        ("5.5", 550),  # single decimal place
        ("-0.05", -5),
    ],
)
def test_parses_format_variations(text: str, expected: int) -> None:
    assert parse_money_to_cents(text) == expected


def test_trailing_and_leading_minus_agree() -> None:
    """Whole Foods prints `2.00-`; other receipts print `-2.00`."""
    assert parse_money_to_cents("2.00-") == parse_money_to_cents("-2.00") == -200


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "abc",
        "TOTAL",
        "4.666",  # three decimals is not a price
        "--5.00",  # two minus signs
        "-5.00-",  # sign on both ends
        "1.2.3",
        "@ 5.99/kg",  # a unit-price fragment, not an amount
    ],
)
def test_refuses_ambiguous_input(text: str) -> None:
    """Refusing beats guessing: a misread price corrupts reconciliation
    silently, where an unparsed one is visible (principle 2)."""
    with pytest.raises(MoneyParseError):
        parse_money_to_cents(text)


def test_cents_round_trip_without_float_drift() -> None:
    """The reason money is integer cents at all."""
    total = 0
    for _ in range(1000):
        total += parse_money_to_cents("0.07")
    assert total == 7000
    assert format_cents(total) == "$70.00"


def test_australian_receipt_reconciles_exactly() -> None:
    """Fixture 01: items sum to 39.20, then LOYALTY -15.00 gives 24.20.

    The `SPECIAL` lines are positive and part of the subtotal — treating them
    as discounts yields 35.28 and flags a clean receipt as broken.
    """
    items = [
        "4.66",
        "1.32",
        "0.99",
        "1.50",
        "3.97",
        "4.84",
        "5.15",
        "0.99",
        "7.03",
        "3.27",
        "2.99",
        "2.49",
    ]
    subtotal = sum(parse_money_to_cents(p) for p in items)
    assert subtotal == 3920

    assert subtotal + parse_money_to_cents("-15.00") == parse_money_to_cents("$24.20")


def test_costco_receipt_reconciles_exactly() -> None:
    """Fixture 04: subtotal 85.61 + tax 3.52 = 89.13."""
    items = [
        "23.99",
        "6.49",
        "2.97",
        "12.87",
        "6.29",
        "6.49",
        "18.47",
        "3.59",
        "4.45",
    ]
    subtotal = sum(parse_money_to_cents(p) for p in items)
    assert subtotal == 8561
    assert subtotal + parse_money_to_cents("3.52") == parse_money_to_cents("89.13")


def test_sprouts_receipt_reconciles_exactly() -> None:
    """Fixture 05: 3.99 + 0.05 + 3.00 + 3.99 + 0.05 = 11.08."""
    lines = ["3.99", "0.05", "3.00", "3.99", "0.05"]
    assert sum(parse_money_to_cents(p) for p in lines) == parse_money_to_cents("11.08")


def test_formatting_shows_currency_and_sign() -> None:
    assert format_cents(2420) == "$24.20"
    assert format_cents(-1500) == "-$15.00"
    assert format_cents(33816, "ZAR") == "R338.16"
    assert format_cents(5) == "$0.05"


def test_decimal_conversion_is_exact() -> None:
    assert str(cents_to_decimal(2420)) == "24.20"
    assert str(cents_to_decimal(5)) == "0.05"
