"""Reconciliation against a real database, replayed from stored extractions.

Costs nothing: everything here works from `raw_extraction` that already
exists. The API tests cover the endpoint that drives the same code.
"""

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ns.models import LineItem, Receipt
from ns.models.enums import LineItemKind, PipelineStatus, ReconciliationStatus
from ns.pipeline.ingest import ingest_receipt
from ns.pipeline.normalize import normalize_receipt
from ns.pipeline.reconcile import TAX_EXCLUSIVE, reconcile_receipt
from ns.providers.storage import LocalReceiptStorage
from tests.integration.test_extract import patch_call
from tests.unit.test_images import make_image

pytestmark = pytest.mark.integration


@pytest.fixture
def storage(tmp_path: Path) -> LocalReceiptStorage:
    return LocalReceiptStorage(root=tmp_path / "receipts")


async def _normalized(session: AsyncSession, storage: LocalReceiptStorage) -> Receipt:
    from ns.pipeline.extract import extract_receipt

    receipt = (await ingest_receipt(session, make_image(), storage=storage)).receipt
    with patch_call():
        await extract_receipt(session, receipt, storage=storage)
    await normalize_receipt(session, receipt)
    return receipt


async def test_the_costco_fixture_balances_end_to_end(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """85.61 + 3.52 = 89.13, straight from the stored transcription."""
    receipt = await _normalized(session, storage)

    result = await reconcile_receipt(session, receipt)

    assert result.status is ReconciliationStatus.BALANCED
    assert result.delta_cents == 0
    assert result.tax_model == TAX_EXCLUSIVE


async def test_the_verdict_is_persisted_on_the_receipt(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await _normalized(session, storage)
    await reconcile_receipt(session, receipt)

    assert receipt.reconciliation_status is ReconciliationStatus.BALANCED
    assert receipt.reconciliation_delta_cents == 0
    assert receipt.reconciliation_report is not None
    assert receipt.reconciliation_report["computed_total_cents"] == 8913
    # The stage ran, which is what the pipeline status records. Whether the
    # arithmetic closed is a separate fact on a separate field.
    assert receipt.status is PipelineStatus.RECONCILED


async def test_a_suspect_receipt_is_still_marked_reconciled(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """A receipt that does not balance is flagged with a reason, not dropped."""
    receipt = await _normalized(session, storage)
    receipt.total_cents = 9999
    await session.flush()

    result = await reconcile_receipt(session, receipt)

    assert result.status is ReconciliationStatus.SUSPECT
    assert receipt.status is PipelineStatus.RECONCILED
    assert receipt.reconciliation_delta_cents == 8913 - 9999
    assert receipt.reconciliation_report is not None
    checks = receipt.reconciliation_report["checks"]
    assert any(c["name"] == "total" and c["passed"] is False for c in checks)  # type: ignore[index,union-attr]


async def test_reconciliation_is_free_to_re_run(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """No API call, no image read — so a rule change can be applied to history."""
    receipt = await _normalized(session, storage)

    first = await reconcile_receipt(session, receipt)
    second = await reconcile_receipt(session, receipt)

    assert first.status is second.status
    assert first.delta_cents == second.delta_cents


async def test_an_unextracted_receipt_cannot_be_reconciled(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = (await ingest_receipt(session, make_image(), storage=storage)).receipt

    result = await reconcile_receipt(session, receipt)

    assert result.status is ReconciliationStatus.UNRECONCILABLE
    assert receipt.reconciliation_delta_cents is None


async def test_a_stray_unclassified_line_makes_the_receipt_suspect(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """The regression this stage was built to catch.

    An early prompt turned `TOTAL NUMBER OF ITEMS SOLD = 11` into an $11.00
    line. Reconciliation must notice, and must say which line to look at.
    """
    receipt = await _normalized(session, storage)
    session.add(
        LineItem(
            receipt_id=receipt.id,
            line_index=999,
            raw_text="TOTAL NUMBER OF ITEMS SOLD",
            normalized_text="total number of items sold",
            normalizer_version="v1",
            kind=LineItemKind.UNKNOWN,
            price_cents=1100,
        )
    )
    await session.flush()

    result = await reconcile_receipt(session, receipt)

    assert result.status is ReconciliationStatus.SUSPECT
    assert result.delta_cents == 1100
    assert any("unclassified" in h for h in result.report["hypotheses"])  # type: ignore[union-attr]


# ── The endpoints ─────────────────────────────────────────────────────────


async def test_normalize_and_reconcile_endpoints(client: AsyncClient) -> None:
    upload = await client.post(
        "/receipts", files={"file": ("receipt.jpg", make_image(), "image/jpeg")}
    )
    receipt_id = upload.json()["receipt"]["id"]

    with patch_call():
        assert (await client.post(f"/receipts/{receipt_id}/extract")).status_code == 200

    normalized = await client.post(f"/receipts/{receipt_id}/normalize")
    assert normalized.status_code == 200
    assert normalized.json()["line_item_count"] == 15

    reconciled = await client.post(f"/receipts/{receipt_id}/reconcile")
    assert reconciled.status_code == 200
    body = reconciled.json()
    assert body["reconciliation_status"] == "balanced"
    assert body["delta_cents"] == 0
    assert body["report"]["checks"]


async def test_normalize_before_extract_is_a_conflict(client: AsyncClient) -> None:
    upload = await client.post(
        "/receipts", files={"file": ("receipt.jpg", make_image(), "image/jpeg")}
    )
    receipt_id = upload.json()["receipt"]["id"]

    response = await client.post(f"/receipts/{receipt_id}/normalize")

    assert response.status_code == 409
    assert "extraction" in response.json()["detail"].lower()


async def test_reconciling_an_unknown_receipt_is_a_404(client: AsyncClient) -> None:
    assert (await client.post("/receipts/98765432/reconcile")).status_code == 404
