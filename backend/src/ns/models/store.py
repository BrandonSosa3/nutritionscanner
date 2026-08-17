"""Stores and the aliases receipts print for them."""

from datetime import datetime

from sqlmodel import Field, SQLModel

from ns.models.base import jsonb_column, timestamp_column, utcnow


class Store(SQLModel, table=True):
    __tablename__ = "store"

    id: int | None = Field(default=None, primary_key=True)

    name: str = Field(index=True, max_length=200)
    # The branch, not the chain. Prices differ between branches, and Phase 2
    # cross-store comparison is meaningless if two branches collapse into one.
    location: str | None = Field(default=None, max_length=200)
    currency: str = Field(default="USD", max_length=3)  # ISO 4217

    # Learned per-store parsing hints: whether the weight line sits above or
    # below its item, which flag characters mean tax, and so on.
    receipt_format_hints: dict[str, object] | None = Field(default=None, sa_column=jsonb_column())

    created_at: datetime = Field(default_factory=utcnow, sa_column=timestamp_column(nullable=False))


class StoreAlias(SQLModel, table=True):
    """Header text a store prints, mapped to a canonical store.

    Store identification is itself a fuzzy match that can fail. Without
    aliases, every header variant silently creates a duplicate Store and
    fragments the price history that Phase 2 depends on.
    """

    __tablename__ = "store_alias"

    id: int | None = Field(default=None, primary_key=True)
    store_id: int = Field(foreign_key="store.id", index=True, ondelete="CASCADE")
    alias_text: str = Field(unique=True, index=True, max_length=200)
    created_at: datetime = Field(default_factory=utcnow, sa_column=timestamp_column(nullable=False))
