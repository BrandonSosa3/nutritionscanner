"""API response models.

Deliberately separate from the SQLModel tables. Receipt images are personal
financial records, so `image_path` is never serialised — the client refers to
an image by receipt id and fetches it through an endpoint, and a storage key
never appears in a response body or a log line.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ns.models import Receipt
from ns.models.enums import (
    EvalSplit,
    FoodCategory,
    GramsBasis,
    LabelSource,
    LineItemKind,
    PipelineStatus,
    ReconciliationStatus,
    ResolutionSource,
)


class ReceiptSummary(BaseModel):
    """A receipt as it appears in a list."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: PipelineStatus
    reconciliation_status: ReconciliationStatus
    store_id: int | None
    # Resolved by the router. A correction's scope is stated to the user as
    # "applies only at Costco Wholesale", which needs a name, not an id.
    store_name: str | None = None
    purchased_at: date | None
    total_cents: int | None
    currency: str
    uploaded_at: datetime

    @classmethod
    def of(cls, receipt: Receipt) -> "ReceiptSummary":
        return cls.model_validate(receipt)


class ReceiptDetail(ReceiptSummary):
    """A single receipt, with the reconciliation evidence the UI needs."""

    subtotal_cents: int | None
    tax_cents: int | None
    reconciliation_delta_cents: int | None
    reconciliation_report: dict[str, object] | None
    extraction_model: str | None
    extracted_at: datetime | None
    duplicate_of_receipt_id: int | None
    image_bytes: int
    # Content hash is safe to expose and lets a client tell whether a local
    # file has already been uploaded before sending it.
    image_sha256: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, receipt: Receipt) -> "ReceiptDetail":
        return cls.model_validate(receipt)


class ReceiptUploadResponse(BaseModel):
    """The result of an upload.

    `created` distinguishes a new receipt from an idempotent re-upload, so the
    UI can say "already uploaded on 12 August" instead of implying a second
    receipt was recorded.
    """

    receipt: ReceiptDetail
    created: bool
    width: int
    height: int
    image_format: str


class ReceiptListResponse(BaseModel):
    items: list[ReceiptSummary]
    total: int
    limit: int
    offset: int


class ErrorResponse(BaseModel):
    """Errors state what happened and what to do about it (see DESIGN.md)."""

    detail: str = Field(description="Human-readable, safe to show a user verbatim.")


class ExtractionResponse(BaseModel):
    """What extraction produced, summarised for the client.

    The full transcription is available on the receipt detail endpoint as
    `raw_extraction`; this is the at-a-glance version.
    """

    receipt_id: int
    status: PipelineStatus
    store_name: str | None
    purchased_at: str | None
    currency: str
    line_item_count: int
    total: str | None
    legibility: str
    notes: str | None
    cost_usd: str
    latency_ms: int


class BudgetStatusResponse(BaseModel):
    """Current month's LLM spend against the configured ceiling."""

    month: date
    limit_usd: str | None
    spent_usd: str
    remaining_usd: str | None
    is_exhausted: bool
    call_count: int


class NormalizationResponse(BaseModel):
    """What normalisation made of a stored extraction.

    `dropped` counts lines that carry no basket value — section headers,
    payment lines, and anything with no readable amount. `unparseable_amounts`
    lists the printed text of amounts that could not be read, so a
    transcription problem is visible rather than silently absorbed.
    """

    receipt_id: int
    status: PipelineStatus
    line_item_count: int
    dropped: int
    with_grams: int
    unparseable_amounts: list[str]


class ReconciliationResponse(BaseModel):
    """Whether a receipt's arithmetic closes, and the evidence either way.

    `report` is the full working: what was summed, what the receipt stated,
    every check that ran, and — when the total does not close — the specific
    misreadings that would have made it close. It is meant to be rendered,
    not just logged.
    """

    receipt_id: int
    status: PipelineStatus
    reconciliation_status: ReconciliationStatus
    delta_cents: int | None
    tax_model: str
    report: dict[str, object]


# ── Resolution and corrections ────────────────────────────────────────────


class LineItemOut(BaseModel):
    """A line item as the correction queue needs to see it.

    Carries the resolution state in full, because the screen this feeds has to
    say not just what a line resolved to but how confidently and by what route
    — a store correction and a 0.61 model guess are not the same claim.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    line_index: int
    raw_text: str
    normalized_text: str
    kind: LineItemKind
    price_cents: int
    quantity: Decimal | None
    unit: str | None
    grams_as_purchased: Decimal | None
    grams_edible: Decimal | None
    grams_basis: GramsBasis
    food_id: int | None
    food_name: str | None = None
    resolution_source: ResolutionSource
    confidence: float | None


class LineItemListResponse(BaseModel):
    receipt_id: int
    items: list[LineItemOut]
    resolved: int
    total: int
    coverage: float = Field(description="Resolved share of resolvable lines, 0 to 1.")


class ResolutionResponse(BaseModel):
    """The result of resolving a receipt.

    `coverage` is stated here and not buried, because every summary has to say
    how much of the basket it actually accounts for (principle 6).
    """

    receipt_id: int
    status: PipelineStatus
    by_source: dict[str, int]
    coverage: float
    unresolved: list[str]
    cost_usd: str
    latency_ms: int | None


class CorrectionRequest(BaseModel):
    """A user's fix for one line.

    `grams_basis` and `grams_value` are a *rule*, never a measured weight
    (DECISIONS.md D3): "eggs come in 18-count boxes of 50 g each", not "this
    box weighed 900 g". The rule is replayed against each future line's own
    quantity.
    """

    food_id: int | None = None
    is_nonfood: bool = False
    grams_basis: GramsBasis = GramsBasis.UNKNOWN
    grams_value: Decimal | None = None
    global_scope: bool = Field(
        default=False,
        description="Apply at every store rather than only the one on this receipt.",
    )


class CorrectionResponse(BaseModel):
    line_item_id: int
    correction_id: int
    applied_to_line_items: int = Field(
        description="Lines updated across all receipts, past ones included."
    )
    eval_example_id: int
    split: EvalSplit
    label_source: LabelSource


class EvalRunResponse(BaseModel):
    """One scored run against a labeled split.

    `precision_at_threshold` matters more than `food_accuracy`: it is how often
    the resolver is right *when it was confident enough to skip review*.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    run_at: datetime
    model: str
    prompt_version: str
    normalizer_version: str
    n_examples: int
    food_accuracy: float
    grams_within_tolerance: float
    precision_at_threshold: float
    confidence_threshold: float
    expected_calibration_error: float | None
    cost_usd_per_receipt: float | None
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    breakdown: dict[str, object] | None
    notes: str | None


