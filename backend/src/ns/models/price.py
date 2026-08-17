"""Price observations — derived, and rebuildable by design (DECISIONS.md D9).

`price_cents_per_100g` is computed from price and grams, and *both* inputs
change when a line item is corrected. Without a rebuild path a gram fix leaves
stale price history, which is exactly the data the flagship ranking reads. So
`derive` drops and regenerates these per receipt; nothing here is ever
hand-patched.
"""

from datetime import date
from decimal import Decimal

from sqlmodel import Field, Index, SQLModel

from ns.models.base import enum_column, grams_column, ratio_column
from ns.models.enums import GramsBasis


class PriceObservation(SQLModel, table=True):
    __tablename__ = "price_observation"

    id: int | None = Field(default=None, primary_key=True)

    # Provenance: which line produced this, so a rebuild is exact.
    line_item_id: int = Field(foreign_key="line_item.id", index=True, ondelete="CASCADE")
    food_id: int = Field(foreign_key="food.id", index=True)
    store_id: int | None = Field(default=None, foreign_key="store.id", index=True)
    observed_at: date = Field(index=True)

    # Raw components kept alongside the ratio so the number is auditable and
    # a unit change never requires re-deriving from the receipt.
    price_cents: int
    grams: Decimal = Field(sa_column=grams_column(nullable=False))
    price_cents_per_100g: Decimal = Field(sa_column=ratio_column(nullable=False))

    # A ranking built from receipt-stated weights is much stronger evidence
    # than one built from estimates, and the UI must be able to say so.
    grams_basis: GramsBasis = Field(sa_column=enum_column(GramsBasis, nullable=False))

    # Sale prices are excluded from the baseline: a sale is not what the food
    # costs (DECISIONS.md D11).
    was_discounted: bool = Field(default=False, nullable=False, index=True)

    __table_args__ = (
        # The flagship ranking and Phase 2 cross-store comparison both read
        # this shape.
        Index("ix_price_obs_food_store_date", "food_id", "store_id", "observed_at"),
    )
