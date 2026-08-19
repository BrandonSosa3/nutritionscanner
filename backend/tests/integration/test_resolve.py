"""Resolution: corrections, then the model, then an honest unresolved state.

The model is mocked throughout. One test at the bottom of test_extract.py
makes a real call; nothing here spends money.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.models import Correction, Food, LineItem, LlmCall, Receipt, Store
from ns.models.enums import (
    GramsBasis,
    LineItemKind,
    LlmStage,
    PipelineStatus,
    ResolutionSource,
)
from ns.pipeline.ingest import ingest_receipt
from ns.pipeline.normalize import normalize_receipt
from ns.pipeline.resolve import resolve_receipt
from ns.providers.anthropic.client import CallResult
from ns.providers.anthropic.schemas import ResolutionBatch, ResolvedLine
from ns.providers.storage import LocalReceiptStorage
from tests.integration.test_extract import patch_call
from tests.unit.test_images import make_image

pytestmark = pytest.mark.integration


@pytest.fixture
def storage(tmp_path: Path) -> LocalReceiptStorage:
    return LocalReceiptStorage(root=tmp_path / "receipts")


def resolved(
    line_index: int,
    name: str | None = "chicken breast, boneless skinless, raw",
    *,
    category: str = "protein",
    is_nonfood: bool = False,
    grams: str | None = None,
    basis: str = "unknown",
    confidence: float = 0.95,
) -> ResolvedLine:
    return ResolvedLine(
        line_index=line_index,
        canonical_name=name,
        category=category,  # type: ignore[arg-type]
        is_nonfood=is_nonfood,
        grams_estimate=grams,
        grams_basis=basis,  # type: ignore[arg-type]
        confidence=confidence,
    )


def patch_resolve(*answers: ResolvedLine, echo: bool = False) -> Any:
    """Replace the batched resolution call, still writing the LlmCall row.

    `echo=True` answers every line it is given, which is what a well-behaved
    model does; the explicit form is for testing what happens when it doesn't.
    """

    async def _fake(session: AsyncSession, **kwargs: Any) -> CallResult[ResolutionBatch]:
        items = list(answers)
        if echo:
            text = str(kwargs["content"][0]["text"])
            indices = [
                int(part.split("|")[0].strip())
                for part in text.splitlines()
                if part.strip() and part.split("|")[0].strip().isdigit()
            ]
            items = [resolved(i, f"food {i}") for i in indices]

        session.add(
            LlmCall(
                receipt_id=kwargs.get("receipt_id"),
                # Whatever the caller asked for, like the real client. The
                # harness passes EVAL so measuring the resolver stays
                # separable from running it.
                stage=kwargs.get("stage", LlmStage.RESOLVE),
                model="claude-opus-5",
                prompt_version="res123456789",
                input_tokens=2000,
                output_tokens=800,
                latency_ms=3100,
                cost_usd=Decimal("0.021000"),
                ok=True,
            )
        )
        await session.flush()
        return CallResult(
            parsed=ResolutionBatch(items=items),
            cost_usd=Decimal("0.021000"),
            latency_ms=3100,
            input_tokens=2000,
            output_tokens=800,
            cache_read_tokens=0,
            cache_write_tokens=0,
            stop_reason="end_turn",
            model="claude-opus-5",
            prompt_version="res123456789",
        )

    return patch("ns.pipeline.resolve.call_structured", new=AsyncMock(side_effect=_fake))


async def normalized_receipt(
    session: AsyncSession, storage: LocalReceiptStorage, *, color: str = "white"
) -> Receipt:
    """A receipt through extract and normalize, ready to resolve.

    The store comes from normalisation, which reads it off the extraction — so
    a test using `store_of` is exercising the real chain rather than a store
    it attached by hand.

    `color` varies the image so a test needing two distinct receipts gets two.
    Ingestion is idempotent on content hash, so the same bytes are one receipt
    however many times they are uploaded.
    """
    from ns.pipeline.extract import extract_receipt

    receipt = (await ingest_receipt(session, make_image(color=color), storage=storage)).receipt
    with patch_call():
        await extract_receipt(session, receipt, storage=storage)
    await normalize_receipt(session, receipt)
    return receipt


async def store_of(session: AsyncSession, receipt: Receipt) -> Store:
    """The store normalisation resolved for this receipt."""
    store = await session.get(Store, receipt.store_id)
    if store is None:  # pragma: no cover - a normalised fixture always has one
        raise AssertionError("normalisation did not attach a store")
    return store


async def lines_of(session: AsyncSession, receipt: Receipt) -> list[LineItem]:
    rows = await session.execute(
        select(LineItem)
        .where(col(LineItem.receipt_id) == receipt.id)
        .order_by(col(LineItem.line_index))
    )
    return list(rows.scalars().all())


async def make_store(session: AsyncSession, name: str = "Costco") -> Store:
    store = Store(name=name)
    session.add(store)
    await session.flush()
    return store


async def make_food(session: AsyncSession, name: str) -> Food:
    """Get or create, like production does.

    Creating unconditionally assumes an empty database — which stops being
    true the moment a real receipt is resolved and its foods are committed.
    """
    existing = (
        await session.execute(select(Food).where(col(Food.canonical_name) == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    food = Food(canonical_name=name)
    session.add(food)
    await session.flush()
    return food


# ── Tier 2: the model ─────────────────────────────────────────────────────


async def test_the_model_resolves_products_and_creates_foods(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await normalized_receipt(session, storage)

    with patch_resolve(echo=True):
        result = await resolve_receipt(session, receipt)

    assert result.by_source[ResolutionSource.LLM.value] == 9
    assert result.coverage == 1.0
    assert receipt.status is PipelineStatus.COMPLETE

    items = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]
    assert all(i.food_id is not None for i in items)
    assert all(i.resolution_source is ResolutionSource.LLM for i in items)


async def test_one_call_covers_the_whole_basket(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Batching is the difference between cents and dollars per receipt."""
    receipt = await normalized_receipt(session, storage)
    before = (
        await session.execute(select(LlmCall).where(col(LlmCall.stage) == LlmStage.RESOLVE))
    ).all()

    with patch_resolve(echo=True):
        await resolve_receipt(session, receipt)

    after = (
        await session.execute(select(LlmCall).where(col(LlmCall.stage) == LlmStage.RESOLVE))
    ).all()
    assert len(after) - len(before) == 1