class EvalRunListResponse(BaseModel):
    items: list[EvalRunResponse]
    total: int


# ── Foods and nutrition ───────────────────────────────────────────────────


class NutrientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    nutrient_code: str
    amount_per_100g: Decimal
    unit: str


class FoodSummary(BaseModel):
    """A food and whether it has nutrition behind it yet.

    `has_nutrition` is not cosmetic: a food without it contributes visible
    uncovered mass to every summary rather than a silent zero.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    canonical_name: str
    category: FoodCategory
    fdc_id: int | None
    fdc_data_type: str | None
    edible_portion_pct: Decimal
    density_g_per_ml: Decimal | None
    nutrient_count: int = 0
    has_nutrition: bool = False


class UsdaCandidateOut(BaseModel):
    """A FoodData Central entry that was considered, and how it fared."""

    fdc_id: int
    description: str
    data_type: str | None
    score: float
    recall: float
    precision: float
    rejected_reason: str | None


class FoodDetail(FoodSummary):
    nutrients: list[NutrientOut] = Field(default_factory=list)
    # Recorded at match time. When nothing matched, this is what the review
    # screen offers the user to choose from.
    candidates: list[UsdaCandidateOut] = Field(default_factory=list)
    chosen_by: str | None = None


class FoodListResponse(BaseModel):
    items: list[FoodSummary]
    total: int
    without_nutrition: int


class EnrichmentResponse(BaseModel):
    attempted: int
    enriched: int
    unmatched: list[str]
    failed: list[str]
    coverage: float


class UsdaOverrideRequest(BaseModel):
    fdc_id: int = Field(description="The FoodData Central entry to attach.")


# ── Basket summary ────────────────────────────────────────────────────────


class CoverageOut(BaseModel):
    """How much of a basket a total accounts for.

    `spend_share` and `line_share` have complete denominators — every line has
    a price, and every line is a line.

    `weight_share` does not: lines with no weight are absent from both sides of
    it, so it describes the mass that was measured rather than the basket. It
    always travels with `lines_without_weight` so it cannot be read as the
    whole.
    """

    lines_total: int
    lines_resolved: int
    lines_with_nutrition: int
    spend_share: float
    line_share: float
    weight_share: float
    grams_total: str
    grams_with_nutrition: str
    # Why the rest is missing, counted separately because each needs a
    # different fix: a correction, a USDA match, or a weight.
    unresolved_lines: int
    lines_without_nutrition: int
    lines_without_weight: int
    is_partial: bool


class BasketSummaryResponse(BaseModel):
    """What a set of receipts *contained*.

    Supply, not intake. These are groceries bought, not food eaten, and the
    field names say so. A client rendering this as "you consumed" is a bug.
    """

    receipt_ids: list[int]
    starts_on: date | None
    ends_on: date | None
    currency: str
    total_spend_cents: int
    # Amounts as strings: these are Decimals, and a float here would put
    # binary rounding between the database and the display.
    nutrients: dict[str, str]
    units: dict[str, str]
    coverage: CoverageOut
    # Built server-side so every client says the same thing, and so none can
    # render the totals without the caveat (principle 6).
    headline: str


class NutrientCostRow(BaseModel):
    food_id: int
    canonical_name: str
    observations: int
    median_price_cents_per_100g: str
    nutrient_per_100g: str
    cost_cents_per_unit: str
    from_receipt_weights: int = Field(
        description="Observations whose weight was read off a receipt rather than estimated."
    )


class NutrientCostResponse(BaseModel):
    nutrient: str
    label: str
    unit: str
    items: list[NutrientCostRow]


class DerivationResponse(BaseModel):
    """Price observations rebuilt from a receipt's resolved lines.

    The two skip counts are separate because they need different fixes: an
    unresolved line needs a correction, a line with no weight needs a gram
    rule.
    """

    receipt_id: int
    observations: int
    skipped_no_grams: int
    skipped_unresolved: int


class FoodCreateRequest(BaseModel):
    """A food the user names because the catalogue doesn't have it yet.

    Category is coarse and optional — it drives the Phase 2 spend breakdown,
    not nutrition, so leaving it uncategorised costs nothing that matters.
    """

    canonical_name: str = Field(
        min_length=1,
        max_length=300,
        description="Specific food name, as USDA would describe the ingredient.",
    )
    category: FoodCategory = FoodCategory.UNCATEGORIZED


class UsdaCandidateListResponse(BaseModel):
    """Candidates for a food, scored against its name but not saved.

    Rejected ones are included with their reason: the automatic matcher is
    strict on purpose, and a person can see that a candidate was excluded for
    naming a different species and decide for themselves.
    """

    food_id: int
    queried: str
    items: list[UsdaCandidateOut]
