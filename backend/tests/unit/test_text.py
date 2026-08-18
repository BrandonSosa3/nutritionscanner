"""Receipt text normalisation, exercised on lines from the real fixtures."""

import pytest

from ns.domain.text import NORMALIZER_VERSION, looks_like_noise, normalise


@pytest.mark.parametrize(
    ("raw", "expected", "why"),
    [
        ("E FF BS BREAST", "ff bs breast", "Costco leading tax flag"),
        ("673919 FF BS BREAST", "ff bs breast", "Costco leading SKU"),
        ("PL TORTILLA'S B", "pl tortilla's", "Whole Foods trailing tax flag"),
        ("MONT JACK 2#", "mont jack", "pound notation in the name"),
        ("SPAR COOKING OIL 375ML", "spar cooking oil", "SPAR inline pack size"),
        ("MILKY BAR CHOC 80GR", "milky bar choc", "SPAR inline pack size"),
        ("BANANAS LOOSE 17KG", "bananas loose", "SPAR bin code"),
        ("ZUCHINNI GREEN", "zuchinni green", "misspelling preserved"),
        ("PEALED PEACHES 400G", "pealed peaches", "misspelling preserved"),
        ("  LETTUCE   ICEBERG  ", "lettuce iceberg", "whitespace collapsed"),
    ],
)
def test_normalises_real_receipt_lines(raw: str, expected: str, why: str) -> None:
    assert normalise(raw) == expected, why


def test_quality_markers_survive() -> None:
    """`OG` means organic, which maps to a different USDA food than the
    conventional variant. Stripping it would collapse two distinct foods onto
    one correction key — and a correction is permanent."""
    assert "og" in normalise("OG LF COTTAGE CHEE")
    assert "og" in normalise("GALA APPLES OG")


def test_store_brand_prefixes_survive() -> None:
    """`KS` is Kirkland Signature. The product may genuinely differ from a
    generic equivalent, so the resolver decides — not the normaliser."""
    assert normalise("KS DICED TOM") == "ks diced tom"


def test_unit_price_fragments_are_stripped() -> None:
    """The brief names these explicitly."""
    assert normalise("BROCCOLI @ 0.69/LB") == "broccoli"
    assert normalise("GRAPES GREEN @ $5.99/kg") == "grapes green"


def test_tare_lines_are_stripped() -> None:
    """Packaging weight, never the item weight."""
    assert normalise("1.08 lb @ 1.99 /lb  TARE = .01") == ""


def test_pure_measurement_lines_normalise_to_nothing() -> None:
    """A weight continuation line carries no food identity of its own."""
    assert normalise("0.778kg NET @ $5.99/kg") == ""
    assert normalise("3 @ 4.29") == ""


def test_same_item_at_different_sizes_shares_one_key() -> None:
    """The food is the same; the amount differs and is carried in grams.
    Otherwise every package size needs its own correction."""
    assert normalise("SPAR COOKING OIL 375ML") == normalise("SPAR COOKING OIL 750ML")


def test_case_and_flags_do_not_split_the_key() -> None:
    """The same product printed with and without a tax flag must match, or
    corrections silently stop applying."""
    assert normalise("E GRAPE TOMATO") == normalise("GRAPE TOMATO")
    assert normalise("grape tomato") == normalise("GRAPE TOMATO")


def test_empty_and_missing_input() -> None:
    assert normalise("") == ""
    assert normalise("   ") == ""


@pytest.mark.parametrize("text", ["", " ", "x", "42", "3.99", "---"])
def test_noise_is_detected(text: str) -> None:
    """These must never become correction keys: they would match unrelated
    lines on future receipts, permanently."""
    assert looks_like_noise(normalise(text)) is True


@pytest.mark.parametrize("text", ["grape tomato", "ff bs breast", "og lf cottage chee"])
def test_real_items_are_not_noise(text: str) -> None:
    assert looks_like_noise(text) is False


def test_version_is_recorded() -> None:
    """Stored on every LineItem and EvalExample so an accuracy comparison can
    never silently span two definitions of 'normalised'."""
    assert NORMALIZER_VERSION
    assert isinstance(NORMALIZER_VERSION, str)
