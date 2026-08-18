"""Corrections: permanent, compounding, and labelled.

Principle 3 says a fix is applied to all future receipts. This checks the
harder half of that promise — that it reaches the past ones too — and that
every fix and every confirmation produces a label the resolver is scored
against.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.models import Correction, EvalExample, LineItem
from ns.models.enums import (
    EvalSplit,
    GramsBasis,
    LabelSource,
    LineItemKind,
    ResolutionSource,
)
from ns.pipeline.corrections import HOLDOUT_EVERY, confirm_resolution, record_correction
from ns.pipeline.resolve import resolve_receipt
from ns.providers.storage import LocalReceiptStorage
from tests.integration.test_extract import patch_call
from tests.integration.test_resolve import (
    lines_of,
    make_food,
    make_store,
    normalized_receipt,
    patch_resolve,
    resolved,
)
from tests.unit.test_images import make_image

pytestmark = pytest.mark.integration


@pytest.fixture
def storage(tmp_path: Path) -> LocalReceiptStorage:
    return LocalReceiptStorage(root=tmp_path / "receipts")


async def _examples(session: AsyncSession, text: str) -> list[EvalExample]:
    rows = await session.execute(
        select(EvalExample).where(col(EvalExample.normalized_text) == text)
    )
    return list(rows.scalars().all())


# ── A fix compounds ───────────────────────────────────────────────────────


async def test_a_correction_reaches_receipts_already_processed(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """A fix that only helped future receipts would leave the user's own
    history disagreeing with their own correction."""
    store = await make_store(session)
    first = await normalized_receipt(session, storage, store=store, color="white")
    second = await normalized_receipt(session, storage, store=store, color="black")
    assert first.id != second.id

    with patch_resolve():  # both left unresolved
        await resolve_receipt(session, first)
        await resolve_receipt(session, second)

    line = next(i for i in await lines_of(session, first) if i.kind is LineItemKind.PRODUCT)
    twin = next(
        i for i in await lines_of(session, second) if i.normalized_text == line.normalized_text
    )
    food = await make_food(session, "chicken breast, boneless skinless, raw")

    result = await record_correction(session, line, food_id=food.id, store_id=store.id)

    # Both receipts, not just the one the fix was made on.
    assert result.applied_to == 2
    for line_id in (line.id, twin.id):
        refreshed = await session.get(LineItem, line_id)
        assert refreshed is not None
        assert refreshed.food_id == food.id
        assert refreshed.resolution_source is ResolutionSource.CORRECTION_STORE
        assert refreshed.confidence == 1.0


async def test_a_correction_is_stored_for_next_time(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    store = await make_store(session)
    receipt = await normalized_receipt(session, storage, store=store)
    line = next(i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT)
    food = await make_food(session, "a corrected food")

    await record_correction(session, line, food_id=food.id, store_id=store.id)

    stored = (
        await session.execute(
            select(Correction).where(
                col(Correction.normalized_text) == line.normalized_text,
                col(Correction.store_id) == store.id,
            )
        )
    ).scalar_one()
    assert stored.food_id == food.id


async def test_correcting_the_same_line_twice_revises_one_correction(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    store = await make_store(session)
    receipt = await normalized_receipt(session, storage, store=store)
    line = next(i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT)
    wrong = await make_food(session, "the wrong food")
    right = await make_food(session, "the right food")

    await record_correction(session, line, food_id=wrong.id, store_id=store.id)
    await record_correction(session, line, food_id=right.id, store_id=store.id)

    stored = (
        (
            await session.execute(
                select(Correction).where(
                    col(Correction.normalized_text) == line.normalized_text,
                    col(Correction.store_id) == store.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(stored) == 1
    assert stored[0].food_id == right.id


async def test_a_global_correction_is_scoped_to_no_store(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    store = await make_store(session)
    receipt = await normalized_receipt(session, storage, store=store)
    line = next(i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT)
    food = await make_food(session, "a globally corrected food")

    result = await record_correction(
        session, line, food_id=food.id, store_id=store.id, global_scope=True
    )

    assert result.correction.store_id is None


async def test_a_correction_stores_a_rule_not_a_weight(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """D3. "Eggs come in 900 g boxes", never "that box weighed 900 g"."""
    store = await make_store(session)
    receipt = await normalized_receipt(session, storage, store=store)
    line = next(
        i
        for i in await lines_of(session, receipt)
        if i.kind is LineItemKind.PRODUCT and i.quantity == Decimal(3)
    )
    food = await make_food(session, "eggs, large, raw")

    await record_correction(
        session,
        line,
        food_id=food.id,
        grams_basis=GramsBasis.PER_PACKAGE,
        grams_value=Decimal("900"),
        store_id=store.id,
    )

    refreshed = await session.get(LineItem, line.id)
    assert refreshed is not None
    # The rule times this line's own count of three, not a stored 900.
    assert refreshed.grams_as_purchased == Decimal("2700.000")


async def test_a_nonfood_correction_needs_no_food(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """ "This is paper towels" is a valid, useful correction."""
    store = await make_store(session)
    receipt = await normalized_receipt(session, storage, store=store)
    line = next(i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT)

    await record_correction(session, line, is_nonfood=True, store_id=store.id)

    refreshed = await session.get(LineItem, line.id)
    assert refreshed is not None
    assert refreshed.resolution_source is ResolutionSource.NONFOOD
    assert refreshed.food_id is None


# ── Every fix is a label (principle 4, D6) ────────────────────────────────


async def test_a_correction_creates_an_eval_example(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    store = await make_store(session)
    receipt = await normalized_receipt(session, storage, store=store)
    line = next(i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT)
    food = await make_food(session, "a labelled food")

    await record_correction(session, line, food_id=food.id, store_id=store.id)

    examples = await _examples(session, line.normalized_text)
    assert len(examples) == 1
    assert examples[0].expected_food_id == food.id
    assert examples[0].label_source is LabelSource.CORRECTED
    assert examples[0].normalizer_version


async def test_a_confirmation_is_a_label_too(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Without these the eval set is entirely the resolver's failures — a
    biased sample that can never show an improvement."""
    store = await make_store(session)
    receipt = await normalized_receipt(session, storage, store=store)
    lines = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]

    with patch_resolve(resolved(lines[0].line_index, "chicken breast, raw")):
        await resolve_receipt(session, receipt)

    line = next(i for i in await lines_of(session, receipt) if i.line_index == lines[0].line_index)
    example = await confirm_resolution(session, line, store_id=store.id)

    assert example.label_source is LabelSource.CONFIRMED
    assert example.expected_food_id == line.food_id
    # A confirmation overrides nothing, so it writes no correction.
    corrections = (
        await session.execute(
            select(Correction).where(col(Correction.normalized_text) == line.normalized_text)
        )
    ).all()
    assert corrections == []