async def test_the_same_food_on_two_lines_reuses_one_row(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Two rows for one food would split its price history in half."""
    receipt = await normalized_receipt(session, storage)
    items = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]

    with patch_resolve(*[resolved(i.line_index, "tomatoes, diced, canned") for i in items]):
        await resolve_receipt(session, receipt)

    food_ids = {i.food_id for i in await lines_of(session, receipt) if i.food_id is not None}
    assert len(food_ids) == 1


async def test_a_name_differing_only_in_case_and_spacing_is_the_same_food(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await normalized_receipt(session, storage)
    items = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]

    with patch_resolve(
        resolved(items[0].line_index, "Tomatoes,  Diced, Canned"),
        resolved(items[1].line_index, "tomatoes, diced, canned"),
    ):
        await resolve_receipt(session, receipt)

    refreshed = await lines_of(session, receipt)
    first = next(i for i in refreshed if i.line_index == items[0].line_index)
    second = next(i for i in refreshed if i.line_index == items[1].line_index)
    assert first.food_id == second.food_id


# ── Tier 3: honest refusal ────────────────────────────────────────────────


async def test_a_null_name_leaves_the_line_unresolved(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Principle 2: an unresolved line is correct, a guessed one is not."""
    receipt = await normalized_receipt(session, storage)
    items = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]

    with patch_resolve(resolved(items[0].line_index, None, confidence=0.1)):
        result = await resolve_receipt(session, receipt)

    assert result.by_source[ResolutionSource.UNRESOLVED.value] >= 1
    assert receipt.status is PipelineStatus.NEEDS_REVIEW
    first = next(i for i in await lines_of(session, receipt) if i.line_index == items[0].line_index)
    assert first.food_id is None
    assert first.resolution_source is ResolutionSource.UNRESOLVED


