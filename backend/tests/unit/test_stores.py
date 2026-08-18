"""Store name normalisation and branch identification. Pure.

A Store is a branch, not a chain: prices differ between branches, and the
Phase 2 cross-store comparison is meaningless if two collapse into one row.
"""

import pytest

from ns.pipeline.stores import alias_key, branch_number, normalise_store_name


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("COSTCO WHOLESALE", "costco wholesale"),
        ("Costco Wholesale", "costco wholesale"),
        ("COSTCO-WHOLESALE", "costco wholesale"),
        ("  Costco   Wholesale  ", "costco wholesale"),
        ("SPAR BERGVILLE", "spar bergville"),
        ("Sprouts Farmers Market", "sprouts farmers market"),
        ("Whole Foods Market", "whole foods market"),
    ],
)
def test_punctuation_and_case_do_not_fork_a_store(printed: str, expected: str) -> None:
    assert normalise_store_name(printed) == expected


def test_corporate_suffixes_are_dropped() -> None:
    assert normalise_store_name("Example Grocers Inc.") == "example grocers"
    assert normalise_store_name("Example Grocers Pty Ltd") == "example grocers"


def test_brand_words_that_look_like_suffixes_are_kept() -> None:
    """ "Wholesale" is part of the Costco brand; "Market" part of Sprouts."""
    assert "wholesale" in normalise_store_name("COSTCO WHOLESALE")
    assert "market" in normalise_store_name("Sprouts Farmers Market")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Thornton #629", "629"),
        ("#629", "629"),
        ("Whse:629", "629"),
        ("Store 1204", "1204"),
        ("No. 88", "88"),
        ("Bergville", None),
        ("Sharon Rd", None),
        (None, None),
    ],
)
def test_branch_numbers_are_found_however_they_are_printed(
    text: str | None, expected: str | None
) -> None:
    assert branch_number(text) == expected


def test_branch_number_is_searched_across_the_whole_header() -> None:
    """Costco puts it in the location line; other stores put it in the name."""
    assert branch_number(None, "COSTCO WHOLESALE #629") == "629"
    assert branch_number("Thornton #629", "COSTCO WHOLESALE") == "629"


def test_a_street_address_is_not_read_as_a_branch_number() -> None:
    """`3150 Sharon Rd` would otherwise become branch 3150."""
    assert branch_number("3150 Sharon Rd") is None


def test_the_alias_key_combines_name_and_location() -> None:
    assert alias_key("COSTCO WHOLESALE", "Thornton #629") == "costco wholesale | thornton 629"


def test_two_printings_of_one_header_share_an_alias_key() -> None:
    assert alias_key("COSTCO WHOLESALE", "Thornton, #629") == alias_key(
        "Costco  Wholesale", "Thornton #629"
    )


def test_two_branches_of_one_chain_do_not_share_an_alias_key() -> None:
    assert alias_key("COSTCO WHOLESALE", "Thornton #629") != alias_key(
        "COSTCO WHOLESALE", "San Diego #452"
    )


def test_a_store_with_no_location_still_has_a_key() -> None:
    assert alias_key("SPAR", None) == "spar"


def test_the_alias_key_fits_the_column() -> None:
    assert len(alias_key("X" * 300, "Y" * 300)) <= 200
