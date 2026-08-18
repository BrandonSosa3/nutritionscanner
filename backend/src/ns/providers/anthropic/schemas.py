"""The structured shape extraction returns.

Design rule: extraction is *faithful*, not interpretive. It records what is
printed and nothing more. It does not decide that `ZUCHINNI GREEN` is
zucchini, does not convert 0.778 kg to grams, and does not judge whether a
`SPECIAL` line is a discount. Normalisation does all of that, working from
this stored record — which is what makes `raw_extraction` durable across
prompt, normaliser, and resolver changes.

Monetary and decimal values are strings, deliberately. The model transcribes
the characters printed; `domain.money` parses them into integer cents. A JSON
number would put float representation between the paper and the database.

## Why the field set is this small

Structured outputs compiles a decoding grammar, and an unbounded array of
objects with many optional fields is expensive to compile. A twelve-field
version of `ExtractedLineItem` failed with "Grammar compilation timed out";
this eight-field version compiles in about twenty seconds.

Nothing is lost by the reduction. `raw_text` is verbatim, so tax flags,
units, and package sizes are all still present — they are simply parsed
downstream rather than split out here, which is where that interpretation
belonged anyway.
"""

from typing import Literal

from pydantic import BaseModel, Field

LineKind = Literal[
    "product",
    "discount",
    "fee",
    "tax",
    "subtotal",
    "total",
    "section_header",
    "payment",
    "unknown",
]

Legibility = Literal["clear", "partial", "poor"]


class ExtractedLineItem(BaseModel):
    """One printed line, transcribed."""

    line_index: int
    raw_text: str
    amount: str | None = Field(default=None, description="As printed, e.g. '4.66', '2.00-'.")
    kind: LineKind

    quantity: str | None = None
    unit_price: str | None = None
    weight_text: str | None = Field(
        default=None, description="Weight stated for this item, with unit, e.g. '0.778 kg'."
    )
    item_code: str | None = Field(default=None, description="PLU, SKU, or item number.")


class ExtractedReceipt(BaseModel):
    """A whole receipt, transcribed."""

    store_name: str | None = None
    store_location: str | None = None
    currency: str = Field(description="ISO 4217, inferred from the receipt.")
    purchased_at: str | None = Field(default=None, description="YYYY-MM-DD, or null if ambiguous.")

    line_items: list[ExtractedLineItem]

    subtotal: str | None = None
    tax_total: str | None = None
    total: str | None = None
    item_count_stated: int | None = None

    legibility: Legibility
    notes: str | None = Field(default=None, description="Anything ambiguous or unreadable.")
