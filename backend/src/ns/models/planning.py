"""Phase 2 and Phase 3 storage.

These tables land in the first migration (DECISIONS.md D17). Schema is nearly
free to add now and expensive to migrate later, and the computations that read
them are pure functions testable against synthetic data today. What waits is
not the code but the *display*, behind explicit sufficiency gates — a
cross-store comparison with one observation per item is noise, and the UI says
so rather than printing a number.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlmodel import Field, Index, SQLModel

from ns.models.base import ratio_column, timestamp_column, utcnow


class Budget(SQLModel, table=True):
    """Phase 2: monthly grocery budget with spend pacing."""

    __tablename__ = "budget"

    id: int | None = Field(default=None, primary_key=True)
    # First day of the month it applies to; a plain date keeps range queries
    # trivial and avoids a text "YYYY-MM" that cannot be compared.
    month: date = Field(unique=True, index=True)
    amount_cents: int
    created_at: datetime = Field(default_factory=utcnow, sa_column=timestamp_column(nullable=False))


class NutrientReference(SQLModel, table=True):
    """Phase 3: reference intakes, framed as gaps rather than targets.

    Static reference data — it depends on no receipt history, so it can be
    seeded immediately. Principle 5 governs how it is *used*: this exists to
    answer "am I getting enough", never to compute a deficit to eat under.
    """

    __tablename__ = "nutrient_reference"

    id: int | None = Field(default=None, primary_key=True)

    nutrient_code: str = Field(index=True, max_length=60)
    # Demographic scoping, kept coarse. NULL means "applies to everyone".
    sex: str | None = Field(default=None, max_length=16)
    age_min_years: int | None = None
    age_max_years: int | None = None

    # Recommended Dietary Allowance / Adequate Intake, per day.
    rda_per_day: Decimal | None = Field(default=None, sa_column=ratio_column())
    # Tolerable Upper Intake Level. Present so "enough" never silently means
    # "more is always better".
    upper_limit_per_day: Decimal | None = Field(default=None, sa_column=ratio_column())
    unit: str = Field(max_length=12)

    source: str = Field(default="usda_dri", max_length=80)

    __table_args__ = (
        Index(
            "uq_nutrient_reference_scope",
            "nutrient_code",
            "sex",
            "age_min_years",
            "age_max_years",
            unique=True,
        ),
    )
