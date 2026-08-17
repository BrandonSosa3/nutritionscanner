"""The schema decisions from DECISIONS.md, verified against a real Postgres.

These assert that the *database* enforces the rules, not that the application
remembers to. A constraint that only exists in Python is a convention; a
constraint in the schema is a guarantee, and this system is meant to hold years
of irreplaceable history.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ns.models import (
    Correction,
    Food,
    FoodNutrient,
    GramsBasis,
    LineItem,
    LineItemKind,
    Receipt,
    Store,
)

pytestmark = pytest.mark.integration


async def _store(session: AsyncSession, name: str) -> Store:
    store = Store(name=name)
    session.add(store)
    await session.flush()
    return store


async def _receipt(session: AsyncSession, sha: str) -> Receipt:
    receipt = Receipt(
        image_sha256=sha,
        image_path=f"data/receipts/{sha}.jpg",
        image_bytes=1024,
        purchased_at=date(2026, 8, 15),
    )
    session.add(receipt)
    await session.flush()
    return receipt


# ── D2: correction keying ─────────────────────────────────────────────────


async def test_two_global_corrections_for_same_text_are_rejected(
    session: AsyncSession,
) -> None:
    """The partial unique index is the whole point of D2.

    Postgres treats NULLs as distinct, so the (normalized_text, store_id)
    unique index alone would happily accept two conflicting global rows and
    tier-1b resolution would match either one.
    """
    session.add(Correction(normalized_text="MILK 2%", store_id=None))
    await session.flush()

    session.add(Correction(normalized_text="MILK 2%", store_id=None))
    with pytest.raises(IntegrityError, match="uq_correction_text_global"):
        await session.flush()


async def test_global_and_store_specific_corrections_coexist(
    session: AsyncSession,
) -> None:
    """The case the brief's original schema made impossible.

    A global default plus a per-store override for the same receipt text is
    the entire reason store_id is nullable.
    """
    store = await _store(session, "Sprouts")

    session.add(Correction(normalized_text="GRN ONION", store_id=None))
    session.add(Correction(normalized_text="GRN ONION", store_id=store.id))
    await session.flush()

    rows = (
        await session.execute(select(Correction).where(Correction.normalized_text == "GRN ONION"))
    ).all()
    assert len(rows) == 2


async def test_duplicate_store_specific_corrections_are_rejected(
    session: AsyncSession,
) -> None:
    store = await _store(session, "Costco")

    session.add(Correction(normalized_text="KS DICED TOM", store_id=store.id))
    await session.flush()

    session.add(Correction(normalized_text="KS DICED TOM", store_id=store.id))
    with pytest.raises(IntegrityError, match="uq_correction_text_store"):
        await session.flush()


async def test_same_text_at_two_stores_is_allowed(session: AsyncSession) -> None:
    """Receipt abbreviations genuinely collide across chains."""
    a = await _store(session, "Store A")
    b = await _store(session, "Store B")

    session.add(Correction(normalized_text="GV MLK 2%", store_id=a.id))
    session.add(Correction(normalized_text="GV MLK 2%", store_id=b.id))
    await session.flush()  # must not raise


# ── D3: corrections carry a gram rule, not a gram number ──────────────────


async def test_correction_stores_a_gram_rule(session: AsyncSession) -> None:
    """A per-package rule is reusable; a raw weight would not be."""
    correction = Correction(
        normalized_text="SPAR COOKING OIL 375ML",
        grams_basis=GramsBasis.PER_PACKAGE,
        grams_value=Decimal("375.000"),
    )
    session.add(correction)
    await session.flush()
    await session.refresh(correction)

    assert correction.grams_basis is GramsBasis.PER_PACKAGE
    assert correction.grams_value == Decimal("375.000")
    # There is deliberately no `grams_override` column to write a one-off
    # weight into.
    assert not hasattr(correction, "grams_override")


# ── D1: money is integer cents ────────────────────────────────────────────


# A rate, not an amount. Cents per 100 g is a computed ratio that genuinely
# needs decimal places; it is the one `_cents` column allowed to be numeric.
RATE_COLUMNS = {"price_cents_per_100g"}


async def test_money_amounts_are_integer_cents(session: AsyncSession) -> None:
    """Float drift eventually fails an arithmetically perfect receipt, and
    that failure is indistinguishable from a real extraction error."""
    result = await session.execute(
        text(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name LIKE '%cents%' "
            "ORDER BY table_name, column_name"
        )
    )
    rows = result.all()
    assert rows, "expected money columns to exist"

    amounts = [(t, c, d) for t, c, d in rows if c not in RATE_COLUMNS]
    assert amounts, "expected at least one money amount column"
    for table, column, data_type in amounts:
        assert data_type == "integer", (
            f"{table}.{column} is {data_type}; money amounts must be integer cents"
        )

    rates = [(t, c, d) for t, c, d in rows if c in RATE_COLUMNS]
    for table, column, data_type in rates:
        assert data_type == "numeric", f"{table}.{column} is a rate and should be numeric"


# ── Structural integrity ──────────────────────────────────────────────────


async def test_receipt_image_hash_is_unique(session: AsyncSession) -> None:
    """Ingestion is idempotent on image hash — re-upload updates, never duplicates."""
    await _receipt(session, "a" * 64)
    session.add(
        Receipt(image_sha256="a" * 64, image_path="data/receipts/dupe.jpg", image_bytes=2048)
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_line_item_order_is_unique_per_receipt(session: AsyncSession) -> None:
    receipt = await _receipt(session, "b" * 64)
    for _ in range(2):
        session.add(
            LineItem(
                receipt_id=receipt.id,  # type: ignore[arg-type]
                line_index=0,
                raw_text="BROCCOLI",
                normalized_text="broccoli",
                normalizer_version="v1",
                price_cents=484,
            )
        )
    with pytest.raises(IntegrityError, match="uq_line_item_receipt_index"):
        await session.flush()


async def test_deleting_a_receipt_cascades_to_line_items(session: AsyncSession) -> None:
    receipt = await _receipt(session, "c" * 64)
    session.add(
        LineItem(
            receipt_id=receipt.id,  # type: ignore[arg-type]
            line_index=0,
            raw_text="ZUCHINNI GREEN",
            normalized_text="zucchini",
            normalizer_version="v1",
            price_cents=466,
            kind=LineItemKind.PRODUCT,
        )
    )
    await session.flush()

    await session.delete(receipt)
    await session.flush()

    remaining = (
        await session.execute(select(LineItem).where(LineItem.receipt_id == receipt.id))
    ).all()
    assert remaining == []


async def test_enum_columns_reject_unknown_values(session: AsyncSession) -> None:
    """VARCHAR + CHECK still gives database-level integrity."""
    receipt = await _receipt(session, "d" * 64)
    with pytest.raises(DBAPIError):
        await session.execute(
            text(
                "INSERT INTO line_item "
                "(receipt_id, line_index, raw_text, normalized_text, "
                " normalizer_version, price_cents, kind, grams_basis, "
                " resolution_source, discount_cents) "
                "VALUES (:rid, 0, 'X', 'x', 'v1', 100, 'not_a_real_kind', "
                "'unknown', 'unresolved', 0)"
            ),
            {"rid": receipt.id},
        )


async def test_food_nutrient_is_unique_per_food_and_code(session: AsyncSession) -> None:
    food = Food(canonical_name="Broccoli, raw", edible_portion_pct=Decimal("100"))
    session.add(food)
    await session.flush()

    for _ in range(2):
        session.add(
            FoodNutrient(
                food_id=food.id,  # type: ignore[arg-type]
                nutrient_code="protein",
                amount_per_100g=Decimal("2.820"),
                unit="g",
            )
        )
    with pytest.raises(IntegrityError, match="uq_food_nutrient_food_code"):
        await session.flush()


async def test_timestamps_are_timezone_aware(session: AsyncSession) -> None:
    """Naive datetimes silently misattribute a purchase across a date boundary."""
    receipt = await _receipt(session, "e" * 64)
    await session.refresh(receipt)
    assert receipt.created_at.tzinfo is not None
    assert receipt.created_at.astimezone(UTC) <= datetime.now(UTC)
