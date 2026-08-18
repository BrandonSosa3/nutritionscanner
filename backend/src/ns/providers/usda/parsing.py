"""Reading FoodData Central payloads. Pure — no network, no database.

The API returns nutrients in two different shapes depending on the endpoint,
and both appear in data this system stores:

- **Search** (`/foods/search`) flattens them:
  `{"nutrientId": 1003, "unitName": "G", "value": 22.5}`
- **Detail** (`/food/{id}`) nests them:
  `{"nutrient": {"id": 1003, "unitName": "g"}, "amount": 22.5}`

Unit case differs too — the search endpoint shouts `UG`, the detail endpoint
writes `µg`. Both are handled, because a parser that only knows one shape
fails silently on the other: it finds no nutrients and produces a food with no
nutrition rather than an error.

Nothing here fabricates. A nutrient USDA does not publish is absent from the
result, not zero, and a value whose unit cannot be read is dropped rather than
filed under a guessed scale.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ns.domain.nutrition import canonical_unit, nutrient_for_usda_id
from ns.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ParsedNutrient:
    code: str
    amount_per_100g: Decimal
    unit: str


@dataclass(frozen=True, slots=True)
class ParsedFood:
    fdc_id: int
    description: str
    data_type: str | None
    nutrients: tuple[ParsedNutrient, ...]
    # USDA's own category, kept for provenance. Our FoodCategory is coarser
    # and set by the resolver; this is not silently mapped onto it.
    usda_category: str | None = None

    @property
    def is_generic(self) -> bool:
        """Foundation and SR Legacy carry generic ingredients with refuse
        percentages; Branded carries packaged goods with a UPC."""
        return (self.data_type or "").lower() in {"foundation", "sr legacy", "survey (fndds)"}


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ArithmeticError, ValueError):
        return None


def _entry_fields(entry: dict[str, Any]) -> tuple[int | None, str | None, Any]:
    """Pull (usda id, unit, amount) out of either response shape."""
    nested = entry.get("nutrient")
    if isinstance(nested, dict):
        raw_id = nested.get("id")
        unit = nested.get("unitName")
        amount = entry.get("amount")
    else:
        raw_id = entry.get("nutrientId")
        unit = entry.get("unitName")
        amount = entry.get("value")

    return (raw_id if isinstance(raw_id, int) else None, unit, amount)


def parse_nutrients(entries: list[dict[str, Any]]) -> tuple[ParsedNutrient, ...]:
    """Extract the nutrients we track, at their best available identifier.

    When a food publishes several ids for one nutrient — energy as both 1008
    and the Atwater factors — the one earliest in that nutrient's preference
    order wins.
    """
    best: dict[str, tuple[int, ParsedNutrient]] = {}

    for entry in entries:
        usda_id, raw_unit, raw_amount = _entry_fields(entry)
        if usda_id is None:
            continue

        mapping = nutrient_for_usda_id(usda_id)
        if mapping is None:
            continue
        code, preference = mapping

        # Group headings — `Proximates`, `Lipids` — arrive as nutrients with no
        # amount at all. They are not zeroes.
        amount = _decimal(raw_amount)
        if amount is None or amount < 0:
            continue

        unit = canonical_unit(raw_unit) if isinstance(raw_unit, str) else None
        if unit is None:
            log.warning("usda.unreadable_unit", usda_id=usda_id, unit=raw_unit)
            continue

        existing = best.get(code)
        if existing is None or preference < existing[0]:
            best[code] = (preference, ParsedNutrient(code=code, amount_per_100g=amount, unit=unit))

    # Sorted by nutrient code so a stored food's rows are in a stable order.
    return tuple(value[1] for _, value in sorted(best.items(), key=lambda kv: kv[0]))


def parse_food(payload: dict[str, Any]) -> ParsedFood | None:
    """Turn a search hit or a detail payload into a food, or None if unusable."""
    fdc_id = payload.get("fdcId")
    description = payload.get("description")
    if not isinstance(fdc_id, int) or not isinstance(description, str) or not description.strip():
        return None

    category = payload.get("foodCategory")
    if isinstance(category, dict):
        category = category.get("description")

    return ParsedFood(
        fdc_id=fdc_id,
        description=description.strip(),
        data_type=payload.get("dataType"),
        nutrients=parse_nutrients(payload.get("foodNutrients") or []),
        usda_category=category if isinstance(category, str) else None,
    )
