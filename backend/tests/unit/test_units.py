"""Unit conversion — one of the three places the brief flags for silent wrongness."""

from decimal import Decimal

import pytest

from ns.domain.units import (
    Quantity,
    UnitKind,
    classify_unit,
    extract_package_size,
    parse_quantity,
    to_grams,
)


def grams(text: str, density: Decimal | None = None) -> Decimal | None:
    quantity = parse_quantity(text)
    assert quantity is not None, f"failed to parse {text!r}"
    return to_grams(quantity, density_g_per_ml=density)


# ── Exact conversions ─────────────────────────────────────────────────────


def test_pound_uses_the_exact_definition() -> None:
    """A pound is exactly 0.45359237 kg. Rounding to 454 g is a 0.09% error
    that compounds across every weighed item in a basket."""
    assert grams("1 lb") == Decimal("453.592")


def test_ounce_is_one_sixteenth_of_a_pound() -> None:
    assert grams("16 oz") == grams("1 lb")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 kg", "1000.000"),
        ("0.778 kg", "778.000"),  # fixture 01, zucchini
        ("0.596kg", "596.000"),  # fixture 03, bananas
        ("500 g", "500.000"),
        ("500GR", "500.000"),  # fixture 03, rice
        ("1.08 lb", "489.880"),  # fixture 02, plums
        ("1.64 lb", "743.891"),  # fixture 02, gala apples
        ("2#", "907.185"),  # fixture 04, monterey jack
        ("250mg", "0.250"),
    ],
)
def test_mass_conversions(text: str, expected: str) -> None:
    assert grams(text) == Decimal(expected)


def test_case_and_spacing_do_not_matter() -> None:
    assert grams("0.778KG") == grams("0.778 kg") == grams("  0.778  Kg ")


def test_comma_decimal_separator_is_accepted() -> None:
    """Receipts from comma-decimal locales."""
    assert grams("0,778 kg") == Decimal("778.000")


# ── Volume needs density, and says so ─────────────────────────────────────


def test_volume_without_density_returns_none() -> None:
    """The brief is explicit: fall back to unresolved rather than assuming
    water. A 375ml bottle of oil is 345g, not 375g."""
    assert grams("375 ml") is None


def test_volume_with_density_converts() -> None:
    oil = Decimal("0.92")
    assert grams("375 ml", oil) == Decimal("345.000")


def test_water_density_is_not_assumed_silently() -> None:
    """Same volume, different foods, different masses — the reason density
    must be supplied rather than defaulted."""
    assert grams("1 l", Decimal("1.03")) == Decimal("1030.000")  # milk
    assert grams("1 l", Decimal("0.92")) == Decimal("920.000")  # oil


def test_zero_or_negative_density_is_refused() -> None:
    assert grams("500 ml", Decimal("0")) is None
    assert grams("500 ml", Decimal("-1")) is None


def test_us_fluid_measures_use_exact_definitions() -> None:
    water = Decimal("1")
    assert grams("1 qt", water) == Decimal("946.353")
    assert grams("1 gal", water) == Decimal("3785.412")
    assert grams("1 cup", water) == Decimal("236.588")


# ── Counts are not measurements ───────────────────────────────────────────


def test_count_units_do_not_convert_to_grams() -> None:
    """How much one cucumber weighs is a question about cucumbers, answered
    by a correction — not by unit conversion."""
    quantity = parse_quantity("1 EA")
    assert quantity is not None
    assert quantity.kind is UnitKind.COUNT
    assert to_grams(quantity) is None


def test_count_classification() -> None:
    assert classify_unit("ea") is UnitKind.COUNT
    assert classify_unit("ct") is UnitKind.COUNT
    assert classify_unit("kg") is UnitKind.MASS
    assert classify_unit("ml") is UnitKind.VOLUME
    assert classify_unit("frobnicate") is None


# ── Refusing rather than guessing ─────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["", "   ", "BROCCOLI", "1.08", "abc kg", "0 kg", "-5 kg", "1 furlong"],
)
def test_unparseable_input_returns_none(text: str) -> None:
    assert parse_quantity(text) is None


# ── Package sizes printed in the item name ────────────────────────────────


@pytest.mark.parametrize(
    ("item_text", "expected_grams"),
    [
        ("LAZENBY WORCESTER SAUCE 125ML", None),  # volume, needs density
        ("MILKY BAR CHOC 80GR", "80.000"),
        ("SMOKED VIENNAS 500GR", "500.000"),
        ("PEALED PEACHES 400G", "400.000"),
        ("MEDITERRANEAN MIX 1KG", "1000.000"),
        ("TASTIC RICE 500GR", "500.000"),
        ("BLACK CAT SMOOTH 270GR", "270.000"),
        ("MONT JACK 2#", "907.185"),
    ],
)
def test_package_sizes_from_real_spar_and_costco_lines(
    item_text: str, expected_grams: str | None
) -> None:
    """SPAR prints the pack size inline on most lines, which resolves roughly
    half that basket to exact grams with no model estimate involved."""
    quantity = extract_package_size(item_text)
    assert quantity is not None, item_text
    result = to_grams(quantity)
    assert result == (Decimal(expected_grams) if expected_grams else None)


def test_package_size_takes_the_trailing_measurement() -> None:
    """Descriptions put the size at the end; a leading number is usually a
    count or brand fragment."""
    quantity = extract_package_size("2 PACK COFFEE 250G")
    assert quantity is not None
    assert quantity.value == Decimal("250")


def test_no_package_size_returns_none() -> None:
    assert extract_package_size("GRAPE TOMATO") is None
    assert extract_package_size("FF BS BREAST") is None
    assert extract_package_size("") is None


def test_bin_code_is_not_mistaken_for_a_package_size() -> None:
    """Fixture 03 prints `BANANAS LOOSE 17KG`, where 17KG is a bin code and
    the real weight is 0.596kg on the continuation line.

    Extraction cannot know this, so the text parses as a size. Normalisation
    resolves it by preferring a stated weight when one exists — asserted here
    so the precedence rule is not lost.
    """
    from_text = extract_package_size("BANANAS LOOSE 17KG")
    stated = parse_quantity("0.596 kg")
    assert from_text is not None and stated is not None
    assert to_grams(from_text) == Decimal("17000.000")
    assert to_grams(stated) == Decimal("596.000")


def test_quantity_reports_convertibility() -> None:
    mass = parse_quantity("1 kg")
    volume = parse_quantity("1 l")
    assert mass is not None and volume is not None
    assert mass.is_convertible_without_density is True
    assert volume.is_convertible_without_density is False


def test_quantity_is_immutable() -> None:
    quantity = Quantity(value=Decimal("1"), unit="kg", kind=UnitKind.MASS)
    with pytest.raises(AttributeError):
        quantity.value = Decimal("2")  # type: ignore[misc]