async def test_confirming_an_unresolved_line_is_refused(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """There is nothing to confirm, and recording one would poison the labels."""
    store = await make_store(session)
    receipt = await normalized_receipt(session, storage, store=store)
    line = next(i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT)

    with pytest.raises(ValueError, match="no resolution to confirm"):
        await confirm_resolution(session, line, store_id=store.id)


async def test_revising_a_label_does_not_create_a_second_example(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Duplicates would weight one line more heavily in every future score."""
    store = await make_store(session)
    receipt = await normalized_receipt(session, storage, store=store)
    line = next(i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT)
    wrong = await make_food(session, "the first answer")
    right = await make_food(session, "the second answer")

    await record_correction(session, line, food_id=wrong.id, store_id=store.id)
    await record_correction(session, line, food_id=right.id, store_id=store.id)

    examples = await _examples(session, line.normalized_text)
    assert len(examples) == 1
    assert examples[0].expected_food_id == right.id


async def test_the_split_is_deterministic(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """A random split cannot be reproduced, which makes two runs incomparable
    for reasons that have nothing to do with the resolver."""
    store = await make_store(session)
    receipt = await normalized_receipt(session, storage, store=store)
    lines = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]
    food = await make_food(session, "a food for splitting")

    splits = []
    for line in lines:
        result = await record_correction(session, line, food_id=food.id, store_id=store.id)
        splits.append(result.example.split)

    assert EvalSplit.HOLDOUT in splits
    assert EvalSplit.TRAIN in splits
    # Roughly one in HOLDOUT_EVERY, and never more than that.
    assert splits.count(EvalSplit.HOLDOUT) <= len(splits) // HOLDOUT_EVERY + 1


# ── The endpoints ─────────────────────────────────────────────────────────


async def test_the_correction_endpoint(client: AsyncClient, session: AsyncSession) -> None:
    upload = await client.post(
        "/receipts", files={"file": ("receipt.jpg", make_image(), "image/jpeg")}
    )
    receipt_id = upload.json()["receipt"]["id"]
    with patch_call():
        await client.post(f"/receipts/{receipt_id}/extract")
    await client.post(f"/receipts/{receipt_id}/normalize")
    with patch_resolve(echo=True):
        await client.post(f"/receipts/{receipt_id}/resolve")

    lines = (await client.get(f"/receipts/{receipt_id}/lines")).json()["items"]
    target = next(i for i in lines if i["kind"] == "product")
    food_id = next(i["food_id"] for i in lines if i["food_id"])

    response = await client.post(f"/line-items/{target['id']}/correct", json={"food_id": food_id})

    assert response.status_code == 200
    body = response.json()
    assert body["applied_to_line_items"] >= 1
    assert body["label_source"] == "corrected"
    assert body["split"] in {"train", "holdout"}


async def test_the_confirmation_endpoint(client: AsyncClient) -> None:
    upload = await client.post(
        "/receipts", files={"file": ("receipt.jpg", make_image(), "image/jpeg")}
    )
    receipt_id = upload.json()["receipt"]["id"]
    with patch_call():
        await client.post(f"/receipts/{receipt_id}/extract")
    await client.post(f"/receipts/{receipt_id}/normalize")
    with patch_resolve(echo=True):
        await client.post(f"/receipts/{receipt_id}/resolve")

    lines = (await client.get(f"/receipts/{receipt_id}/lines")).json()["items"]
    target = next(i for i in lines if i["food_id"])

    response = await client.post(f"/line-items/{target['id']}/confirm")

    assert response.status_code == 200
    assert response.json()["label_source"] == "confirmed"


async def test_a_correction_pointing_at_no_food_is_refused(client: AsyncClient) -> None:
    upload = await client.post(
        "/receipts", files={"file": ("receipt.jpg", make_image(), "image/jpeg")}
    )
    receipt_id = upload.json()["receipt"]["id"]
    with patch_call():
        await client.post(f"/receipts/{receipt_id}/extract")
    await client.post(f"/receipts/{receipt_id}/normalize")
    lines = (await client.get(f"/receipts/{receipt_id}/lines")).json()["items"]
    target = next(i for i in lines if i["kind"] == "product")

    empty = await client.post(f"/line-items/{target['id']}/correct", json={})
    assert empty.status_code == 422

    missing = await client.post(f"/line-items/{target['id']}/correct", json={"food_id": 987654})
    assert missing.status_code == 422


async def test_correcting_an_unknown_line_is_a_404(client: AsyncClient) -> None:
    response = await client.post("/line-items/98765432/correct", json={"is_nonfood": True})
    assert response.status_code == 404
