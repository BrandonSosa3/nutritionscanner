"""Scoring the resolver against a held-out labeled set.

Ships alongside the resolver, not after it (principle 4). The resolver is the
one component that can be confidently wrong, and without a tracked number
there is no way to tell whether a prompt change helped, hurt, or did nothing.

Three properties this harness is built to have:

**It scores the resolver, not the corrections table.** Holdout examples are
never fed to tier 1. The model is asked the same question it would be asked on
a fresh receipt, with no access to the answer.

**It is reproducible.** The split is deterministic, and every run records the
model, the prompt hash, and the normaliser version — the three things whose
change makes two runs incomparable. Comparing a run against one with a
different prompt version is comparing two different systems.

**It measures being wrong *confidently*, not just being wrong.** Accuracy alone
would let a resolver that is 80% right and 100% sure look identical to one
that is 80% right and knows which 20%. The second is far more useful: it sends
the doubtful lines to the correction queue. So precision at the confidence
floor and calibration error are recorded next to raw accuracy.

Cost and latency are recorded too. A prompt that is 2% more accurate at five
times the cost is a bad trade, and that only shows up if both numbers live in
the same table.
"""

import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, func, select

from ns.config import get_settings
from ns.domain.text import NORMALIZER_VERSION
from ns.logging import get_logger
from ns.models import EvalExample, Food, LineItem, ResolverRun
from ns.models.enums import EvalSplit, LineItemKind
from ns.pipeline.resolve import PROMPT_NAME, resolve_lines
from ns.providers.anthropic.client import load_prompt
from ns.providers.anthropic.schemas import ResolvedLine

log = get_logger(__name__)

# A gram estimate is counted correct within this much of the label. Grams are
# an estimate for anything the receipt did not weigh, and demanding exactness
# would measure luck.
GRAMS_TOLERANCE_PCT = Decimal("10")

# Lines per model call. Small enough that one bad batch does not lose a whole
# run, large enough that batching is still the point.
BATCH_SIZE = 25

# Calibration bins. Ten is the convention for expected calibration error.
CALIBRATION_BINS = 10


@dataclass(frozen=True, slots=True)
class Scored:
    """One example, with what the resolver said about it."""

    example: EvalExample
    predicted: ResolvedLine | None
    predicted_food_id: int | None
    food_correct: bool
    grams_correct: bool | None  # None when the label states no expected grams
    confidence: float

    @property
    def answered(self) -> bool:
        """Whether the resolver committed to an identity at all."""
        return self.predicted is not None and self.predicted.canonical_name is not None


@dataclass(frozen=True, slots=True)
class EvalReport:
    run: ResolverRun
    scored: list[Scored] = field(default_factory=list)

    @property
    def mistakes(self) -> list[Scored]:
        return [s for s in self.scored if not s.food_correct]


def _canonical(name: str | None) -> str | None:
    if name is None:
        return None
    return " ".join(name.strip().lower().split())


def _as_line(example: EvalExample, index: int) -> LineItem:
    """A transient LineItem carrying the example's text.

    Never added to the session. The resolver takes line items, and building one
    here means the eval path runs exactly the code a real receipt runs rather
    than a parallel implementation that could drift from it.
    """
    return LineItem(
        receipt_id=0,
        line_index=index,
        raw_text=example.raw_text,
        normalized_text=example.normalized_text,
        normalizer_version=example.normalizer_version,
        kind=LineItemKind.PRODUCT,
        price_cents=0,
    )


def _grams_within_tolerance(expected: Decimal | None, predicted: str | None) -> bool | None:
    if expected is None or expected <= 0:
        return None
    if predicted is None:
        return False
    try:
        value = Decimal(predicted)
    except (ArithmeticError, ValueError):
        return False
    return abs(value - expected) / expected * Decimal(100) <= GRAMS_TOLERANCE_PCT


def _expected_calibration_error(scored: list[Scored]) -> float | None:
    """How far the resolver's stated confidence is from its actual accuracy.

    Binned, because calibration is not defined for a single prediction: it is a
    claim about a population. A resolver saying 0.9 on a hundred lines should
    be right on about ninety of them.
    """
    answered = [s for s in scored if s.answered]
    if not answered:
        return None

    total = len(answered)
    error = 0.0
    for index in range(CALIBRATION_BINS):
        low = index / CALIBRATION_BINS
        high = (index + 1) / CALIBRATION_BINS
        bucket = [
            s
            for s in answered
            if (low < s.confidence <= high) or (index == 0 and s.confidence <= high)
        ]
        if not bucket:
            continue
        accuracy = sum(1 for s in bucket if s.food_correct) / len(bucket)
        confidence = sum(s.confidence for s in bucket) / len(bucket)
        error += len(bucket) / total * abs(accuracy - confidence)
    return round(error, 4)


async def _average_lines_per_receipt(session: AsyncSession) -> float | None:
    """Measured, not assumed, so cost per receipt means something."""
    counts = (
        (
            await session.execute(
                select(func.count())
                .select_from(LineItem)
                .where(col(LineItem.kind) == LineItemKind.PRODUCT)
                .group_by(col(LineItem.receipt_id))
            )
        )
        .scalars()
        .all()
    )
    return statistics.mean(counts) if counts else None


