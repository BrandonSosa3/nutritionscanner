"""Reading FoodData Central payloads.

The payloads are real API responses from 2026-08-18 — see the provenance table
in `tests/fixtures/usda/README.md`, which notes that one of the two was
reconstructed by hand after DEMO_KEY hit its rate limit. Synthetic fixtures
would test the shape I believed the API has, which is exactly the belief that
needs checking.
"""

import json
from decimal import Decimal

import pytest

from ns.domain.nutrition import BY_CODE, NUTRIENTS, canonical_unit, nutrient_for_usda_id
from ns.providers.usda.parsing import parse_food, parse_nutrients
from tests.conftest import FIXTURES

USDA_FIXTURES = FIXTURES / "usda"


def load(name: str) -> dict:
    path = USDA_FIXTURES / f"{name}.json"
    if not path.is_file():
        pytest.skip(f"USDA fixture {name} not present")
    return json.loads(path.read_text())


# ── The vocabulary ────────────────────────────────────────────────────────


def test_nutrient_codes_are_unique() -> None:
    assert len(BY_CODE) == len(NUTRIENTS)


def test_energy_falls_back_through_the_atwater_factors() -> None:
    """SR Legacy publishes 1008; Foundation foods often publish only 2047/2048.
    A lookup for 1008 alone returns nothing for half the database."""
    assert BY_CODE["energy_kcal"].usda_ids == (1008, 2048, 2047)
    assert nutrient_for_usda_id(1008) == ("energy_kcal", 0)
    assert nutrient_for_usda_id(2048) == ("energy_kcal", 1)
    assert nutrient_for_usda_id(2047) == ("energy_kcal", 2)


def test_folate_is_the_dfe_form() -> None:
    """1177 is `Folate, total`; 1190 is `Folate, DFE`, which is the form
    dietary reference intakes are stated in."""
    assert BY_CODE["folate_dfe_ug"].usda_ids == (1190,)
    assert nutrient_for_usda_id(1177) is None


def test_untracked_nutrients_map_to_nothing() -> None:
    assert nutrient_for_usda_id(1057) is None  # caffeine
    assert nutrient_for_usda_id(999999) is None


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("g", "g"),
        ("G", "g"),
        ("mg", "mg"),
        ("MG", "mg"),
        ("µg", "ug"),
        ("UG", "ug"),
        ("kcal", "kcal"),
        ("KCAL", "kcal"),
        ("IU", "IU"),
    ],
)
def test_units_are_normalised_across_both_endpoints(printed: str, expected: str) -> None:
    """Detail returns `µg`; search returns `UG`. Both are the same unit."""
    assert canonical_unit(printed) == expected


def test_an_unreadable_unit_is_none_not_a_guess() -> None:
    assert canonical_unit("furlongs") is None


# ── Real payloads ─────────────────────────────────────────────────────────


def test_the_detail_shape_is_parsed() -> None:
    """Detail nests: {"nutrient": {"id": ...}, "amount": ...}."""
    food = parse_food(load("food-2646170-chicken-breast"))

    assert food is not None
    assert food.fdc_id == 2646170
    assert food.description == "Chicken, breast, boneless, skinless, raw"
    assert food.data_type == "Foundation"
    assert food.usda_category == "Poultry Products"

    by_code = {n.code: n for n in food.nutrients}
    assert by_code["protein_g"].amount_per_100g == Decimal("22.525")
    assert by_code["protein_g"].unit == "g"
    assert by_code["sodium_mg"].unit == "mg"


def test_group_headings_are_not_read_as_zero() -> None:
    """`Proximates`, `Lipids`, `Minerals` and `Carbohydrates` arrive as
    nutrients with no `amount` key at all."""
    payload = load("food-2646170-chicken-breast")
    assert sum(1 for e in payload["foodNutrients"] if "amount" not in e) == 4

    food = parse_food(payload)
    assert food is not None
    codes = {n.code for n in food.nutrients}
    assert "carbohydrate_g" in codes  # the real one, id 1005, amount 0
    by_code = {n.code: n for n in food.nutrients}
    assert by_code["carbohydrate_g"].amount_per_100g == Decimal("0")


