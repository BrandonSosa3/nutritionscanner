"""Receipt endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from ns.api.schemas import (
    DerivationResponse,
    ExtractionResponse,
    LineItemListResponse,
    LineItemOut,
    NormalizationResponse,
    ReceiptDetail,
    ReceiptListResponse,
    ReceiptSummary,
    ReceiptUploadResponse,
    ReconciliationResponse,
    ResolutionResponse,
)
from ns.db import get_session
from ns.domain.images import MAX_IMAGE_BYTES, InvalidImageError
from ns.logging import get_logger
from ns.models import Food, LineItem, Receipt
from ns.models.enums import LineItemKind, ResolutionSource
from ns.pipeline.derive import derive_receipt
from ns.pipeline.extract import extract_receipt
from ns.pipeline.ingest import ingest_receipt
from ns.pipeline.normalize import normalize_receipt
from ns.pipeline.reconcile import reconcile_receipt
from ns.pipeline.resolve import RESOLVABLE_KINDS, resolve_receipt
from ns.providers.anthropic.budget import BudgetExceededError
from ns.providers.anthropic.client import MissingApiKeyError

router = APIRouter(prefix="/receipts", tags=["receipts"])
log = get_logger(__name__)

_CHUNK = 1024 * 1024


async def _read_capped(upload: UploadFile) -> bytes:
    """Read an upload, refusing to buffer more than the image size limit.

    `UploadFile.read()` with no argument would happily pull an arbitrarily
    large body into memory before any validation runs.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(_CHUNK):
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Image is larger than the {MAX_IMAGE_BYTES // 1_048_576} MB limit. "
                    "Retake the photo at a lower resolution."
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "",
    response_model=ReceiptUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a receipt photo",
)
async def upload_receipt(
    file: Annotated[UploadFile, File(description="A photo of a grocery receipt.")],
    session: AsyncSession = Depends(get_session),
) -> ReceiptUploadResponse:
    """Store a receipt image and register it for processing.

    Idempotent on image content: uploading the same file twice returns the
    original receipt with `created: false` rather than creating a duplicate.

    The image is stored before any processing is attempted, so a receipt is
    never lost to an extraction failure or an API outage.
    """
    data = await _read_capped(file)

    try:
        result = await ingest_receipt(session, data)
    except InvalidImageError as exc:
        # 422 rather than 400: the request was well-formed, the content wasn't
        # usable. The message is written to be shown to a user verbatim.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return ReceiptUploadResponse(
        receipt=ReceiptDetail.of(result.receipt),
        created=result.created,
        width=result.facts.width,
        height=result.facts.height,
        image_format=result.facts.image_format,
    )


@router.get("", response_model=ReceiptListResponse, summary="List receipts")
async def list_receipts(
    session: AsyncSession = Depends(get_session),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReceiptListResponse:
    """Most recent first by purchase date.

    Ordered by `purchased_at`, not upload time: backfilled receipts belong in
    their real chronological place (DECISIONS.md D18). Receipts not yet
    extracted have no purchase date and sort first, which is also where they
    want to be — they are the ones needing attention.
    """
    total = (await session.execute(select(func.count()).select_from(Receipt))).scalar_one()

    # `desc()` is on the SQLAlchemy column, which mypy can't see through the
    # SQLModel annotation; `col()` recovers the column expression.
    rows = await session.execute(
        select(Receipt)
        .order_by(col(Receipt.purchased_at).desc().nullsfirst(), col(Receipt.id).desc())
        .limit(limit)
        .offset(offset)
    )
    return ReceiptListResponse(
        items=[ReceiptSummary.of(r) for r in rows.scalars().all()],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{receipt_id}", response_model=ReceiptDetail, summary="Get one receipt")
async def get_receipt(
    receipt_id: int,
    session: AsyncSession = Depends(get_session),
) -> ReceiptDetail:
    receipt = await session.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No receipt with id {receipt_id}.",
        )
    return ReceiptDetail.of(receipt)


@router.post(
    "/{receipt_id}/extract",
    response_model=ExtractionResponse,
    summary="Run extraction on a stored receipt",
)
async def extract(
    receipt_id: int,
    force: Annotated[
        bool, Query(description="Re-extract a receipt that already has a transcription.")
    ] = False,
    session: AsyncSession = Depends(get_session),
) -> ExtractionResponse:
    """Transcribe the receipt image into structured data.

    Runs synchronously so the result is visible immediately; the same work is
    available as a background job for bulk processing.

    Extraction is idempotent unless `force` is set — a receipt that already has
    a transcription is not re-sent, because that costs money and yields nothing
    new. Re-running with `force` is how a prompt revision gets evaluated
    against a receipt already on file.
    """
    receipt = await session.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"No receipt with id {receipt_id}.")

    try:
        outcome = await extract_receipt(session, receipt, force=force)
    except MissingApiKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except BudgetExceededError as exc:
        # 402: the request is valid, but spending it would breach the ceiling.
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    extraction = outcome.extraction
    return ExtractionResponse(
        receipt_id=receipt_id,
        status=outcome.receipt.status,
        store_name=extraction.store_name,
        purchased_at=extraction.purchased_at,
        currency=extraction.currency,
        line_item_count=len(extraction.line_items),
        total=extraction.total,
        legibility=extraction.legibility,
        notes=extraction.notes,
        cost_usd=str(outcome.call.cost_usd),
        latency_ms=outcome.call.latency_ms,
    )


