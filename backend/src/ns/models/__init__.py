"""SQLModel table definitions.

Importing this package registers every table on `SQLModel.metadata`. Alembic's
env.py imports it for exactly that reason — without these imports autogenerate
produces an empty migration.

Every model here is re-exported, so `from ns.models import Receipt` works and
nothing depends on the file layout.
"""

from ns.models.correction import Correction
from ns.models.enums import (
    EvalSplit,
    FoodCategory,
    GramsBasis,
    LabelSource,
    LineItemKind,
    LlmStage,
    PipelineStatus,
    ReconciliationStatus,
    ResolutionSource,
)
from ns.models.evaluation import EvalExample, ResolverRun
from ns.models.food import Food, FoodNutrient
from ns.models.observability import LlmCall
from ns.models.planning import Budget, NutrientReference
from ns.models.price import PriceObservation
from ns.models.receipt import LineItem, Receipt
from ns.models.store import Store, StoreAlias

__all__ = [
    "Budget",
    "Correction",
    "EvalExample",
    "EvalSplit",
    "Food",
    "FoodCategory",
    "FoodNutrient",
    "GramsBasis",
    "LabelSource",
    "LineItem",
    "LineItemKind",
    "LlmCall",
    "LlmStage",
    "NutrientReference",
    "PipelineStatus",
    "PriceObservation",
    "Receipt",
    "ReconciliationStatus",
    "ResolutionSource",
    "ResolverRun",
    "Store",
    "StoreAlias",
]
