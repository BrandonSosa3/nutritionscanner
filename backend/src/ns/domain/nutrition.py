"""The nutrients this system tracks, and how USDA names them.

Every identifier below was read off the live FoodData Central API on
2026-08-18 rather than recalled, because a wrong nutrient number produces a
plausible number attached to the wrong nutrient — the worst possible failure
for a system whose whole purpose is nutritional truth. The two foods checked
were fdcId 2646170 (Foundation, chicken breast) and 171077 (SR Legacy, the
same food), chosen because Foundation and SR Legacy publish different nutrient
sets and the differences matter.

Three things that catch a naive reader of this API:

**Energy has no single identifier.** SR Legacy publishes nutrient 1008
(`Energy`, kcal). Foundation foods often do not, publishing 2048 and 2047
(Atwater specific and general factors) instead. A lookup for 1008 alone
returns nothing for half the database, so energy is resolved through an
ordered fallback.

**Folate has four identifiers.** 1177 is `Folate, total`, 1187 `Folate, food`,
1186 `Folic acid`, and 1190 `Folate, DFE`. Only DFE accounts for the higher
bioavailability of synthetic folic acid, and it is what dietary reference
intakes are stated in, so 1190 is the one that belongs here.

**Some entries are group headings with no amount.** `Proximates`, `Lipids`,
`Minerals`, and `Carbohydrates` appear as nutrients with no `amount` key at
all. They are skipped rather than read as zero.

Absent is not zero. A nutrient USDA does not publish for a food is left out;
writing a zero would be inventing data, and an incomplete total is correct
where a fabricated complete one is not.
"""

from dataclasses import dataclass

# Units as FoodData Central writes them. The detail endpoint returns lowercase
# with the micro sign (`µg`); the search endpoint returns uppercase ASCII
# (`UG`). Both are normalised to the canonical form here.
UNIT_ALIASES: dict[str, str] = {
    "g": "g",
    "mg": "mg",
    "µg": "ug",
    "ug": "ug",
    "mcg": "ug",
    "kcal": "kcal",
    "kj": "kJ",
    "iu": "IU",
}


def canonical_unit(unit: str) -> str | None:
    """The canonical spelling of a USDA unit, or None if unrecognised.

    None rather than a guess: a nutrient whose unit cannot be read is stored
    with no unit at all, which is visible, instead of being silently filed
    under the wrong scale.
    """
    return UNIT_ALIASES.get(unit.strip().lower())


@dataclass(frozen=True, slots=True)
class Nutrient:
    """One nutrient we track, and the USDA identifiers that carry it."""

    code: str  # our stable name, used in FoodNutrient.nutrient_code
    label: str  # for display
    unit: str  # what we store it in
    usda_ids: tuple[int, ...]  # in preference order


# The tracked set. Deliberately not "everything USDA publishes": these are the
# nutrients the adequacy view asks about and the flagship cost-per-nutrient
# ranking sorts by. Adding one later is a data backfill, not a migration,
# because nutrients live in a narrow table (D8).
NUTRIENTS: tuple[Nutrient, ...] = (
    # Energy first — the fallback chain is the reason this table exists.
    Nutrient("energy_kcal", "Energy", "kcal", (1008, 2048, 2047)),
    Nutrient("protein_g", "Protein", "g", (1003,)),
    Nutrient("fat_g", "Total fat", "g", (1004,)),
    Nutrient("carbohydrate_g", "Carbohydrate", "g", (1005,)),
    Nutrient("fiber_g", "Fibre", "g", (1079,)),
    Nutrient("sugars_g", "Total sugars", "g", (2000,)),
    Nutrient("saturated_fat_g", "Saturated fat", "g", (1258,)),
    Nutrient("cholesterol_mg", "Cholesterol", "mg", (1253,)),
    # Minerals.
    Nutrient("calcium_mg", "Calcium", "mg", (1087,)),
    Nutrient("iron_mg", "Iron", "mg", (1089,)),
    Nutrient("magnesium_mg", "Magnesium", "mg", (1090,)),
    Nutrient("phosphorus_mg", "Phosphorus", "mg", (1091,)),
    Nutrient("potassium_mg", "Potassium", "mg", (1092,)),
    Nutrient("sodium_mg", "Sodium", "mg", (1093,)),
    Nutrient("zinc_mg", "Zinc", "mg", (1095,)),
    Nutrient("copper_mg", "Copper", "mg", (1098,)),
    Nutrient("manganese_mg", "Manganese", "mg", (1101,)),
    Nutrient("selenium_ug", "Selenium", "ug", (1103,)),
    # Vitamins.
    Nutrient("vitamin_a_rae_ug", "Vitamin A", "ug", (1106,)),
    Nutrient("vitamin_c_mg", "Vitamin C", "mg", (1162,)),
    Nutrient("vitamin_d_ug", "Vitamin D", "ug", (1114,)),
    Nutrient("vitamin_e_mg", "Vitamin E", "mg", (1109,)),
    Nutrient("vitamin_k_ug", "Vitamin K", "ug", (1185,)),
    Nutrient("thiamin_mg", "Thiamin", "mg", (1165,)),
    Nutrient("riboflavin_mg", "Riboflavin", "mg", (1166,)),
    Nutrient("niacin_mg", "Niacin", "mg", (1167,)),
    Nutrient("vitamin_b6_mg", "Vitamin B6", "mg", (1175,)),
    # 1190 is Folate, DFE — the form dietary reference intakes are stated in.
    Nutrient("folate_dfe_ug", "Folate", "ug", (1190,)),
    Nutrient("vitamin_b12_ug", "Vitamin B12", "ug", (1178,)),
)

BY_CODE: dict[str, Nutrient] = {n.code: n for n in NUTRIENTS}

# USDA id to the nutrient it satisfies, with preference order preserved: an id
# earlier in a nutrient's tuple beats one later, so `Energy` wins over the
# Atwater fallbacks when a food publishes both.
_ID_TO_NUTRIENT: dict[int, tuple[str, int]] = {}
for nutrient in NUTRIENTS:
    for preference, usda_id in enumerate(nutrient.usda_ids):
        _ID_TO_NUTRIENT[usda_id] = (nutrient.code, preference)


def nutrient_for_usda_id(usda_id: int) -> tuple[str, int] | None:
    """The nutrient code an id maps to, with its preference rank.

    A lower rank wins. Returns None for the hundreds of nutrients FoodData
    Central publishes that this system does not track.
    """
    return _ID_TO_NUTRIENT.get(usda_id)


# The flagship view ranks foods by cost per gram of this (see BRIEF).
PROTEIN = "protein_g"
ENERGY = "energy_kcal"
