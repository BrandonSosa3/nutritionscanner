"""Enumerations shared across the schema.

Stored as VARCHAR with a CHECK constraint (`native_enum=False`) rather than a
native Postgres enum type. Both give database-level integrity; the CHECK form
is far easier to evolve, because adding a value is an ordinary migration
instead of an `ALTER TYPE` that cannot run inside a transaction.
"""

from enum import StrEnum


class PipelineStatus(StrEnum):
    """Where a receipt is in the pipeline.

    Deliberately separate from reconciliation state: a receipt can be fully
    processed *and* fail to reconcile, and the UI needs to say so.
    """

    UPLOADED = "uploaded"  # image stored and hashed — a receipt is never lost
    EXTRACTING = "extracting"
    EXTRACT_FAILED = "extract_failed"  # API down or unreadable; retryable
    NORMALIZED = "normalized"
    RECONCILED = "reconciled"
    RESOLVING = "resolving"
    NEEDS_REVIEW = "needs_review"  # unresolved lines awaiting correction
    COMPLETE = "complete"


class ReconciliationStatus(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    BALANCED = "balanced"
    SUSPECT = "suspect"  # differs from the printed total beyond tolerance
    UNRECONCILABLE = "unreconcilable"  # extraction too incomplete to even try


class ResolutionSource(StrEnum):
    """How a line item got its food identity. Ordered by trust."""

    CORRECTION_STORE = "correction_store"  # tier 1a: store-specific correction
    CORRECTION_GLOBAL = "correction_global"  # tier 1b: global correction
    LLM = "llm"  # tier 2: batched model call
    UNRESOLVED = "unresolved"  # tier 3: a real, visible, honest state
    NONFOOD = "nonfood"  # bag fees, foil pans, deposits


class GramsBasis(StrEnum):
    """How a gram figure was derived — see DECISIONS.md D3.

    A correction stores one of these plus a value, never a bare gram number,
    because food identity is stable across purchases and weight usually is not.
    """

    FROM_RECEIPT = "from_receipt"  # receipt states the weight; trust it
    PER_PACKAGE = "per_package"  # fixed pack size x quantity
    PER_UNIT_ESTIMATE = "per_unit_estimate"  # countable, estimated per item
    DENSITY = "density"  # volume x Food.density_g_per_ml
    UNKNOWN = "unknown"  # cannot determine — stays unresolved


class LabelSource(StrEnum):
    """Why an eval example exists — see DECISIONS.md D6.

    Without CONFIRMED, the eval set is entirely cases the resolver got wrong:
    a biased sample of hard cases that can never demonstrate improvement.
    """

    CORRECTED = "corrected"  # the resolver was wrong and was fixed
    CONFIRMED = "confirmed"  # the resolver was right and was confirmed


class EvalSplit(StrEnum):
    TRAIN = "train"
    HOLDOUT = "holdout"  # never feeds the corrections table used at inference


class LineItemKind(StrEnum):
    """What a printed line actually represents.

    Receipts carry more than products: coupons on their own line, bag fees,
    bottle deposits, and section headers. Classifying these explicitly is what
    lets reconciliation add up.
    """

    PRODUCT = "product"
    DISCOUNT = "discount"  # negative, or a basket-level LOYALTY line
    FEE = "fee"  # bag fee, bottle deposit, CRV
    TAX = "tax"
    SUBTOTAL = "subtotal"
    TOTAL = "total"
    UNKNOWN = "unknown"  # e.g. the Australian bare `SPECIAL` lines


class FoodCategory(StrEnum):
    """Phase 2 spend breakdown. Coarse on purpose."""

    PRODUCE = "produce"
    PROTEIN = "protein"
    DAIRY = "dairy"
    GRAINS = "grains"
    PACKAGED = "packaged"
    BEVERAGES = "beverages"
    HOUSEHOLD = "household"  # non-food
    UNCATEGORIZED = "uncategorized"


class LlmStage(StrEnum):
    EXTRACT = "extract"
    RESOLVE = "resolve"
    EVAL = "eval"