async def _score(
    session: AsyncSession, example: EvalExample, predicted: ResolvedLine | None
) -> Scored:
    """Compare one prediction against its label.

    Food identity is compared by resolved id, not by string similarity. An
    exact-match rule is a strict lower bound — two different phrasings of the
    same food score as a miss — and a strict, reproducible number is worth more
    than a generous one that moves when the judge does. Near misses are kept in
    the run's breakdown so the strictness stays visible.
    """
    name = _canonical(predicted.canonical_name) if predicted else None
    predicted_food_id: int | None = None
    if name is not None:
        food = (
            await session.execute(select(Food).where(col(Food.canonical_name) == name))
        ).scalar_one_or_none()
        predicted_food_id = food.id if food else None

    if example.expected_is_nonfood:
        correct = predicted is not None and predicted.is_nonfood
    elif example.expected_food_id is None:
        # The label says this text is not identifiable. Getting that right
        # means declining to answer — which is principle 2 as a measurement.
        correct = predicted is None or predicted.canonical_name is None
    else:
        correct = predicted_food_id == example.expected_food_id

    return Scored(
        example=example,
        predicted=predicted,
        predicted_food_id=predicted_food_id,
        food_correct=correct,
        grams_correct=_grams_within_tolerance(
            example.expected_grams, predicted.grams_estimate if predicted else None
        ),
        confidence=predicted.confidence if predicted else 0.0,
    )


async def run_eval(
    session: AsyncSession,
    *,
    split: EvalSplit = EvalSplit.HOLDOUT,
    threshold: float | None = None,
    notes: str | None = None,
) -> EvalReport:
    """Score the resolver against a labeled split and record the run.

    Costs money: it is a batch of real model calls. It is meant to be run
    deliberately, after a prompt change, not on every receipt.
    """
    settings = get_settings()
    confidence_threshold = (
        threshold if threshold is not None else settings.resolution_min_confidence
    )

    examples = list(
        (
            await session.execute(
                select(EvalExample)
                .where(col(EvalExample.split) == split)
                .order_by(col(EvalExample.id))
            )
        )
        .scalars()
        .all()
    )
    if not examples:
        raise ValueError(
            f"No {split.value} examples to score. Labels come from corrections and "
            "confirmations — resolve a receipt and review it first."
        )

    predictions: dict[int, ResolvedLine] = {}
    costs: list[Decimal] = []
    latencies: list[int] = []
    model = settings.anthropic_model

    for start in range(0, len(examples), BATCH_SIZE):
        chunk = examples[start : start + BATCH_SIZE]
        lines = [_as_line(example, start + offset) for offset, example in enumerate(chunk)]
        resolved, call = await resolve_lines(session, lines)
        predictions.update(resolved)
        costs.append(call.cost_usd)
        latencies.append(call.latency_ms)
        model = call.model

    scored = [
        await _score(session, example, predictions.get(index))
        for index, example in enumerate(examples)
    ]

    confident = [s for s in scored if s.answered and s.confidence >= confidence_threshold]
    graded_grams = [s for s in scored if s.grams_correct is not None]

    average_lines = await _average_lines_per_receipt(session)
    total_cost = sum(costs, Decimal(0))
    cost_per_receipt = (
        float(total_cost) / len(examples) * average_lines if average_lines and examples else None
    )

    run = ResolverRun(
        model=model,
        prompt_version=load_prompt(PROMPT_NAME).version,
        normalizer_version=NORMALIZER_VERSION,
        n_examples=len(examples),
        food_accuracy=round(sum(1 for s in scored if s.food_correct) / len(scored), 4),
        grams_within_tolerance=(
            round(sum(1 for s in graded_grams if s.grams_correct) / len(graded_grams), 4)
            if graded_grams
            else 0.0
        ),
        precision_at_threshold=(
            round(sum(1 for s in confident if s.food_correct) / len(confident), 4)
            if confident
            else 0.0
        ),
        confidence_threshold=confidence_threshold,
        expected_calibration_error=_expected_calibration_error(scored),
        breakdown=_breakdown(scored, confidence_threshold),
        cost_usd_per_receipt=cost_per_receipt,
        latency_p50_ms=int(statistics.median(latencies)) if latencies else None,
        latency_p95_ms=_percentile(latencies, 95),
        notes=notes,
    )
    session.add(run)
    await session.flush()

    log.info(
        "eval.completed",
        split=split.value,
        n=len(examples),
        food_accuracy=run.food_accuracy,
        precision_at_threshold=run.precision_at_threshold,
        ece=run.expected_calibration_error,
        cost_usd=str(total_cost),
    )
    return EvalReport(run=run, scored=scored)


def _percentile(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(percentile / 100 * (len(ordered) - 1)))
    return ordered[index]


def _breakdown(scored: list[Scored], threshold: float) -> dict[str, object]:
    """Per-store and per-outcome detail, plus every mistake.

    The aggregate number says whether the resolver is good. This says where it
    is bad, which is the part that tells you what to change.
    """
    by_store: dict[str, dict[str, int]] = {}
    for s in scored:
        key = str(s.example.store_id) if s.example.store_id is not None else "global"
        bucket = by_store.setdefault(key, {"n": 0, "correct": 0})
        bucket["n"] += 1
        bucket["correct"] += int(s.food_correct)

    declined = [s for s in scored if not s.answered]
    wrong_and_confident = [
        s for s in scored if s.answered and s.confidence >= threshold and not s.food_correct
    ]

    return {
        "by_store": by_store,
        "declined_to_answer": len(declined),
        "declined_correctly": sum(1 for s in declined if s.food_correct),
        # The failure that actually costs the user something: a wrong answer
        # delivered with enough confidence to skip the correction queue.
        "confidently_wrong": [
            {
                "normalized_text": s.example.normalized_text,
                "predicted": s.predicted.canonical_name if s.predicted else None,
                "expected_food_id": s.example.expected_food_id,
                "confidence": s.confidence,
            }
            for s in wrong_and_confident
        ],
        "mistakes": [
            {
                "normalized_text": s.example.normalized_text,
                "predicted": s.predicted.canonical_name if s.predicted else None,
                "expected_food_id": s.example.expected_food_id,
                "confidence": s.confidence,
            }
            for s in scored
            if not s.food_correct
        ],
    }
