"""Normalisation, replayed from stored extractions.

Costs nothing: every test here works from `raw_extraction` that already
exists, which is the entire point of storing it permanently.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.domain.money import parse_money_to_cents
from ns.models import LineItem, Receipt
from ns.models.enums import GramsBasis, LineItemKind, PipelineStatus
from ns.pipeline.ingest import ingest_receipt
from ns.pipeline.normalize import normalize_receipt
from ns.providers.storage import LocalReceiptStorage
from tests.integration.test_extract import fake_extraction, patch_call
from tests.unit.test_images import make_image

pytestmark = pytest.mark.integration


@pytest.fixture
def storage(tmp_path: Path) -> LocalReceiptStorage:
    return LocalReceiptStorage(root=tmp_path / "receipts")


async def _extracted(session: AsyncSession, storage: LocalReceiptStorage) -> Receipt:
    from ns.pipeline.extract import extract_receipt

    receipt = (await ingest_receipt(session, make_image(), storage=storage)).receipt
    with patch_call():
        await extract_receipt(session, receipt, storage=storage)
    return receipt


async def _items(session: AsyncSession, receipt: Receipt) -> list[LineItem]:
    rows = await session.execute(
        select(LineItem)
        .where(col(LineItem.receipt_id) == receipt.id)
        .order_by(col(LineItem.line_index))
    )
    return list(rows.scalars().all())


async def test_normalisation_creates_line_items(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await _extracted(session, storage)

    result = await normalize_receipt(session, receipt)

    assert len(result.line_items) == 2
    assert receipt.status is PipelineStatus.NORMALIZED


async def test_prices_become_integer_cents(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await _extracted(session, storage)
    await normalize_receipt(session, receipt)

    items = await _items(session, receipt)
    assert items[0].price_cents == 2399  # "23.99"
    assert receipt.subtotal_cents == 8561
    assert receipt.tax_cents == 352
    assert receipt.total_cents == 8913


async def test_receipt_date_is_parsed(session: AsyncSession, storage: LocalReceiptStorage) -> None:
    receipt = await _extracted(session, storage)
    await normalize_receipt(session, receipt)

    assert receipt.purchased_at is not None
    assert receipt.purchased_at.isoformat() == "2016-04-20"


async def test_normalized_text_strips_codes_and_flags(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await _extracted(session, storage)
    await normalize_receipt(session, receipt)

    items = await _items(session, receipt)
    assert items[0].normalized_text == "ff bs breast"
    assert items[0].normalizer_version


async def test_rerunning_replaces_rather_than_appends(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Normalisation replays from stored data, so it will be re-run often."""
    receipt = await _extracted(session, storage)

    await normalize_receipt(session, receipt)
    await normalize_receipt(session, receipt)

    assert len(await _items(session, receipt)) == 2


