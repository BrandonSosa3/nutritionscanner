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
    normalized_receipt,
    patch_resolve,
    resolved,
    store_of,
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
    first = await normalized_receipt(session, storage, color="white")
    second = await normalized_receipt(session, storage, color="black")
    assert first.id != second.id
    # Both headers are the same Costco, so store resolution gives them one store.
    store = await store_of(session, first)
    assert second.store_id == store.id

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
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
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
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
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
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
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
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
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
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
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
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
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
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
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
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
    line = next(i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT)

    with pytest.raises(ValueError, match="no resolution to confirm"):
        await confirm_resolution(session, line, store_id=store.id)


async def test_revising_a_label_does_not_create_a_second_example(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Duplicates would weight one line more heavily in every future score."""
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
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
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
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


# ── A label must not freeze its receipt ───────────────────────────────────


async def test_a_labelled_receipt_can_still_be_re_normalised(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """The guarantee at stake: every stage after extract replays from the
    stored extraction. A foreign key from the label to the line item turned
    that into a hard failure once a line had been labelled — normalisation
    replaces line items wholesale, and the delete was refused.
    """
    from ns.pipeline.normalize import normalize_receipt

    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
    line = next(i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT)
    food = await make_food(session, "a food that outlives its line item")

    result = await record_correction(session, line, food_id=food.id, store_id=store.id)
    example_id = result.example.id

    await normalize_receipt(session, receipt)

    # The label survives; only its provenance pointer is cleared.
    surviving = await session.get(EvalExample, example_id)
    assert surviving is not None
    # The delete happened in SQL; the identity map still holds the old row.
    await session.refresh(surviving)
    assert surviving.expected_food_id == food.id
    assert surviving.normalized_text == line.normalized_text
    assert surviving.source_line_item_id is None


async def test_the_correction_still_applies_after_re_normalisation(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Rebuilt line items must pick the correction back up on the next resolve."""
    from ns.pipeline.normalize import normalize_receipt

    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
    line = next(i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT)
    text = line.normalized_text
    food = await make_food(session, "a food that survives a rebuild")
    await record_correction(session, line, food_id=food.id, store_id=store.id)

    await normalize_receipt(session, receipt)
    with patch_resolve():
        await resolve_receipt(session, receipt)

    rebuilt = next(i for i in await lines_of(session, receipt) if i.normalized_text == text)
    assert rebuilt.food_id == food.id
    assert rebuilt.resolution_source is ResolutionSource.CORRECTION_STORE


# ── A rule must not outrank a weight the receipt printed ──────────────────


async def test_a_stored_rule_does_not_override_a_later_receipt_s_own_weight(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """The case that matters for anything sold by weight.

    "Chicken breast comes in 1134 g packs" is a reasonable rule for a shop that
    prints no weight. Next week's pack weighs 1360 g and the receipt says so —
    and that printed figure is this purchase's actual weight. Replaying the
    rule over it would silently substitute a fiction for a measurement on every
    future receipt.
    """
    from decimal import Decimal

    from ns.models.enums import GramsBasis
    from ns.pipeline.resolve import resolve_receipt

    receipt = await normalized_receipt(session, storage, color="white")
    store = await store_of(session, receipt)
    line = next(i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT)
    food = await make_food(session, "chicken breast, for the weight rule")

    await record_correction(
        session,
        line,
        food_id=food.id,
        grams_basis=GramsBasis.PER_PACKAGE,
        grams_value=Decimal("1134"),
        store_id=store.id,
    )

    # A later receipt whose own line states a weight.
    later = await normalized_receipt(session, storage, color="black")
    twin = next(
        i for i in await lines_of(session, later) if i.normalized_text == line.normalized_text
    )
    twin.grams_as_purchased = Decimal("1360.000")
    twin.grams_basis = GramsBasis.FROM_RECEIPT
    await session.flush()

    with patch_resolve():
        await resolve_receipt(session, later)

    resolved = next(i for i in await lines_of(session, later) if i.id == twin.id)
    assert resolved.food_id == food.id  # the identity still applies
    assert resolved.grams_as_purchased == Decimal("1360.000")  # the weight does not
    assert resolved.grams_basis is GramsBasis.FROM_RECEIPT


async def test_a_rule_still_fills_in_a_receipt_that_states_no_weight(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """The rule's whole purpose: fixed packages that receipts never weigh."""
    from decimal import Decimal

    from ns.models.enums import GramsBasis
    from ns.pipeline.resolve import resolve_receipt

    receipt = await normalized_receipt(session, storage, color="white")
    store = await store_of(session, receipt)
    line = next(
        i
        for i in await lines_of(session, receipt)
        if i.kind is LineItemKind.PRODUCT and i.grams_as_purchased is None
    )
    food = await make_food(session, "a fixed-size package")

    await record_correction(
        session,
        line,
        food_id=food.id,
        grams_basis=GramsBasis.PER_PACKAGE,
        grams_value=Decimal("500"),
        store_id=store.id,
    )

    later = await normalized_receipt(session, storage, color="black")
    with patch_resolve():
        await resolve_receipt(session, later)

    twin = next(
        i for i in await lines_of(session, later) if i.normalized_text == line.normalized_text
    )
    assert twin.grams_as_purchased == Decimal("500.000")
    assert twin.grams_basis is GramsBasis.PER_PACKAGE


async def test_correcting_a_line_directly_does_override_its_parsed_weight(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """`BANANAS LOOSE 17KG` is a bin code, not a weight. Looking straight at a
    line and saying so has to win — that is a different act from replaying a
    rule onto a receipt nobody is looking at."""
    from decimal import Decimal

    from ns.models.enums import GramsBasis

    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
    line = next(
        i for i in await lines_of(session, receipt) if i.grams_basis is GramsBasis.FROM_RECEIPT
    )
    food = await make_food(session, "a misread package size")

    await record_correction(
        session,
        line,
        food_id=food.id,
        grams_basis=GramsBasis.PER_PACKAGE,
        grams_value=Decimal("596"),
        store_id=store.id,
    )

    fixed = await session.get(LineItem, line.id)
    assert fixed is not None
    assert fixed.grams_as_purchased == Decimal("596.000")
