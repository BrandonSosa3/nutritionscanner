"""Evaluation harness storage.

Ships in Phase 1 alongside the resolver, not after it (principle 4). The
resolver is the one component that can be confidently wrong, and without a
tracked number there is no way to tell whether a prompt change helped.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, ForeignKey, Integer
from sqlmodel import Field, Index, SQLModel

from ns.models.base import enum_column, grams_column, jsonb_column, timestamp_column, utcnow
from ns.models.enums import EvalSplit, GramsBasis, LabelSource


class EvalExample(SQLModel, table=True):
    """One labeled line item.

    Both corrections *and* confirmations land here (DECISIONS.md D6). An eval
    set drawn only from corrections is entirely cases the resolver got wrong —
    a biased sample that can never show improvement.
    """

    __tablename__ = "eval_example"

    id: int | None = Field(default=None, primary_key=True)

    raw_text: str = Field(max_length=500)
    # Both are stored: labels are attached to raw text, but the resolver runs
    # on normalized text, and the normalizer version pins what that meant.
    normalized_text: str = Field(index=True, max_length=300)
    normalizer_version: str = Field(max_length=64)

    store_id: int | None = Field(default=None, foreign_key="store.id", index=True)

    expected_food_id: int | None = Field(default=None, foreign_key="food.id", index=True)
    expected_is_nonfood: bool = Field(default=False, nullable=False)
    expected_grams: Decimal | None = Field(default=None, sa_column=grams_column())
    expected_grams_basis: GramsBasis = Field(
        default=GramsBasis.UNKNOWN,
        sa_column=enum_column(GramsBasis, nullable=False),
    )

    label_source: LabelSource = Field(
        sa_column=enum_column(LabelSource, nullable=False, index=True)
    )
    split: EvalSplit = Field(
        default=EvalSplit.TRAIN,
        sa_column=enum_column(EvalSplit, nullable=False, index=True),
    )

    # Provenance only, and cleared rather than blocking (`SET NULL`).
    # Normalisation replaces a receipt's line items wholesale, which a plain
    # foreign key turned into a hard failure: once a line had been labelled,
    # its receipt could never be re-normalised, and "every stage after extract
    # is replayable from the stored extraction" stopped being true.
    #
    # The label survives losing this pointer. `raw_text`, `normalized_text`,
    # and the expected answer are all stored on the row itself, deliberately,
    # so an example is self-contained.
    source_line_item_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("line_item.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    added_at: datetime = Field(default_factory=utcnow, sa_column=timestamp_column(nullable=False))


class ResolverRun(SQLModel, table=True):
    """One scored run against the holdout set. The trend line over time."""

    __tablename__ = "resolver_run"

    id: int | None = Field(default=None, primary_key=True)
    run_at: datetime = Field(default_factory=utcnow, sa_column=timestamp_column(nullable=False))

    # The three things whose change invalidates a comparison.
    model: str = Field(max_length=80)
    prompt_version: str = Field(max_length=64)
    normalizer_version: str = Field(max_length=64)

    n_examples: int
    food_accuracy: float
    grams_within_tolerance: float
    precision_at_threshold: float
    confidence_threshold: float
    expected_calibration_error: float | None = None

    # Broken out by store and by category, per the brief.
    breakdown: dict[str, object] | None = Field(default=None, sa_column=jsonb_column())

    # A prompt that is 2% more accurate at 5x the cost is a bad trade, so cost
    # and latency are tracked next to accuracy rather than separately.
    cost_usd_per_receipt: float | None = None
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None

    notes: str | None = Field(default=None, max_length=2000)

    __table_args__ = (Index("ix_resolver_run_model_time", "model", "run_at"),)