async def test_normalising_without_an_extraction_is_refused(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = (await ingest_receipt(session, make_image(), storage=storage)).receipt

    with pytest.raises(ValueError, match="no extraction"):
        await normalize_receipt(session, receipt)


# ── Grams ─────────────────────────────────────────────────────────────────


async def test_stated_weight_becomes_grams(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    extraction = fake_extraction()
    extraction.line_items[0].weight_text = "1.08 lb"

    receipt = (await ingest_receipt(session, make_image(), storage=storage)).receipt
    from ns.pipeline.extract import extract_receipt

    with patch_call(extraction):
        await extract_receipt(session, receipt, storage=storage)
    await normalize_receipt(session, receipt)

    items = await _items(session, receipt)
    assert items[0].grams_as_purchased == Decimal("489.880")
    assert items[0].grams_basis is GramsBasis.FROM_RECEIPT


async def test_package_size_in_the_name_becomes_grams(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """The SPAR case: roughly half that basket resolves to exact grams from
    the printed text, with no model estimate involved."""
    extraction = fake_extraction()
    extraction.line_items[0].raw_text = "SMOKED VIENNAS 500GR"
    extraction.line_items[0].weight_text = None
    extraction.line_items[0].quantity = None

    receipt = (await ingest_receipt(session, make_image(), storage=storage)).receipt
    from ns.pipeline.extract import extract_receipt

    with patch_call(extraction):
        await extract_receipt(session, receipt, storage=storage)
    await normalize_receipt(session, receipt)

    items = await _items(session, receipt)
    assert items[0].grams_as_purchased == Decimal("500.000")
    assert items[0].grams_basis is GramsBasis.PER_PACKAGE
    assert items[0].normalized_text == "smoked viennas"


async def test_stated_weight_beats_a_code_in_the_name(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Fixture 03 prints `BANANAS LOOSE 17KG`, where 17KG is a bin code and
    0.596 kg is the measured weight."""
    extraction = fake_extraction()
    extraction.line_items[0].raw_text = "BANANAS LOOSE 17KG"
    extraction.line_items[0].weight_text = "0.596 kg"
    extraction.line_items[0].quantity = None

    receipt = (await ingest_receipt(session, make_image(), storage=storage)).receipt
    from ns.pipeline.extract import extract_receipt

    with patch_call(extraction):
        await extract_receipt(session, receipt, storage=storage)
    await normalize_receipt(session, receipt)

    items = await _items(session, receipt)
    assert items[0].grams_as_purchased == Decimal("596.000")
    assert items[0].grams_basis is GramsBasis.FROM_RECEIPT


async def test_volume_without_density_leaves_grams_unknown(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Never assume the density of water — the brief is explicit."""
    extraction = fake_extraction()
    extraction.line_items[0].raw_text = "SPAR COOKING OIL 375ML"
    extraction.line_items[0].weight_text = None
    extraction.line_items[0].quantity = None

    receipt = (await ingest_receipt(session, make_image(), storage=storage)).receipt
    from ns.pipeline.extract import extract_receipt

    with patch_call(extraction):
        await extract_receipt(session, receipt, storage=storage)
    await normalize_receipt(session, receipt)

    items = await _items(session, receipt)
    assert items[0].grams_as_purchased is None
    assert items[0].grams_basis is GramsBasis.UNKNOWN


async def test_bare_item_has_no_grams(session: AsyncSession, storage: LocalReceiptStorage) -> None:
    receipt = await _extracted(session, storage)
    await normalize_receipt(session, receipt)

    items = await _items(session, receipt)
    # "FF BS BREAST" states no weight and no size.
    assert items[0].grams_as_purchased is None
    assert items[0].grams_basis is GramsBasis.UNKNOWN


# ── Line kinds ────────────────────────────────────────────────────────────


async def test_section_headers_and_payment_lines_are_dropped(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """They carry no basket value and would distort reconciliation."""
    extraction = fake_extraction()
    from ns.providers.anthropic.schemas import ExtractedLineItem

    extraction.line_items.append(
        ExtractedLineItem(line_index=90, raw_text="GROCERY", amount=None, kind="section_header")
    )
    extraction.line_items.append(
        ExtractedLineItem(line_index=91, raw_text="CHANGE", amount="0.00", kind="payment")
    )

    receipt = (await ingest_receipt(session, make_image(), storage=storage)).receipt
    from ns.pipeline.extract import extract_receipt

    with patch_call(extraction):
        await extract_receipt(session, receipt, storage=storage)
    result = await normalize_receipt(session, receipt)

    assert result.dropped == 2
    assert all(i.kind is LineItemKind.PRODUCT for i in result.line_items)


async def test_ambiguous_lines_are_preserved_as_unknown(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Fixture 01's bare `SPECIAL` lines. Treating them as discounts yields
    35.28 and flags a clean receipt as broken; dropping them loses 3.48."""
    extraction = fake_extraction()
    from ns.providers.anthropic.schemas import ExtractedLineItem

    extraction.line_items.append(
        ExtractedLineItem(line_index=50, raw_text="SPECIAL", amount="0.99", kind="unknown")
    )

    receipt = (await ingest_receipt(session, make_image(), storage=storage)).receipt
    from ns.pipeline.extract import extract_receipt

    with patch_call(extraction):
        await extract_receipt(session, receipt, storage=storage)
    await normalize_receipt(session, receipt)

    items = await _items(session, receipt)
    special = [i for i in items if i.raw_text == "SPECIAL"]
    assert len(special) == 1
    assert special[0].kind is LineItemKind.UNKNOWN
    assert special[0].price_cents == 99


async def test_the_costco_basket_reconciles_after_normalisation(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """The arithmetic that reconciliation will check, verified end to end
    through real parsing rather than by hand."""
    receipt = await _extracted(session, storage)
    await normalize_receipt(session, receipt)

    items = await _items(session, receipt)
    products = [i for i in items if i.kind is LineItemKind.PRODUCT]

    assert sum(i.price_cents for i in products) == 2399 + 1287
    assert receipt.subtotal_cents == parse_money_to_cents("85.61")
    assert receipt.total_cents == receipt.subtotal_cents + receipt.tax_cents
