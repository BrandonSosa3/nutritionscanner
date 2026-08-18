"""Choosing which FoodData Central entry a canonical name refers to.

Scored against the real search payload for `chicken breast, boneless
skinless, raw`, which is a better adversarial set than anything invented:
among its ten hits are three cooked variants of the right food, a fried
coated one, a chicken *thigh*, and the breast meat of a Ruffed Grouse and a
Canada Goose.
"""

import json
from typing import Any

import pytest

from ns.providers.usda.matching import (
    MIN_RECALL,
    Candidate,
    best_match,
    rank_candidates,
    score_candidate,
    tokenise,
)
from ns.providers.usda.parsing import ParsedFood, ParsedNutrient, parse_food
from tests.conftest import FIXTURES

QUERY = "chicken breast, boneless skinless, raw"


def hits() -> list[ParsedFood]:
    path = FIXTURES / "usda" / "search-chicken-breast.json"
    if not path.is_file():
        pytest.skip("USDA search fixture not present")
    payload: dict[str, Any] = json.loads(path.read_text())
    return [p for f in payload["foods"] if (p := parse_food(f)) is not None]


def synthetic(description: str, *, data_type: str = "SR Legacy", nutrients: int = 1) -> ParsedFood:
    return ParsedFood(
        fdc_id=1,
        description=description,
        data_type=data_type,
        nutrients=tuple(
            ParsedNutrient(code=f"n{i}", amount_per_100g=1, unit="g") for i in range(nutrients)
        ),
    )


def by_id(candidates: list[Candidate]) -> dict[int, Candidate]:
    return {c.food.fdc_id: c for c in candidates}


# ── The real shortlist ────────────────────────────────────────────────────


def test_the_right_food_wins() -> None:
    chosen = best_match(QUERY, hits())
    assert chosen is not None
    assert chosen.food.fdc_id == 2646170
    assert chosen.food.description == "Chicken, breast, boneless, skinless, raw"
    assert chosen.score == 1.0


def test_cooked_variants_are_rejected_outright() -> None:
    """Cooked chicken is denser in everything per 100 g because water left."""
    ranked = by_id(rank_candidates(QUERY, hits()))

    for cooked in (331960, 171140, 171534):
        assert not ranked[cooked].usable
        assert "states disagree" in str(ranked[cooked].rejected_reason)


def test_the_thigh_is_rejected_although_it_clears_the_score_floor() -> None:
    """The case a score threshold cannot catch.

    `Chicken, thigh, boneless, skinless, raw` scores 0.80 — comfortably above
    MIN_SCORE, and only a hair under a genuine breast entry at 0.84. Any cutoff
    low enough to admit real matches admits this one too. What rejects it is
    that it is missing a word the canonical name specified.
    """
    from ns.providers.usda.matching import MIN_SCORE

    ranked = by_id(rank_candidates(QUERY, hits()))
    thigh = ranked[2646171]
    genuine = ranked[171509]  # a real breast entry, and the weakest usable one

    assert thigh.score > MIN_SCORE
    assert abs(thigh.score - genuine.score) < 0.05
    assert genuine.usable
    assert not thigh.usable
    assert thigh.rejected_reason == "does not mention breast"


def test_other_birds_are_rejected() -> None:
    ranked = by_id(rank_candidates(QUERY, hits()))

    for bird in (172831, 173634):  # Ruffed Grouse, Canada Goose
        assert not ranked[bird].usable
        assert "chicken" in str(ranked[bird].rejected_reason)


def test_a_breaded_fried_variant_is_rejected() -> None:
    ranked = by_id(rank_candidates(QUERY, hits()))
    assert not ranked[2705975].usable


def test_only_genuine_matches_survive() -> None:
    usable = [c.food.fdc_id for c in rank_candidates(QUERY, hits()) if c.usable]
    assert usable == [2646170, 171077, 171509]


def test_rejected_candidates_are_still_reported() -> None:
    """Kept so a match that looks wrong later is explainable, and so the
    review screen can offer them."""
    ranked = rank_candidates(QUERY, hits())
    assert len(ranked) == 10
    assert all(c.rejected_reason or c.usable for c in ranked)


# ── The rules, in isolation ───────────────────────────────────────────────


def test_every_term_of_the_name_must_appear() -> None:
    assert MIN_RECALL == 1.0
    assert not score_candidate("tomatoes, diced, canned", synthetic("Tomatoes, canned")).usable


def test_a_candidate_containing_every_term_is_usable() -> None:
    candidate = score_candidate(
        "tomatoes, diced, canned", synthetic("Tomatoes, red, ripe, canned, diced")
    )
    assert candidate.usable
    assert candidate.recall == 1.0


def test_extra_words_in_the_candidate_cost_precision_but_do_not_reject() -> None:
    """USDA descriptions are verbose; that alone is not disqualifying."""
    candidate = score_candidate(
        "tomatoes, canned", synthetic("Tomatoes, red, ripe, canned, packed in tomato juice")
    )
    assert candidate.usable
    assert candidate.precision < 0.5


def test_a_silent_candidate_does_not_conflict_on_state() -> None:
    """USDA often omits `raw` on foods that are raw. Silence is not a
    contradiction — only a stated disagreement counts."""
    candidate = score_candidate("onions, chopped, raw", synthetic("Onions, chopped"))
    assert candidate.rejected_reason == "does not mention raw"  # recall, not state


def test_compatible_states_do_not_conflict() -> None:
    """`canned` describes a pack, not a cooking method."""
    candidate = score_candidate("tomatoes, canned", synthetic("Tomatoes, canned, cooked"))
    assert candidate.rejected_reason is None


def test_dried_and_dehydrated_are_the_same_state() -> None:
    candidate = score_candidate("onions, dried", synthetic("Onions, dehydrated, dried"))
    assert candidate.rejected_reason is None


def test_a_candidate_with_no_nutrients_is_not_a_match() -> None:
    """It would claim the food is known while contributing nothing."""
    candidate = score_candidate("beans, black, raw", synthetic("Beans, black, raw", nutrients=0))
    assert candidate.rejected_reason == "candidate carries no nutrient data"


def test_foundation_data_is_preferred_over_sr_legacy() -> None:
    """Foundation is analytically measured and current.

    A tiebreaker rather than a score bonus, because as a bonus it vanished in
    the one case it exists for: two candidates matching the text perfectly
    both score 1.0, and the clamp swallowed the difference.
    """
    foundation = synthetic("Beans, black", data_type="Foundation")
    legacy = synthetic("Beans, black", data_type="SR Legacy")

    assert score_candidate("beans, black", foundation).score == 1.0
    assert score_candidate("beans, black", legacy).score == 1.0

    ranked = rank_candidates("beans, black", [legacy, foundation])
    assert ranked[0].food.data_type == "Foundation"


def test_nothing_matches_when_nothing_is_good_enough() -> None:
    assert best_match("cheese, monterey jack", [synthetic("Cheese, cheddar")]) is None
    assert best_match("anything", []) is None


def test_tokenising_drops_punctuation_and_stopwords() -> None:
    assert tokenise("Chicken, breast (boneless and skinless), raw") == {
        "chicken",
        "breast",
        "boneless",
        "skinless",
        "raw",
    }
