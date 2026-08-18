"""Unit parsing and conversion to grams. Pure — no I/O, no database.

This is one of the three places the brief calls out as where silent wrongness
lives, so the conversions are exact rationals rather than rounded constants,
and anything that cannot be converted honestly returns None instead of a
plausible number.

Volume is the important asymmetry: converting millilitres to grams requires
the density of the specific food. Without it, the line stays unresolved. The
brief is explicit that assuming the density of water is not acceptable, and a
50 ml bottle of oil weighing 46 g rather than 50 g is exactly the kind of
quiet 8% error this project exists to avoid.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class UnitKind(StrEnum):
    MASS = "mass"
    VOLUME = "volume"
    COUNT = "count"


# Exact conversions. The pound is defined as exactly 0.45359237 kg, and the
# US fluid ounce as exactly 29.5735295625 ml, so these are not approximations.
_MASS_TO_GRAMS: dict[str, Decimal] = {
    "g": Decimal(1),
    "gr": Decimal(1),
    "gram": Decimal(1),
    "grams": Decimal(1),
    "kg": Decimal(1000),
    "kilo": Decimal(1000),
    "kilos": Decimal(1000),
    "kilogram": Decimal(1000),
    "kilograms": Decimal(1000),
    "mg": Decimal("0.001"),
    "lb": Decimal("453.59237"),
    "lbs": Decimal("453.59237"),
    "pound": Decimal("453.59237"),
    "pounds": Decimal("453.59237"),
    "#": Decimal("453.59237"),  # Costco prints "MONT JACK 2#"
    "oz": Decimal("28.349523125"),
    "ounce": Decimal("28.349523125"),
    "ounces": Decimal("28.349523125"),
}

_VOLUME_TO_ML: dict[str, Decimal] = {
    "ml": Decimal(1),
    "mls": Decimal(1),
    "millilitre": Decimal(1),
    "milliliter": Decimal(1),
    "cl": Decimal(10),
    "dl": Decimal(100),
    "l": Decimal(1000),
    "lt": Decimal(1000),
    "ltr": Decimal(1000),
    "litre": Decimal(1000),
    "liter": Decimal(1000),
    "litres": Decimal(1000),
    "liters": Decimal(1000),
    "floz": Decimal("29.5735295625"),
    "fl oz": Decimal("29.5735295625"),
    "pt": Decimal("473.176473"),
    "pint": Decimal("473.176473"),
    "qt": Decimal("946.352946"),
    "quart": Decimal("946.352946"),
    "gal": Decimal("3785.411784"),
    "gallon": Decimal("3785.411784"),
    "cup": Decimal("236.5882365"),
    "cups": Decimal("236.5882365"),
}

# Units that mean "one item", carrying no measurement.
_COUNT_UNITS = {"ea", "each", "ct", "count", "pk", "pack", "s", "1s", "'s", "x"}

_GRAMS_PRECISION = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class Quantity:
    """A parsed magnitude and unit, e.g. 0.778 kg."""

    value: Decimal
    unit: str  # canonical lowercase form as written
    kind: UnitKind

    @property
    def is_convertible_without_density(self) -> bool:
        return self.kind is UnitKind.MASS


def classify_unit(unit: str) -> UnitKind | None:
    """What sort of unit this is, or None if unrecognised."""
    key = unit.strip().lower().rstrip(".")
    if key in _MASS_TO_GRAMS:
        return UnitKind.MASS
    if key in _VOLUME_TO_ML:
        return UnitKind.VOLUME
    if key in _COUNT_UNITS:
        return UnitKind.COUNT
    return None


# A number followed by a unit: "0.778kg", "1.08 lb", "2#", "500GR", "1 KG".
_QUANTITY = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>#|[a-zA-Z]{1,10}(?:\s?oz)?)",
    re.IGNORECASE,
)


def parse_quantity(text: str | None) -> Quantity | None:
    """Parse a magnitude with a unit, e.g. '0.778 kg' or '2#'.

    Returns None rather than guessing when the text has no unit, an
    unrecognised unit, or no parseable number.
    """
    if not text:
        return None

    cleaned = str(text).strip()
    match = _QUANTITY.search(cleaned)
    if match is None:
        return None

    # `search` would happily find "5 kg" inside "-5 kg". A negative weight is
    # nonsense on a receipt, so refuse it rather than silently dropping the sign.
    preceding = cleaned[: match.start("value")].rstrip()
    if preceding.endswith("-"):
        return None

    raw_unit = match.group("unit").strip().lower().rstrip(".")
    kind = classify_unit(raw_unit)
    if kind is None:
        return None

    try:
        value = Decimal(match.group("value").replace(",", "."))
    except InvalidOperation:
        return None

    if value <= 0:
        return None

    return Quantity(value=value, unit=raw_unit, kind=kind)


def to_grams(quantity: Quantity, *, density_g_per_ml: Decimal | None = None) -> Decimal | None:
    """Convert to grams, or None when the conversion cannot be made honestly.

    Volume requires a food-specific density. Without one this returns None and
    the line stays unresolved, rather than silently assuming water.
    """
    if quantity.kind is UnitKind.MASS:
        grams = quantity.value * _MASS_TO_GRAMS[quantity.unit]
        return grams.quantize(_GRAMS_PRECISION)

    if quantity.kind is UnitKind.VOLUME:
        if density_g_per_ml is None or density_g_per_ml <= 0:
            return None
        millilitres = quantity.value * _VOLUME_TO_ML[quantity.unit]
        return (millilitres * density_g_per_ml).quantize(_GRAMS_PRECISION)

    # A count is not a measurement. How much a "1S" cucumber weighs is a
    # question about cucumbers, answered by a correction or the resolver.
    return None


# Package sizes printed inside an item name: "MILKY BAR CHOC 80GR",
# "SPAR COOKING OIL 375ML", "MEDITERRANEAN MIX 1KG". SPAR does this on most
# lines, which makes roughly half that basket resolvable to exact grams from
# the text alone, with no model estimate involved.
_PACKAGE_SIZE = re.compile(
    r"(?<![\d.])(?P<value>\d+(?:[.,]\d+)?)\s?(?P<unit>kg|kgs|g|gr|gram|grams|mg|"
    r"ml|mls|cl|dl|l|lt|ltr|litre|liter|oz|lb|lbs|#)(?![a-z])",
    re.IGNORECASE,
)


def extract_package_size(text: str) -> Quantity | None:
    """Find a package size stated inside an item description.

    Returns the last match: descriptions put the size at the end
    ("TASTIC RICE 500GR"), and a leading number is more often a count or a
    brand fragment than a size.
    """
    if not text:
        return None

    matches = list(_PACKAGE_SIZE.finditer(text))
    if not matches:
        return None

    match = matches[-1]
    raw_unit = match.group("unit").lower()
    kind = classify_unit(raw_unit)
    if kind is None:  # pragma: no cover - pattern only admits known units
        return None

    try:
        value = Decimal(match.group("value").replace(",", "."))
    except InvalidOperation:  # pragma: no cover - pattern guarantees a number
        return None

    if value <= 0:
        return None

    return Quantity(value=value, unit=raw_unit, kind=kind)
