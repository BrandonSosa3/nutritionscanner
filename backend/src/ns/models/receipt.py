"""Receipts and their line items — the spine of the system.

Money is integer cents throughout (DECISIONS.md D1). Reconciliation compares a
sum of line items against a printed total within tolerance; float drift
eventually fails an arithmetically perfect receipt, and that failure is
indistinguishable from a real extraction error.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlmodel import Field, Index, SQLModel

from ns.models.base import (
    enum_column,
    grams_column,
    jsonb_column,
    ratio_column,
    timestamp_column,
    utcnow,
)
from ns.models.enums import (
    GramsBasis,
    LineItemKind,
    PipelineStatus,
    ReconciliationStatus,
    ResolutionSource,
)


class Receipt(SQLModel, table=True):
    __tablename__ = "receipt"

    id: int | None = Field(default=None, primary_key=True)

    # Idempotency key. Re-uploading the same file updates rather than
    # duplicating. Two photos of one receipt are two hashes — that case is
    # caught after extraction by (store, purchased_at, total_cents).
    image_sha256: str = Field(unique=True, index=True, max_length=64)
    image_path: str = Field(max_length=500)
    image_bytes: int
    uploaded_at: datetime = Field(
        default_factory=utcnow, sa_column=timestamp_column(nullable=False)
    )

    store_id: int | None = Field(default=None, foreign_key="store.id", index=True)

    # From the receipt, never from upload time — backfilling old receipts is a
    # first-class path (DECISIONS.md D18) and all summaries group by this.
    purchased_at: date | None = Field(default=None, index=True)
    currency: str = Field(default="USD", max_length=3)

    subtotal_cents: int | None = None
    tax_cents: int | None = None
    total_cents: int | None = None

    # Stored permanently. Every stage after extract replays from this without
    # re-photographing anything.
    raw_extraction: dict[str, object] | None = Field(default=None, sa_column=jsonb_column())
    extraction_model: str | None = Field(default=None, max_length=80)
    extraction_prompt_version: str | None = Field(default=None, max_length=64)
    extracted_at: datetime | None = Field(default=None, sa_column=timestamp_column())

    status: PipelineStatus = Field(
        default=PipelineStatus.UPLOADED,
        sa_column=enum_column(PipelineStatus, nullable=False, index=True),
    )

    reconciliation_status: ReconciliationStatus = Field(
        default=ReconciliationStatus.NOT_ATTEMPTED,
        sa_column=enum_column(ReconciliationStatus, nullable=False, index=True),
    )
    # "Show me why" needs the size of the discrepancy and which lines caused
    # it, not just a status word.
    reconciliation_delta_cents: int | None = None
    reconciliation_report: dict[str, object] | None = Field(default=None, sa_column=jsonb_column())

    # Set when this receipt looks like a re-photograph of an existing one.
    # Flagged for a one-tap decision, never auto-merged (DECISIONS.md D13).
    duplicate_of_receipt_id: int | None = Field(default=None, foreign_key="receipt.id", index=True)

    notes: str | None = Field(default=None, max_length=2000)

    created_at: datetime = Field(default_factory=utcnow, sa_column=timestamp_column(nullable=False))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=timestamp_column(nullable=False))

    __table_args__ = (
        # Duplicate detection scans this directly.
        Index("ix_receipt_store_date_total", "store_id", "purchased_at", "total_cents"),
    )


class LineItem(SQLModel, table=True):
    __tablename__ = "line_item"

    id: int | None = Field(default=None, primary_key=True)
    receipt_id: int = Field(foreign_key="receipt.id", index=True, ondelete="CASCADE")
    line_index: int  # order as printed, so the UI can mirror the paper

    raw_text: str = Field(max_length=500)
    normalized_text: str = Field(index=True, max_length=300)
    # Pinning this means a normalizer change cannot silently redefine what an
    # eval example or a correction was keyed against.
    normalizer_version: str = Field(max_length=64)

    kind: LineItemKind = Field(
        default=LineItemKind.PRODUCT,
        sa_column=enum_column(LineItemKind, nullable=False, index=True),
    )

    price_cents: int
    quantity: Decimal | None = Field(default=None, sa_column=ratio_column())
    unit: str | None = Field(default=None, max_length=20)  # as printed: LB, kg, EA

    grams_as_purchased: Decimal | None = Field(default=None, sa_column=grams_column())
    grams_edible: Decimal | None = Field(default=None, sa_column=grams_column())
    grams_basis: GramsBasis = Field(
        default=GramsBasis.UNKNOWN,
        sa_column=enum_column(GramsBasis, nullable=False),
    )
    # Snapshot of the factor actually applied, so a later Food edit cannot
    # silently rewrite history without an explicit derive re-run.
    edible_portion_pct_applied: Decimal | None = Field(default=None, sa_column=ratio_column())

    food_id: int | None = Field(default=None, foreign_key="food.id", index=True)
    resolution_source: ResolutionSource = Field(
        default=ResolutionSource.UNRESOLVED,
        sa_column=enum_column(ResolutionSource, nullable=False, index=True),
    )
    # Graded, not boolean: calibration error is not computable over one bin
    # (DECISIONS.md D7).
    confidence: float | None = None
    resolved_at: datetime | None = Field(default=None, sa_column=timestamp_column())

    # Discount applied to this specific line, as a positive number of cents.
    # Basket-level discounts are their own line with kind=DISCOUNT.
    discount_cents: int = Field(default=0, nullable=False)

    __table_args__ = (
        Index("uq_line_item_receipt_index", "receipt_id", "line_index", unique=True),
        # The correction queue reads exactly this.
        Index(
            "ix_line_item_unresolved",
            "normalized_text",
            postgresql_where="food_id IS NULL",
        ),
    )