@router.post(
    "/{receipt_id}/normalize",
    response_model=NormalizationResponse,
    summary="Rebuild line items from the stored extraction",
)
async def normalize(
    receipt_id: int,
    session: AsyncSession = Depends(get_session),
) -> NormalizationResponse:
    """Turn the stored transcription into structured line items.

    Free and repeatable: it replays from `raw_extraction` and never touches
    the image or the API. Re-running replaces this receipt's line items, so
    it is also how a normaliser change gets applied to receipts already on
    file.
    """
    receipt = await session.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"No receipt with id {receipt_id}.")

    try:
        result = await normalize_receipt(session, receipt)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return NormalizationResponse(
        receipt_id=receipt_id,
        status=result.receipt.status,
        line_item_count=len(result.line_items),
        dropped=result.dropped,
        with_grams=sum(1 for i in result.line_items if i.grams_as_purchased is not None),
        unparseable_amounts=result.unparseable_amounts,
    )


@router.post(
    "/{receipt_id}/reconcile",
    response_model=ReconciliationResponse,
    summary="Check that the receipt's arithmetic closes",
)
async def reconcile(
    receipt_id: int,
    session: AsyncSession = Depends(get_session),
) -> ReconciliationResponse:
    """Add the basket up and compare it against the printed total.

    Also free to re-run. A receipt that does not balance comes back as
    `suspect` with a report explaining what was summed and what would have
    made it close — never as clean.
    """
    receipt = await session.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"No receipt with id {receipt_id}.")

    result = await reconcile_receipt(session, receipt)

    return ReconciliationResponse(
        receipt_id=receipt_id,
        status=receipt.status,
        reconciliation_status=result.status,
        delta_cents=result.delta_cents,
        tax_model=result.tax_model,
        report=result.report,
    )


@router.post(
    "/{receipt_id}/resolve",
    response_model=ResolutionResponse,
    summary="Identify what food each line refers to",
)
async def resolve(
    receipt_id: int,
    force: Annotated[bool, Query(description="Re-resolve lines that already have a food.")] = False,
    session: AsyncSession = Depends(get_session),
) -> ResolutionResponse:
    """Resolve line items to foods: corrections first, then one batched model call.

    Idempotent by default. Re-running after adding a correction fixes the
    unresolved lines without paying to re-answer the rest — which is the whole
    point of tier 1 sitting in front of the model.

    This is the only stage that costs money per receipt.
    """
    receipt = await session.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"No receipt with id {receipt_id}.")

    try:
        result = await resolve_receipt(session, receipt, force=force)
    except MissingApiKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except BudgetExceededError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc

    return ResolutionResponse(
        receipt_id=receipt_id,
        status=receipt.status,
        by_source=result.by_source,
        coverage=round(result.coverage, 4),
        unresolved=result.unresolved_texts,
        cost_usd=str(result.call.cost_usd) if result.call else "0",
        latency_ms=result.call.latency_ms if result.call else None,
    )


@router.get(
    "/{receipt_id}/lines",
    response_model=LineItemListResponse,
    summary="The receipt's line items and their resolution state",
)
async def list_lines(
    receipt_id: int,
    session: AsyncSession = Depends(get_session),
) -> LineItemListResponse:
    """What the correction queue reads.

    Coverage is computed over *resolvable* lines only. Counting subtotal and
    tax lines as unresolved would understate a receipt that is in fact fully
    identified.
    """
    receipt = await session.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"No receipt with id {receipt_id}.")

    lines = list(
        (
            await session.execute(
                select(LineItem)
                .where(col(LineItem.receipt_id) == receipt_id)
                .order_by(col(LineItem.line_index))
            )
        )
        .scalars()
        .all()
    )

    food_ids = {line.food_id for line in lines if line.food_id is not None}
    names: dict[int, str] = {}
    if food_ids:
        foods = (
            (await session.execute(select(Food).where(col(Food.id).in_(food_ids)))).scalars().all()
        )
        names = {food.id: food.canonical_name for food in foods if food.id is not None}

    items: list[LineItemOut] = []
    resolvable = 0
    resolved = 0
    for line in lines:
        out = LineItemOut.model_validate(line)
        out.food_name = names.get(line.food_id) if line.food_id is not None else None
        items.append(out)
        if line.kind in RESOLVABLE_KINDS or line.kind is LineItemKind.FEE:
            resolvable += 1
            if line.resolution_source is not ResolutionSource.UNRESOLVED:
                resolved += 1

    return LineItemListResponse(
        receipt_id=receipt_id,
        items=items,
        resolved=resolved,
        total=resolvable,
        coverage=round(resolved / resolvable, 4) if resolvable else 0.0,
    )


@router.post(
    "/{receipt_id}/derive",
    response_model=DerivationResponse,
    summary="Rebuild this receipt's price observations",
)
async def derive(
    receipt_id: int,
    session: AsyncSession = Depends(get_session),
) -> DerivationResponse:
    """Turn resolved lines into price-per-100 g observations.

    Rebuilt every time rather than merged: a correction changes both the price
    and the weight, so a stale observation would keep feeding the ranking with
    the old numbers.
    """
    receipt = await session.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"No receipt with id {receipt_id}.")

    try:
        result = await derive_receipt(session, receipt)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DerivationResponse(
        receipt_id=receipt_id,
        observations=result.observed,
        skipped_no_grams=result.skipped_no_grams,
        skipped_unresolved=result.skipped_unresolved,
    )