def test_a_foundation_food_gets_energy_from_the_atwater_factors() -> None:
    """This food publishes no nutrient 1008 at all."""
    payload = load("food-2646170-chicken-breast")
    ids = {e.get("nutrient", {}).get("id") for e in payload["foodNutrients"]}
    assert 1008 not in ids
    assert 2048 in ids

    food = parse_food(payload)
    assert food is not None
    by_code = {n.code: n for n in food.nutrients}
    # 2048 (specific factors) is preferred over 2047 (general).
    assert by_code["energy_kcal"].amount_per_100g == Decimal("112.20227")
    assert by_code["energy_kcal"].unit == "kcal"


def test_an_sr_legacy_food_uses_nutrient_1008() -> None:
    """The complement of the test above: SR Legacy does publish 1008, and its
    129-nutrient list is where folate and selenium actually appear."""
    hit = next(f for f in load("search-chicken-breast")["foods"] if f["fdcId"] == 171077)
    food = parse_food(hit)

    assert food is not None
    by_code = {n.code: n for n in food.nutrients}
    assert by_code["energy_kcal"].amount_per_100g == Decimal("120")
    assert by_code["folate_dfe_ug"].amount_per_100g == Decimal("9.0")
    # Uppercase `UG` from the search endpoint, canonicalised.
    assert by_code["selenium_ug"].unit == "ug"


def test_the_search_shape_is_parsed() -> None:
    """Search flattens: {"nutrientId": ..., "unitName": "MG", "value": ...}."""
    payload = load("search-chicken-breast")
    hit = next(f for f in payload["foods"] if f["fdcId"] == 2646170)

    food = parse_food(hit)

    assert food is not None
    by_code = {n.code: n for n in food.nutrients}
    assert by_code["protein_g"].amount_per_100g == Decimal("22.5")
    assert by_code["iron_mg"].unit == "mg"


def test_search_and_detail_agree_on_the_nutrients_we_track() -> None:
    """Recorded because it corrects something I had assumed.

    I had written that search returns an abbreviated nutrient list and that
    fetching the detail was needed to recover the vitamins. For this food the
    two agree exactly. The detail call is still made — it is the authoritative
    record and carries `foodPortions` and `foodCategory`, which search omits —
    but not for the reason originally claimed.
    """
    hit = next(f for f in load("search-chicken-breast")["foods"] if f["fdcId"] == 2646170)
    from_search = parse_food(hit)
    from_detail = parse_food(load("food-2646170-chicken-breast"))

    assert from_search is not None and from_detail is not None
    assert {n.code for n in from_search.nutrients} == {n.code for n in from_detail.nutrients}


def test_an_unusable_payload_is_none_not_an_empty_food() -> None:
    assert parse_food({}) is None
    assert parse_food({"fdcId": "not an int", "description": "x"}) is None
    assert parse_food({"fdcId": 1, "description": "   "}) is None


def test_nutrients_with_no_entries_parse_to_nothing() -> None:
    assert parse_nutrients([]) == ()


def test_a_negative_amount_is_refused() -> None:
    entries = [{"nutrient": {"id": 1003, "unitName": "g"}, "amount": -5}]
    assert parse_nutrients(entries) == ()


def test_an_unreadable_unit_drops_the_value_rather_than_guessing() -> None:
    entries = [{"nutrient": {"id": 1003, "unitName": "furlongs"}, "amount": 22}]
    assert parse_nutrients(entries) == ()


def test_generic_data_types_are_recognised() -> None:
    """Foundation and SR Legacy carry generic ingredients with refuse
    percentages; Branded carries one manufacturer's product."""
    detail = parse_food(load("food-2646170-chicken-breast"))
    assert detail is not None
    assert detail.is_generic
