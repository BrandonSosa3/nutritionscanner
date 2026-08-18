"""Extraction.

The API is mocked by default so the suite costs nothing and runs offline.
One test at the bottom makes a real call and is deselected unless explicitly
requested with `-m llm`.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ns.models import LlmCall, Receipt
from ns.models.enums import PipelineStatus
from ns.pipeline.extract import extract_receipt
from ns.pipeline.ingest import ingest_receipt
from ns.providers.anthropic.budget import BudgetExceededError
from ns.providers.anthropic.client import CallResult, load_prompt
from ns.providers.anthropic.schemas import ExtractedLineItem, ExtractedReceipt
from ns.providers.storage import LocalReceiptStorage
from tests.conftest import RECEIPT_FIXTURES
from tests.unit.test_images import make_image

pytestmark = pytest.mark.integration


@pytest.fixture
def storage(tmp_path: Path) -> LocalReceiptStorage:
    return LocalReceiptStorage(root=tmp_path / "receipts")


def fake_extraction() -> ExtractedReceipt:
    """The Costco fixture, transcribed as the model actually transcribed it.

    This is the real receipt 04-us-costco.png, copied from a live extraction
    rather than invented, so its arithmetic is genuinely the paper's: the nine
    products sum to exactly 85.61, tax is 3.52, and the total is 89.13. Two
    tax rate lines are printed alongside a summary tax line, the `3 @ 4.29`
    multiplier sits on its own line above the eggs, and the item count of 11
    is 9 printed lines with the eggs counted three times.

    Every downstream stage reads this, so keeping it faithful means the tests
    exercise real messiness rather than a shape that was convenient to write.
    """
    return ExtractedReceipt(
        store_name="COSTCO WHOLESALE",
        store_location="Thornton #629",
        currency="USD",
        purchased_at="2016-04-20",
        line_items=[
            ExtractedLineItem(
                line_index=0,
                raw_text="E FF BS BREAST",
                amount="23.99",
                kind="product",
                item_code="673919",
            ),
            ExtractedLineItem(
                line_index=1,
                raw_text="E KS DICED TOM",
                amount="6.49",
                kind="product",
                item_code="633561",
            ),
            ExtractedLineItem(
                line_index=2,
                raw_text="E JACKORGSALSA",
                amount="2.97",
                kind="product",
                item_code="967596",
            ),
            ExtractedLineItem(
                line_index=3,
                raw_text="3 @ 4.29",
                amount=None,
                kind="unknown",
                quantity="3",
                unit_price="4.29",
            ),
            ExtractedLineItem(
                line_index=4,
                raw_text="18CT EGGS",
                amount="12.87",
                kind="product",
                quantity="3",
                unit_price="4.29",
                item_code="878137",
            ),
            ExtractedLineItem(
                line_index=5,
                raw_text="E GRAPE TOMATO",
                amount="6.29",
                kind="product",
                item_code="77053",
            ),
            ExtractedLineItem(
                line_index=6,
                raw_text="ECO HALF PAN",
                amount="6.49",
                kind="product",
                item_code="404609",
            ),
            ExtractedLineItem(
                line_index=7,
                raw_text="E GRND TURKEY",
                amount="18.47",
                kind="product",
                item_code="55992",
            ),
            ExtractedLineItem(
                line_index=8,
                raw_text="E CHPD ONION",
                amount="3.59",
                kind="product",
                item_code="263423",
            ),
            ExtractedLineItem(
                line_index=9,
                raw_text="E MONT JACK 2#",
                amount="4.45",
                kind="product",
                item_code="22101",
            ),
            ExtractedLineItem(
                line_index=10,
                raw_text="SUBTOTAL",
                amount="85.61",
                kind="subtotal",
            ),
            ExtractedLineItem(
                line_index=11,
                raw_text="TAX",
                amount="3.52",
                kind="tax",
            ),
            ExtractedLineItem(
                line_index=12,
                raw_text="**** TOTAL",
                amount="89.13",
                kind="total",
            ),
            ExtractedLineItem(
                line_index=13,
                raw_text="Check/Member Prntd",
                amount="89.13",
                kind="payment",
            ),
            ExtractedLineItem(
                line_index=14,
                raw_text="CHANGE",
                amount="0.00",
                kind="payment",
            ),
            ExtractedLineItem(
                line_index=15,
                raw_text="A 8.50% TAX",
                amount="0.55",
                kind="tax",
            ),
            ExtractedLineItem(
                line_index=16,
                raw_text="E 3.75% TAX",
                amount="2.97",
                kind="tax",
            ),
            ExtractedLineItem(
                line_index=17,
                raw_text="TOTAL TAX",
                amount="3.52",
                kind="tax",
            ),
        ],
        subtotal="85.61",
        tax_total="3.52",
        total="89.13",
        item_count_stated=11,
        legibility="clear",
    )


def fake_call(parsed: ExtractedReceipt) -> CallResult[ExtractedReceipt]:
    return CallResult(
        parsed=parsed,
        cost_usd=Decimal("0.092500"),
        latency_ms=4200,
        input_tokens=6000,
        output_tokens=2500,
        cache_read_tokens=0,
        cache_write_tokens=0,
        stop_reason="end_turn",
        model="claude-opus-5",
        prompt_version="abc123def456",
    )


def patch_call(parsed: ExtractedReceipt | None = None) -> Any:
    """Replace the API call, still writing the LlmCall row the real one would."""
    extraction = parsed or fake_extraction()

    async def _fake(session: AsyncSession, **kwargs: Any) -> CallResult[ExtractedReceipt]:
        from ns.models.enums import LlmStage

        session.add(
            LlmCall(
                receipt_id=kwargs.get("receipt_id"),
                stage=LlmStage.EXTRACT,
                model="claude-opus-5",
                prompt_version="abc123def456",
                input_tokens=6000,
                output_tokens=2500,
                latency_ms=4200,
                cost_usd=Decimal("0.092500"),
                ok=True,
            )
        )
        await session.flush()
        return fake_call(extraction)

    return patch("ns.pipeline.extract.call_structured", new=AsyncMock(side_effect=_fake))


async def _ingest(session: AsyncSession, storage: LocalReceiptStorage) -> Receipt:
    return (await ingest_receipt(session, make_image(), storage=storage)).receipt


# ── The prompt ────────────────────────────────────────────────────────────


def test_prompt_loads_and_is_content_versioned() -> None:
    prompt = load_prompt("extract_v1")
    assert len(prompt.version) == 12
    assert load_prompt("extract_v1").version == prompt.version


def test_prompt_states_the_never_invent_rule() -> None:
    """Principle 2 has to survive in the prompt, not just the docs."""
    text = load_prompt("extract_v1").text.lower()
    assert "never invent" in text
    assert "null" in text


def test_missing_prompt_names_the_available_ones() -> None:
    with pytest.raises(FileNotFoundError, match="extract_v1"):
        load_prompt("does_not_exist")


# ── The stage ─────────────────────────────────────────────────────────────


async def test_extraction_stores_the_transcription_verbatim(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await _ingest(session, storage)

    with patch_call():
        outcome = await extract_receipt(session, receipt, storage=storage)

    assert receipt.raw_extraction is not None
    assert receipt.raw_extraction["store_name"] == "COSTCO WHOLESALE"
    assert len(receipt.raw_extraction["line_items"]) == 18
    # Amounts stay as printed strings — parsing to cents is normalisation's job.
    assert receipt.raw_extraction["total"] == "89.13"
    assert outcome.extraction.total == "89.13"


async def test_extraction_records_provenance(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Replaying a stage later requires knowing which model and prompt ran."""
    receipt = await _ingest(session, storage)

    with patch_call():
        await extract_receipt(session, receipt, storage=storage)

    assert receipt.extraction_model == "claude-opus-5"
    assert receipt.extraction_prompt_version == "abc123def456"
    assert receipt.extracted_at is not None
    # Transcribed, not yet normalised — there are no line items until
    # normalisation runs, and the status must not claim otherwise.
    assert receipt.status is PipelineStatus.EXTRACTED


