"""Foods and their nutrients.

Nutrients live in a narrow table rather than a jsonb blob (DECISIONS.md D8):
the flagship view ranks every food by cost per gram of protein, which is a
sort and aggregate over one nutrient. That is a join here and jsonb surgery
otherwise. The raw USDA payload is kept alongside for provenance.
"""

from datetime import datetime
from decimal import Decimal

from sqlmodel import Field, Index, SQLModel

from ns.models.base import (
    enum_column,
    jsonb_column,
    ratio_column,
    timestamp_column,
    utcnow,
)
from ns.models.enums import FoodCategory


class Food(SQLModel, table=True):
    __tablename__ = "food"

    id: int | None = Field(default=None, primary_key=True)

    canonical_name: str = Field(index=True, max_length=300)
    category: FoodCategory = Field(
        default=FoodCategory.UNCATEGORIZED,
        sa_column=enum_column(FoodCategory, nullable=False, index=True),
    )

    # USDA FoodData Central. `data_type` matters for resolution quality:
    # foundation/sr_legacy carry generic ingredients and refuse percentages,
    # branded carries packaged goods with a UPC.
    fdc_id: int | None = Field(default=None, unique=True)
    fdc_data_type: str | None = Field(default=None, max_length=40)

    # Volume to mass. Absent means volume units cannot be converted and the
    # line stays unresolved rather than assuming the density of water.
    density_g_per_ml: Decimal | None = Field(default=None, sa_column=ratio_column())

    # A banana line is peel-inclusive weight. Bone-in chicken, melon, avocado,
    # and shrimp all differ sharply between purchased and edible weight;
    # ignoring this overstates a produce-heavy basket by 20%+.
    edible_portion_pct: Decimal = Field(
        default=Decimal("100"), sa_column=ratio_column(nullable=False)
    )

    # Phase 3 recipe costing. Column exists now so the migration isn't painful.
    cooked_yield_factor: Decimal | None = Field(default=None, sa_column=ratio_column())

    usda_payload: dict[str, object] | None = Field(default=None, sa_column=jsonb_column())
    fetched_at: datetime | None = Field(default=None, sa_column=timestamp_column())
    # Part of the seeded offline cache, so a cold start works without network.
    is_seed: bool = Field(default=False, nullable=False)

    created_at: datetime = Field(default_factory=utcnow, sa_column=timestamp_column(nullable=False))


class FoodNutrient(SQLModel, table=True):
    """One nutrient value per 100 g of *edible* portion."""

    __tablename__ = "food_nutrient"

    id: int | None = Field(default=None, primary_key=True)
    food_id: int = Field(foreign_key="food.id", index=True, ondelete="CASCADE")

    # Free-form rather than an enum: USDA publishes hundreds of nutrients and
    # the set we care about will grow. Canonical codes are in domain/nutrition.
    nutrient_code: str = Field(index=True, max_length=60)
    amount_per_100g: Decimal = Field(sa_column=ratio_column(nullable=False))
    unit: str = Field(max_length=12)  # g | mg | ug | kcal | IU

    __table_args__ = (Index("uq_food_nutrient_food_code", "food_id", "nutrient_code", unique=True),)
