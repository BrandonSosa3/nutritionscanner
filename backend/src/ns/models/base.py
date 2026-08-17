"""Shared column helpers.

Centralising these keeps the money and gram conventions from DECISIONS.md
impossible to get wrong in one table and right in another.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Column, DateTime, Numeric
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB


def utcnow() -> datetime:
    """Timezone-aware UTC. Naive datetimes are banned (ruff DTZ)."""
    return datetime.now(UTC)


def enum_column(enum_cls: type[StrEnum], **kwargs: Any) -> Column[Any]:
    """VARCHAR + CHECK rather than a native Postgres enum — see enums.py.

    `create_constraint=True` is not optional here: SQLAlchemy has defaulted it
    to False since 1.4, so `native_enum=False` alone produces a bare VARCHAR
    with no integrity whatsoever. Without it the database will happily store
    any string in a status column.
    """
    return Column(
        SAEnum(
            enum_cls,
            native_enum=False,
            length=32,
            validate_strings=True,
            create_constraint=True,
            # Store the StrEnum's *value* ("unknown"), not its name ("UNKNOWN").
            # SQLAlchemy defaults to the name, which would make psql output,
            # raw SQL, and CSV export all disagree with the values used in
            # code — a needless trap in a system built for data export.
            values_callable=lambda enum: [member.value for member in enum],
        ),
        **kwargs,
    )


def grams_column(**kwargs: Any) -> Column[Any]:
    """Grams to 3 decimal places: milligram resolution, ample for food."""
    return Column(Numeric(12, 3), **kwargs)


def ratio_column(**kwargs: Any) -> Column[Any]:
    """Percentages and factors, e.g. edible portion 68.000."""
    return Column(Numeric(7, 3), **kwargs)


def jsonb_column(**kwargs: Any) -> Column[Any]:
    return Column(JSONB, **kwargs)


def timestamp_column(**kwargs: Any) -> Column[Any]:
    return Column(DateTime(timezone=True), **kwargs)