async def test_extraction_adopts_the_receipt_currency(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await _ingest(session, storage)
    foreign = fake_extraction()
    foreign.currency = "ZAR"

    with patch_call(foreign):
        await extract_receipt(session, receipt, storage=storage)

    assert receipt.currency == "ZAR"


async def test_extraction_logs_cost_and_tokens(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Every LLM call logs model, tokens, latency, and cost."""
    receipt = await _ingest(session, storage)

    with patch_call():
        await extract_receipt(session, receipt, storage=storage)

    calls = (
        (await session.execute(select(LlmCall).where(LlmCall.receipt_id == receipt.id)))
        .scalars()
        .all()
    )

    assert len(calls) == 1
    assert calls[0].cost_usd == Decimal("0.092500")
    assert calls[0].input_tokens == 6000
    assert calls[0].ok is True


async def test_reextraction_is_refused_without_force(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Re-sending an already-extracted receipt costs money and yields nothing."""
    receipt = await _ingest(session, storage)

    with patch_call():
        await extract_receipt(session, receipt, storage=storage)

        with pytest.raises(ValueError, match="force=True"):
            await extract_receipt(session, receipt, storage=storage)


async def test_force_reextraction_is_allowed(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """How a prompt revision gets evaluated against a receipt already on file."""
    receipt = await _ingest(session, storage)

    with patch_call():
        await extract_receipt(session, receipt, storage=storage)
        await extract_receipt(session, receipt, storage=storage, force=True)

    count = (
        await session.execute(
            select(func.count()).select_from(LlmCall).where(LlmCall.receipt_id == receipt.id)
        )
    ).scalar_one()
    assert count == 2


# ── Failure is never destructive ──────────────────────────────────────────


async def test_api_failure_leaves_the_receipt_retryable(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Graceful degradation: never lose a receipt."""
    receipt = await _ingest(session, storage)
    image_path = receipt.image_path

    with (
        patch(
            "ns.pipeline.extract.call_structured",
            new=AsyncMock(side_effect=RuntimeError("connection reset")),
        ),
        pytest.raises(RuntimeError),
    ):
        await extract_receipt(session, receipt, storage=storage)

    assert receipt.status is PipelineStatus.EXTRACT_FAILED
    assert receipt.raw_extraction is None
    # The image is untouched, so a retry needs no re-photographing.
    assert storage.exists(image_path)


async def test_budget_exhaustion_blocks_before_spending(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """The guard refuses at our boundary, with the receipt still queued."""
    receipt = await _ingest(session, storage)

    with (
        patch(
            "ns.pipeline.extract.call_structured",
            new=AsyncMock(side_effect=BudgetExceededError("over budget")),
        ),
        pytest.raises(BudgetExceededError),
    ):
        await extract_receipt(session, receipt, storage=storage)

    assert receipt.status is PipelineStatus.EXTRACT_FAILED
    assert storage.exists(receipt.image_path)


# ── A real call, deselected by default ────────────────────────────────────


@pytest.mark.llm
async def test_real_extraction_of_the_costco_receipt(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Spends real money. Run deliberately: pytest -m llm

    Asserts only what the receipt unambiguously shows, so a prompt revision
    that changes wording does not fail the test spuriously.
    """
    path = RECEIPT_FIXTURES / "04-us-costco.png"
    if not path.is_file():
        pytest.skip("Costco fixture not present")

    receipt = (await ingest_receipt(session, path.read_bytes(), storage=storage)).receipt
    outcome = await extract_receipt(session, receipt, storage=storage)
    extraction = outcome.extraction

    assert extraction.store_name is not None
    assert "COSTCO" in extraction.store_name.upper()
    assert extraction.currency == "USD"
    assert extraction.total == "89.13"
    assert extraction.subtotal == "85.61"
    # Nine printed product lines; the model may also record summary lines.
    products = [i for i in extraction.line_items if i.kind == "product"]
    assert len(products) >= 9
    assert outcome.call.cost_usd > 0
