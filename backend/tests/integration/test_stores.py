"""Store identification against a real database.

Tier 1a of resolution — store-specific corrections — has nothing to key on
until this works, so these tests are really about whether corrections can be
scoped at all.
"""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.models import Receipt, Store, StoreAlias
from ns.pipeline.ingest import ingest_receipt
from ns.pipeline.normalize import normalize_receipt
from ns.pipeline.stores import resolve_store
from ns.providers.storage import LocalReceiptStorage
from tests.integration.test_extract import fake_extraction, patch_call
from tests.unit.test_images import make_image

pytestmark = pytest.mark.integration


@pytest.fixture
def storage(tmp_path: Path) -> LocalReceiptStorage:
    return LocalReceiptStorage(root=tmp_path / "receipts")


async def bare_receipt(session: AsyncSession, storage: LocalReceiptStorage, color: str) -> Receipt:
    return (await ingest_receipt(session, make_image(color=color), storage=storage)).receipt


async def aliases_of(session: AsyncSession, store: Store) -> list[StoreAlias]:
    rows = await session.execute(select(StoreAlias).where(col(StoreAlias.store_id) == store.id))
    return list(rows.scalars().all())


async def test_a_new_store_is_created_and_attached(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await bare_receipt(session, storage, "white")

    match = await resolve_store(session, receipt, "COSTCO WHOLESALE", "Thornton #629")

    assert match is not None
    assert match.created is True
    assert match.store.name == "costco wholesale"
    assert receipt.store_id == match.store.id


async def test_the_same_header_twice_is_one_store(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    first = await bare_receipt(session, storage, "white")
    second = await bare_receipt(session, storage, "black")

    a = await resolve_store(session, first, "COSTCO WHOLESALE", "Thornton #629")
    b = await resolve_store(session, second, "COSTCO WHOLESALE", "Thornton #629")

    assert a is not None and b is not None
    assert a.store.id == b.store.id
    assert b.matched_on == "alias"
    assert len(await aliases_of(session, a.store)) == 1


async def test_a_reprinted_header_matches_on_the_branch_number(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """The strongest identifier a receipt carries, and it survives the header
    being laid out differently."""
    first = await bare_receipt(session, storage, "white")
    second = await bare_receipt(session, storage, "black")

    a = await resolve_store(session, first, "COSTCO WHOLESALE", "Thornton #629")
    b = await resolve_store(session, second, "Costco Wholesale", "Whse:629 Thornton CO")

    assert a is not None and b is not None
    assert a.store.id == b.store.id
    assert b.matched_on == "branch_number"
    # The new header is learned, so next time it hits the fast path.
    assert len(await aliases_of(session, a.store)) == 2


async def test_two_branches_of_one_chain_stay_separate(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Prices differ between branches. Collapsing them would make the Phase 2
    cross-store comparison compare a store against itself."""
    first = await bare_receipt(session, storage, "white")
    second = await bare_receipt(session, storage, "black")

    a = await resolve_store(session, first, "COSTCO WHOLESALE", "Thornton #629")
    b = await resolve_store(session, second, "COSTCO WHOLESALE", "San Diego #452")

    assert a is not None and b is not None
    assert a.store.id != b.store.id


async def test_a_store_with_no_branch_number_matches_on_location(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    first = await bare_receipt(session, storage, "white")
    second = await bare_receipt(session, storage, "black")

    a = await resolve_store(session, first, "SPAR", "Bergville")
    b = await resolve_store(session, second, "SPAR", "Bergville")

    assert a is not None and b is not None
    assert a.store.id == b.store.id


async def test_two_towns_are_two_stores(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    first = await bare_receipt(session, storage, "white")
    second = await bare_receipt(session, storage, "black")

    a = await resolve_store(session, first, "SPAR", "Bergville")
    b = await resolve_store(session, second, "SPAR", "Winterton")

    assert a is not None and b is not None
    assert a.store.id != b.store.id


async def test_an_unnamed_store_leaves_the_receipt_unattached(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """A torn or badly lit header is a real outcome. Attaching the receipt to
    a store that was never identified would be inventing data."""
    receipt = await bare_receipt(session, storage, "white")

    assert await resolve_store(session, receipt, None, "Somewhere") is None
    assert await resolve_store(session, receipt, "   ", None) is None
    assert receipt.store_id is None


async def test_the_store_takes_the_receipt_s_currency(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    receipt = await bare_receipt(session, storage, "white")
    receipt.currency = "ZAR"

    match = await resolve_store(session, receipt, "SPAR", "Bergville")

    assert match is not None
    assert match.store.currency == "ZAR"


# ── Wired into normalisation ──────────────────────────────────────────────


async def test_normalisation_attaches_the_store(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """The gap this closes: without it every receipt had a null store, so
    store-scoped corrections could never fire."""
    from ns.pipeline.extract import extract_receipt

    receipt = await bare_receipt(session, storage, "white")
    with patch_call():
        await extract_receipt(session, receipt, storage=storage)

    await normalize_receipt(session, receipt)

    assert receipt.store_id is not None
    store = await session.get(Store, receipt.store_id)
    assert store is not None
    assert store.name == "costco wholesale"
    assert store.location == "Thornton #629"


async def test_re_normalising_does_not_create_a_second_store(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    from ns.pipeline.extract import extract_receipt

    receipt = await bare_receipt(session, storage, "white")
    with patch_call():
        await extract_receipt(session, receipt, storage=storage)

    await normalize_receipt(session, receipt)
    first = receipt.store_id
    await normalize_receipt(session, receipt)

    assert receipt.store_id == first
    store = await session.get(Store, first)
    assert store is not None
    assert len(await aliases_of(session, store)) == 1


async def test_a_receipt_with_no_store_name_normalises_anyway(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    from ns.pipeline.extract import extract_receipt

    extraction = fake_extraction()
    extraction.store_name = None
    extraction.store_location = None

    receipt = await bare_receipt(session, storage, "white")
    with patch_call(extraction):
        await extract_receipt(session, receipt, storage=storage)

    result = await normalize_receipt(session, receipt)

    assert receipt.store_id is None
    assert len(result.line_items) == 15


async def test_the_printed_store_name_is_kept_for_display(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """`name` is normalised for matching and is a key, not a label. Rendering
    it to a person produced "applies only at costco wholesale"."""
    receipt = await bare_receipt(session, storage, "white")

    match = await resolve_store(session, receipt, "COSTCO WHOLESALE", "Thornton #629")

    assert match is not None
    assert match.store.name == "costco wholesale"
    assert match.store.display_name == "COSTCO WHOLESALE"


async def test_a_store_identified_before_this_falls_back_to_the_key(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """No backfill was run, so older stores have no display name."""
    from ns.models import Store

    store = Store(name="spar", display_name=None)
    session.add(store)
    await session.flush()

    assert (store.display_name or store.name) == "spar"


async def test_an_older_store_fills_in_its_name_on_the_next_receipt(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Healing in code rather than a SQL backfill: the value comes from a real
    printed header instead of a guess made by re-casing the key."""
    from ns.models import Store, StoreAlias
    from ns.pipeline.stores import alias_key

    store = Store(name="costco wholesale", location="Thornton #629", display_name=None)
    session.add(store)
    await session.flush()
    session.add(
        StoreAlias(store_id=store.id, alias_text=alias_key("COSTCO WHOLESALE", "Thornton #629"))
    )
    await session.flush()

    receipt = await bare_receipt(session, storage, "white")
    match = await resolve_store(session, receipt, "COSTCO WHOLESALE", "Thornton #629")

    assert match is not None
    assert match.matched_on == "alias"
    assert match.store.id == store.id
    assert match.store.display_name == "COSTCO WHOLESALE"
