"""Corrections — the core product loop.

Two decisions are load-bearing here:

D2: keyed on `(normalized_text, store_id)`, not on text alone. Receipt
abbreviations collide across chains, and a nullable store column is dead
weight if the text is globally unique. The global fallback row
(`store_id IS NULL`) needs a partial unique index, because Postgres treats
NULLs as distinct.

D3: stores a gram *rule*, never a bare gram figure. Food identity is stable
across purchases; weight usually is not. Replaying "1.2 lb of broccoli =
544 g" onto every future broccoli line corrupts every later basket.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlmodel import Field, Index, SQLModel

from ns.models.base import enum_column, grams_column, timestamp_column, utcnow
from ns.models.enums import GramsBasis


class Correction(SQLModel, table=True):
    __tablename__ = "correction"

    id: int | None = Field(default=None, primary_key=True)

    normalized_text: str = Field(index=True, max_length=300)
    # NULL means "applies at every store" — the fallback when no
    # store-specific correction matches.
    store_id: int | None = Field(default=None, foreign_key="store.id", index=True)

    food_id: int | None = Field(default=None, foreign_key="food.id", index=True)
    # "This is paper towels" is a valid, useful correction.
    is_nonfood: bool = Field(default=False, nullable=False)

    grams_basis: GramsBasis = Field(
        default=GramsBasis.UNKNOWN,
        sa_column=enum_column(GramsBasis, nullable=False),
    )
    # Meaning depends on grams_basis: grams per package, grams per unit, or
    # unused when the receipt states the weight directly.
    grams_value: Decimal | None = Field(default=None, sa_column=grams_column())

    applied_count: int = Field(default=0, nullable=False)
    created_at: datetime = Field(default_factory=utcnow, sa_column=timestamp_column(nullable=False))
    last_applied_at: datetime | None = Field(default=None, sa_column=timestamp_column())

    __table_args__ = (
        # Store-specific uniqueness.
        Index("uq_correction_text_store", "normalized_text", "store_id", unique=True),
        # Postgres treats NULLs as distinct in a unique index, so the index
        # above does NOT stop two global corrections for the same text, and
        # tier-1b resolution could then match either of two conflicting rows.
        # A partial index over the global rows closes that.
        #
        # Declared here and not only in the migration: an index the database
        # has and the models do not is drift, and it makes every subsequent
        # autogenerate propose dropping it.
        Index(
            "uq_correction_text_global",
            "normalized_text",
            unique=True,
            postgresql_where=text("store_id IS NULL"),
        ),
    )