async def test_a_low_confidence_answer_is_not_an_answer(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Below the floor the line goes to the correction queue instead."""
    receipt = await normalized_receipt(session, storage)
    items = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]

    with patch_resolve(resolved(items[0].line_index, "some food", confidence=0.2)):
        await resolve_receipt(session, receipt)

    first = next(i for i in await lines_of(session, receipt) if i.line_index == items[0].line_index)
    assert first.food_id is None
    assert first.resolution_source is ResolutionSource.UNRESOLVED
    # The number is kept even though it was rejected — the eval harness reads it.
    assert first.confidence == 0.2


async def test_a_line_the_model_omits_stays_unresolved(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Silence is not an answer. A dropped line must not be quietly skipped."""
    receipt = await normalized_receipt(session, storage)

    with patch_resolve():  # answers nothing at all
        result = await resolve_receipt(session, receipt)

    assert result.by_source[ResolutionSource.UNRESOLVED.value] == 9
    assert result.coverage == 0.0


# ── Fees are not sent to the model ────────────────────────────────────────


async def test_a_fee_is_nonfood_without_a_model_call(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Paying to be told a carrier bag is not food would be absurd."""
    receipt = await normalized_receipt(session, storage)
    session.add(
        LineItem(
            receipt_id=receipt.id,
            line_index=900,
            raw_text="CARRIER BAG 24L",
            normalized_text="carrier bag",
            normalizer_version="v1",
            kind=LineItemKind.FEE,
            price_cents=75,
        )
    )
    await session.flush()

    with patch_resolve(echo=True) as mock:
        await resolve_receipt(session, receipt)
        sent = str(mock.call_args.kwargs["content"][0]["text"])

    assert "carrier bag" not in sent
    fee = next(i for i in await lines_of(session, receipt) if i.line_index == 900)
    assert fee.resolution_source is ResolutionSource.NONFOOD


async def test_tax_and_total_lines_are_left_alone(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await normalized_receipt(session, storage)

    with patch_resolve(echo=True) as mock:
        await resolve_receipt(session, receipt)
        sent = str(mock.call_args.kwargs["content"][0]["text"])

    assert "subtotal" not in sent
    assert "total tax" not in sent
    summaries = [
        i
        for i in await lines_of(session, receipt)
        if i.kind in {LineItemKind.SUBTOTAL, LineItemKind.TOTAL, LineItemKind.TAX}
    ]
    assert summaries
    assert all(i.resolution_source is ResolutionSource.UNRESOLVED for i in summaries)
    assert all(i.food_id is None for i in summaries)


# ── Tier 1: corrections win ───────────────────────────────────────────────


async def test_a_correction_resolves_without_a_model_call(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
    food = await make_food(session, "chicken breast, boneless skinless, raw")
    items = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]

    session.add(
        Correction(
            normalized_text=items[0].normalized_text,
            store_id=store.id,
            food_id=food.id,
            grams_basis=GramsBasis.PER_PACKAGE,
            grams_value=Decimal("907.185"),
        )
    )
    await session.flush()

    with patch_resolve(echo=True) as mock:
        await resolve_receipt(session, receipt)
        sent = str(mock.call_args.kwargs["content"][0]["text"])

    assert items[0].normalized_text not in sent
    first = next(i for i in await lines_of(session, receipt) if i.line_index == items[0].line_index)
    assert first.food_id == food.id
    assert first.resolution_source is ResolutionSource.CORRECTION_STORE
    assert first.confidence == 1.0
    assert first.grams_as_purchased == Decimal("907.185")


async def test_a_store_correction_beats_a_global_one(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """`KS DICED TOM` means something at Costco it does not mean elsewhere."""
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
    specific = await make_food(session, "the store specific food")
    fallback = await make_food(session, "the global fallback food")
    items = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]

    session.add_all(
        [
            Correction(
                normalized_text=items[0].normalized_text, store_id=None, food_id=fallback.id
            ),
            Correction(
                normalized_text=items[0].normalized_text, store_id=store.id, food_id=specific.id
            ),
        ]
    )
    await session.flush()

    with patch_resolve(echo=True):
        await resolve_receipt(session, receipt)

    first = next(i for i in await lines_of(session, receipt) if i.line_index == items[0].line_index)
    assert first.food_id == specific.id
    assert first.resolution_source is ResolutionSource.CORRECTION_STORE


async def test_a_global_correction_applies_when_no_store_one_matches(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await normalized_receipt(session, storage)
    fallback = await make_food(session, "the global fallback food")
    items = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]

    session.add(
        Correction(normalized_text=items[0].normalized_text, store_id=None, food_id=fallback.id)
    )
    await session.flush()

    with patch_resolve(echo=True):
        await resolve_receipt(session, receipt)

    first = next(i for i in await lines_of(session, receipt) if i.line_index == items[0].line_index)
    assert first.resolution_source is ResolutionSource.CORRECTION_GLOBAL


async def test_a_correction_at_another_store_does_not_apply(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await normalized_receipt(session, storage)
    elsewhere = await make_store(session, "Sprouts")
    food = await make_food(session, "the other store's food")
    items = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]

    session.add(
        Correction(normalized_text=items[0].normalized_text, store_id=elsewhere.id, food_id=food.id)
    )
    await session.flush()

    with patch_resolve(echo=True):
        await resolve_receipt(session, receipt)

    first = next(i for i in await lines_of(session, receipt) if i.line_index == items[0].line_index)
    assert first.food_id != food.id
    assert first.resolution_source is ResolutionSource.LLM


async def test_a_nonfood_correction_marks_the_line_nonfood(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
    items = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]

    session.add(
        Correction(normalized_text=items[0].normalized_text, store_id=store.id, is_nonfood=True)
    )
    await session.flush()

    with patch_resolve(echo=True):
        await resolve_receipt(session, receipt)

    first = next(i for i in await lines_of(session, receipt) if i.line_index == items[0].line_index)
    assert first.resolution_source is ResolutionSource.NONFOOD


# ── Idempotency ───────────────────────────────────────────────────────────


async def test_re_running_does_not_pay_to_re_answer(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await normalized_receipt(session, storage)

    with patch_resolve(echo=True):
        await resolve_receipt(session, receipt)
    with patch_resolve(echo=True) as mock:
        result = await resolve_receipt(session, receipt)

    assert mock.await_count == 0
    assert result.call is None
    assert result.coverage == 1.0


async def test_force_re_resolves_everything(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """How a prompt revision reaches a receipt already on file."""
    receipt = await normalized_receipt(session, storage)

    with patch_resolve(echo=True):
        await resolve_receipt(session, receipt)
    with patch_resolve(echo=True) as mock:
        await resolve_receipt(session, receipt, force=True)

    assert mock.await_count == 1


async def test_re_running_picks_up_a_correction_added_since(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """The loop: an unresolved line, a fix, and a free re-run that uses it."""
    receipt = await normalized_receipt(session, storage)
    store = await store_of(session, receipt)
    items = [i for i in await lines_of(session, receipt) if i.kind is LineItemKind.PRODUCT]

    with patch_resolve():  # everything comes back unresolved
        await resolve_receipt(session, receipt)
    assert receipt.status is PipelineStatus.NEEDS_REVIEW

    food = await make_food(session, "the corrected food")
    session.add(
        Correction(normalized_text=items[0].normalized_text, store_id=store.id, food_id=food.id)
    )
    await session.flush()

    with patch_resolve():
        result = await resolve_receipt(session, receipt)

    assert result.by_source[ResolutionSource.CORRECTION_STORE.value] == 1
    first = next(i for i in await lines_of(session, receipt) if i.line_index == items[0].line_index)
    assert first.food_id == food.id


# ── Grams ─────────────────────────────────────────────────────────────────


async def test_a_receipt_weight_survives_resolution(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """`MONT JACK 2#` is 907.185 g from the paper. No estimate overrides it."""
    receipt = await normalized_receipt(session, storage)
    weighed = next(
        i for i in await lines_of(session, receipt) if i.grams_basis is GramsBasis.FROM_RECEIPT
    )

    with patch_resolve(
        resolved(weighed.line_index, "cheese, monterey jack", grams="500", basis="per_package")
    ):
        await resolve_receipt(session, receipt)

    refreshed = next(
        i for i in await lines_of(session, receipt) if i.line_index == weighed.line_index
    )
    assert refreshed.grams_as_purchased == Decimal("907.185")


async def test_an_estimated_weight_is_recorded_with_its_basis(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await normalized_receipt(session, storage)
    items = [
        i
        for i in await lines_of(session, receipt)
        if i.kind is LineItemKind.PRODUCT and i.grams_as_purchased is None
    ]

    with patch_resolve(
        resolved(items[0].line_index, "eggs, large", grams="900", basis="per_package")
    ):
        await resolve_receipt(session, receipt)

    first = next(i for i in await lines_of(session, receipt) if i.line_index == items[0].line_index)
    assert first.grams_as_purchased == Decimal("900.000")
    assert first.grams_basis is GramsBasis.PER_PACKAGE


async def test_a_nonsense_gram_string_is_refused_not_coerced(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await normalized_receipt(session, storage)
    items = [
        i
        for i in await lines_of(session, receipt)
        if i.kind is LineItemKind.PRODUCT and i.grams_as_purchased is None
    ]

    with patch_resolve(
        resolved(items[0].line_index, "some food", grams="about 900", basis="per_package")
    ):
        await resolve_receipt(session, receipt)

    first = next(i for i in await lines_of(session, receipt) if i.line_index == items[0].line_index)
    assert first.grams_as_purchased is None
    # The identity still stands; only the weight was unusable.
    assert first.food_id is not None


# ── The endpoints ─────────────────────────────────────────────────────────


async def test_resolve_and_lines_endpoints(client: AsyncClient) -> None:
    upload = await client.post(
        "/receipts", files={"file": ("receipt.jpg", make_image(), "image/jpeg")}
    )
    receipt_id = upload.json()["receipt"]["id"]

    with patch_call():
        await client.post(f"/receipts/{receipt_id}/extract")
    await client.post(f"/receipts/{receipt_id}/normalize")

    with patch_resolve(echo=True):
        response = await client.post(f"/receipts/{receipt_id}/resolve")

    assert response.status_code == 200
    body = response.json()
    assert body["coverage"] == 1.0
    assert body["status"] == "complete"
    assert body["cost_usd"] == "0.021000"

    lines = await client.get(f"/receipts/{receipt_id}/lines")
    assert lines.status_code == 200
    payload = lines.json()
    assert payload["coverage"] == 1.0
    named = [i for i in payload["items"] if i["food_name"]]
    assert len(named) == 9


async def test_resolving_an_unknown_receipt_is_a_404(client: AsyncClient) -> None:
    assert (await client.post("/receipts/98765432/resolve")).status_code == 404


# ── The prompt has to agree with the arithmetic ───────────────────────────


def test_the_prompt_asks_for_a_per_item_figure_not_a_line_total() -> None:
    """A real defect: the prompt asked for the line total while the code
    treated the answer as a per-package rule and multiplied by quantity again.
    Three boxes of 18 eggs came back as 8.1 kg."""
    from ns.providers.anthropic.client import load_prompt

    text = " ".join(load_prompt("resolve_v1").text.lower().split())
    assert "never the line total" in text
    assert "multiplies your figure by that quantity" in text


def test_the_prompt_states_the_never_invent_rule() -> None:
    from ns.providers.anthropic.client import load_prompt

    text = " ".join(load_prompt("resolve_v1").text.lower().split())
    assert "never invent" in text
    assert "null" in text


async def test_a_per_package_estimate_is_multiplied_by_the_line_s_quantity(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """`3 @ 4.29` of 900 g egg boxes is 2700 g, from a rule of 900."""
    receipt = await normalized_receipt(session, storage)
    eggs = next(
        i
        for i in await lines_of(session, receipt)
        if i.kind is LineItemKind.PRODUCT and i.quantity == Decimal(3)
    )

    with patch_resolve(
        resolved(eggs.line_index, "eggs, chicken, whole, raw", grams="900", basis="per_package")
    ):
        await resolve_receipt(session, receipt)

    refreshed = next(i for i in await lines_of(session, receipt) if i.line_index == eggs.line_index)
    assert refreshed.grams_as_purchased == Decimal("2700.000")
