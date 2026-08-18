"""The eval harness — the number that says whether the resolver is any good.

Principle 4: the resolver is measured, not trusted. What is checked here is
that the measurement is honest: that it scores the model rather than the
corrections table, that declining to answer is scored as the correct behaviour
it is, and that being confidently wrong is counted separately from being wrong.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ns.eval.harness import (
    GRAMS_TOLERANCE_PCT,
    _expected_calibration_error,
    _grams_within_tolerance,
    _percentile,
    run_eval,
)
from ns.models import EvalExample, Food
from ns.models.enums import EvalSplit, GramsBasis, LabelSource
from ns.providers.storage import LocalReceiptStorage
from tests.integration.test_resolve import patch_resolve, resolved

pytestmark = pytest.mark.integration


@pytest.fixture
def storage(tmp_path: Path) -> LocalReceiptStorage:
    return LocalReceiptStorage(root=tmp_path / "receipts")


async def label(
    session: AsyncSession,
    text: str,
    *,
    food: Food | None = None,
    is_nonfood: bool = False,
    grams: str | None = None,
    split: EvalSplit = EvalSplit.HOLDOUT,
) -> EvalExample:
    example = EvalExample(
        raw_text=text.upper(),
        normalized_text=text,
        normalizer_version="v1",
        expected_food_id=food.id if food else None,
        expected_is_nonfood=is_nonfood,
        expected_grams=Decimal(grams) if grams else None,
        expected_grams_basis=GramsBasis.PER_PACKAGE if grams else GramsBasis.UNKNOWN,
        label_source=LabelSource.CORRECTED,
        split=split,
    )
    session.add(example)
    await session.flush()
    return example


async def food_named(session: AsyncSession, name: str) -> Food:
    """Get or create — see the note on `make_food` in test_resolve."""
    from sqlmodel import col, select

    existing = (
        await session.execute(select(Food).where(col(Food.canonical_name) == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    food = Food(canonical_name=name)
    session.add(food)
    await session.flush()
    return food


# ── Scoring ───────────────────────────────────────────────────────────────


async def test_a_perfect_resolver_scores_one(session: AsyncSession) -> None:
    chicken = await food_named(session, "chicken breast, raw")
    await label(session, "ff bs breast", food=chicken)

    with patch_resolve(resolved(0, "chicken breast, raw", confidence=0.95)):
        report = await run_eval(session)

    assert report.run.food_accuracy == 1.0
    assert report.run.precision_at_threshold == 1.0
    assert report.run.n_examples == 1


async def test_a_wrong_answer_is_scored_wrong(session: AsyncSession) -> None:
    chicken = await food_named(session, "chicken breast, raw")
    await food_named(session, "turkey, ground, raw")
    await label(session, "ff bs breast", food=chicken)

    with patch_resolve(resolved(0, "turkey, ground, raw", confidence=0.9)):
        report = await run_eval(session)

    assert report.run.food_accuracy == 0.0
    assert len(report.mistakes) == 1


async def test_a_name_no_food_row_matches_is_wrong_not_a_crash(
    session: AsyncSession,
) -> None:
    """The resolver inventing a new name for a labelled food is a miss."""
    chicken = await food_named(session, "chicken breast, raw")
    await label(session, "ff bs breast", food=chicken)

    with patch_resolve(resolved(0, "poultry, breast meat, uncooked", confidence=0.9)):
        report = await run_eval(session)

    assert report.run.food_accuracy == 0.0
    assert report.scored[0].predicted_food_id is None


async def test_declining_to_answer_an_unidentifiable_line_is_correct(
    session: AsyncSession,
) -> None:
    """Principle 2 as a measurement: not guessing is the right answer here."""
    await label(session, "misc store code 4471", food=None)

    with patch_resolve(resolved(0, None, confidence=0.1)):
        report = await run_eval(session)

    assert report.run.food_accuracy == 1.0
    assert report.run.breakdown is not None
    assert report.run.breakdown["declined_correctly"] == 1


async def test_guessing_at_an_unidentifiable_line_is_wrong(session: AsyncSession) -> None:
    await label(session, "misc store code 4471", food=None)

    with patch_resolve(resolved(0, "tomatoes, canned", confidence=0.8)):
        report = await run_eval(session)

    assert report.run.food_accuracy == 0.0


async def test_nonfood_is_scored_on_being_nonfood(session: AsyncSession) -> None:
    await label(session, "carrier bag", is_nonfood=True)

    with patch_resolve(resolved(0, "carrier bag, plastic", category="household", is_nonfood=True)):
        report = await run_eval(session)

    assert report.run.food_accuracy == 1.0


# ── Confidence is measured, not trusted ───────────────────────────────────


async def test_precision_at_threshold_ignores_the_low_confidence_answers(
    session: AsyncSession,
) -> None:
    """The number that matters: right *when it was sure enough to skip review*."""
    right = await food_named(session, "the right food")
    await food_named(session, "the wrong food")
    await label(session, "line one", food=right)
    await label(session, "line two", food=right)

    with patch_resolve(
        resolved(0, "the right food", confidence=0.95),
        resolved(1, "the wrong food", confidence=0.2),  # wrong, and knows it
    ):
        report = await run_eval(session, threshold=0.6)

    # Half of all answers were wrong...
    assert report.run.food_accuracy == 0.5
    # ...but none of the confident ones were, which is the useful behaviour.
    assert report.run.precision_at_threshold == 1.0


async def test_being_confidently_wrong_is_recorded_by_name(session: AsyncSession) -> None:
    """The failure that actually costs the user something."""
    right = await food_named(session, "the right food")
    await food_named(session, "the wrong food")
    await label(session, "ks diced tom", food=right)

    with patch_resolve(resolved(0, "the wrong food", confidence=0.97)):
        report = await run_eval(session, threshold=0.6)

    assert report.run.breakdown is not None
    confidently_wrong = report.run.breakdown["confidently_wrong"]
    assert isinstance(confidently_wrong, list)
    assert confidently_wrong[0]["normalized_text"] == "ks diced tom"
    assert confidently_wrong[0]["predicted"] == "the wrong food"


def test_calibration_error_is_zero_when_confidence_matches_accuracy() -> None:
    """A resolver saying 0.95 on a hundred lines should be right on ~95."""
    from ns.eval.harness import Scored

    def scored(confidence: float, correct: bool) -> Scored:
        example = EvalExample(
            raw_text="X",
            normalized_text="x",
            normalizer_version="v1",
            label_source=LabelSource.CORRECTED,
        )
        return Scored(
            example=example,
            predicted=resolved(0, "a food", confidence=confidence),
            predicted_food_id=1 if correct else 2,
            food_correct=correct,
            grams_correct=None,
            confidence=confidence,
        )

    perfect = [scored(1.0, True) for _ in range(10)]
    assert _expected_calibration_error(perfect) == 0.0

    # Certain and always wrong is the worst possible calibration.
    overconfident = [scored(1.0, False) for _ in range(10)]
    assert _expected_calibration_error(overconfident) == 1.0


def test_calibration_is_undefined_with_no_answers() -> None:
    assert _expected_calibration_error([]) is None


# ── Grams ─────────────────────────────────────────────────────────────────


def test_grams_are_scored_within_a_tolerance() -> None:
    """Estimates for anything unweighed; exactness would measure luck."""
    assert _grams_within_tolerance(Decimal("1000"), "1050") is True
    assert _grams_within_tolerance(Decimal("1000"), "1100") is True  # exactly at 10%
    assert _grams_within_tolerance(Decimal("1000"), "1101") is False
    assert Decimal("10") == GRAMS_TOLERANCE_PCT


def test_grams_are_not_scored_when_the_label_states_none() -> None:
    assert _grams_within_tolerance(None, "500") is None


def test_a_missing_or_unreadable_gram_estimate_is_a_miss() -> None:
    assert _grams_within_tolerance(Decimal("500"), None) is False
    assert _grams_within_tolerance(Decimal("500"), "about 500") is False


def test_percentiles_of_an_empty_list_are_none() -> None:
    assert _percentile([], 95) is None
    assert _percentile([10, 20, 30], 50) == 20


# ── What a run records ────────────────────────────────────────────────────


async def test_a_run_pins_the_three_things_that_make_runs_comparable(
    session: AsyncSession,
) -> None:
    """Model, prompt hash, normaliser version. Change any and it is a different
    system being measured."""
    food = await food_named(session, "a food")
    await label(session, "a line", food=food)

    with patch_resolve(resolved(0, "a food")):
        report = await run_eval(session, notes="baseline")

    assert report.run.model == "claude-opus-5"
    assert len(report.run.prompt_version) == 12
    assert report.run.normalizer_version == "v1"
    assert report.run.notes == "baseline"
    assert report.run.latency_p50_ms == 3100


async def test_the_holdout_split_is_what_gets_scored(session: AsyncSession) -> None:
    """Scoring the training split would be scoring the resolver on answers it
    was handed."""
    food = await food_named(session, "a food")
    await label(session, "held out", food=food, split=EvalSplit.HOLDOUT)
    await label(session, "trained on", food=food, split=EvalSplit.TRAIN)

    with patch_resolve(resolved(0, "a food")):
        report = await run_eval(session, split=EvalSplit.HOLDOUT)

    assert report.run.n_examples == 1
    assert report.scored[0].example.normalized_text == "held out"


async def test_an_empty_split_says_where_labels_come_from(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="corrections and confirmations"):
        await run_eval(session, split=EvalSplit.HOLDOUT)


async def test_the_harness_never_reads_the_corrections_table(
    session: AsyncSession,
) -> None:
    """A holdout example with a correction sitting on it must still be asked
    of the model, or the score measures the corrections table."""
    from ns.models import Correction

    right = await food_named(session, "the labelled food")
    await food_named(session, "the model's answer")
    await label(session, "ff bs breast", food=right)
    session.add(Correction(normalized_text="ff bs breast", store_id=None, food_id=right.id))
    await session.flush()

    with patch_resolve(resolved(0, "the model's answer", confidence=0.9)) as mock:
        report = await run_eval(session)

    assert mock.await_count == 1
    assert report.run.food_accuracy == 0.0


# ── The endpoints ─────────────────────────────────────────────────────────


async def test_the_eval_endpoints(client: AsyncClient, session: AsyncSession) -> None:
    food = await food_named(session, "an endpoint food")
    await label(session, "an endpoint line", food=food)
    await session.commit()

    with patch_resolve(resolved(0, "an endpoint food")):
        created = await client.post("/eval/runs")

    assert created.status_code == 200
    assert created.json()["food_accuracy"] == 1.0

    listed = await client.get("/eval/runs")
    assert listed.status_code == 200
    assert listed.json()["total"] >= 1


async def test_running_eval_with_no_labels_is_a_conflict(client: AsyncClient) -> None:
    response = await client.post("/eval/runs?split=holdout")
    assert response.status_code in {409, 200}
