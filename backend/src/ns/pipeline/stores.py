"""Identifying which store a receipt came from.

A `Store` is a *branch*, not a chain. Prices differ between branches, and the
Phase 2 cross-store comparison is meaningless if two branches collapse into
one row — so `COSTCO WHOLESALE #629 Thornton` and a Costco in San Diego are
two stores, deliberately.

Matching is exact-on-normalised-text, never fuzzy. Three passes:

1. The full header, as an alias. Repeat visits to a store print the same
   header, so this is the fast path and the one that runs almost always.
2. Chain name plus branch number. `#629` is the strongest identifier a receipt
   carries, and it survives the header being printed differently.
3. Chain name plus normalised location, when no branch number is printed.

Anything that matches none of the three becomes a new store. That is the
honest outcome — it records what the receipt said rather than guessing which
existing branch was meant — and every header that led to a store is kept as an
alias, so the same variant resolves without a second row next time.

No model call. Store identity is text matching against rows we already have,
and spending money on it would be spending money to be less deterministic.
"""

import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from ns.logging import get_logger
from ns.models import Receipt, Store, StoreAlias

log = get_logger(__name__)

# Everything except letters, digits, and spaces. Receipt headers vary wildly in
# punctuation for the same store: "COSTCO WHOLESALE #629", "Costco Wholesale,
# #629", "COSTCO-WHOLESALE 629".
_PUNCTUATION = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE = re.compile(r"\s+")

# A branch number, however the receipt writes it.
# The separator is deliberately loose: receipts print `#629`, `Whse:629`,
# `Store 1204`, and `No. 88`. A bare number is never enough — `3150 Sharon
# Rd` is a street address, not branch 3150.
_BRANCH_NUMBER = re.compile(r"(?:#|no\.?|store|whse|branch)[\s:.#-]*(\d{1,6})\b", re.IGNORECASE)

# Corporate suffixes that carry no identity. Deliberately short: "wholesale"
# is part of the Costco brand and "market" part of Sprouts Farmers Market, so
# neither belongs here.
_SUFFIXES = {"inc", "llc", "ltd", "limited", "corp", "co", "pty", "plc", "gmbh", "sa"}

MAX_ALIAS_LENGTH = 200


@dataclass(frozen=True, slots=True)
class StoreMatch:
    store: Store
    created: bool  # a branch we had not seen before
    matched_on: str  # alias | branch_number | location | new


def normalise_store_name(name: str) -> str:
    """Reduce a printed store name to a stable comparison key."""
    lowered = _PUNCTUATION.sub(" ", name.strip().lower())
    words = [w for w in _WHITESPACE.sub(" ", lowered).split() if w and w not in _SUFFIXES]
    return " ".join(words)


def branch_number(*texts: str | None) -> str | None:
    """The branch number a receipt prints, if any.

    Checked across the whole header because stores put it in different places:
    Costco prints `#629` in the location line, SPAR prints the town instead.
    """
    for text in texts:
        if not text:
            continue
        match = _BRANCH_NUMBER.search(text)
        if match:
            return match.group(1)
    return None


def alias_key(name: str, location: str | None) -> str:
    """The header text as stored, normalised so punctuation cannot fork a store."""
    parts = [normalise_store_name(name)]
    if location:
        parts.append(normalise_store_name(location))
    return " | ".join(p for p in parts if p)[:MAX_ALIAS_LENGTH]


async def resolve_store(
    session: AsyncSession, receipt: Receipt, name: str | None, location: str | None
) -> StoreMatch | None:
    """Find or create the branch this receipt came from, and attach it.

    Returns None when the receipt names no store at all — which is a real
    outcome for a torn or badly lit header, and leaves `store_id` null rather
    than attaching the receipt to a store that was never identified.
    """
    if not name or not name.strip():
        log.info("store.unnamed", receipt_id=receipt.id)
        return None

    key = alias_key(name, location)
    if not key:
        return None

    existing_alias = (
        await session.execute(select(StoreAlias).where(col(StoreAlias.alias_text) == key))
    ).scalar_one_or_none()
    printed = " ".join(name.split())[:200]

    if existing_alias is not None:
        store = await session.get(Store, existing_alias.store_id)
        if store is not None:
            # Heal a store identified before display names existed. Normalising
            # is a write path already, and leaving the gap permanent would mean
            # the oldest stores are the ones that read worst.
            if not store.display_name:
                store.display_name = printed
            receipt.store_id = store.id
            return StoreMatch(store=store, created=False, matched_on="alias")

    normalised_name = normalise_store_name(name)
    number = branch_number(location, name)

    candidates = (
        (await session.execute(select(Store).where(col(Store.name) == normalised_name)))
        .scalars()
        .all()
    )

    def same_branch(candidate: Store) -> bool:
        """Whether a candidate is this receipt's branch.

        A printed branch number decides it on its own — it is the strongest
        identifier a receipt carries. Without one, the location text has to
        agree, and two stores that both print no location are taken to be the
        same single-branch store.
        """
        if number is not None:
            return branch_number(candidate.location) == number
        if location and candidate.location:
            return normalise_store_name(candidate.location) == normalise_store_name(location)
        return not location and not candidate.location

    match: Store | None = next((c for c in candidates if same_branch(c)), None)
    matched_on = "new" if match is None else ("branch_number" if number else "location")

    created = match is None
    if match is not None and not match.display_name:
        match.display_name = printed
    if match is None:
        match = Store(
            name=normalised_name,
            display_name=printed,
            location=location[:200] if location else None,
            currency=receipt.currency,
        )
        session.add(match)
        await session.flush()

    session.add(StoreAlias(store_id=match.id, alias_text=key))
    await session.flush()

    receipt.store_id = match.id
    log.info(
        "store.resolved",
        receipt_id=receipt.id,
        store_id=match.id,
        name=normalised_name,
        branch=number,
        matched_on=matched_on,
        created=created,
    )
    return StoreMatch(store=match, created=created, matched_on=matched_on)
