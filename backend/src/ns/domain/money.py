"""Money handling. Pure — no I/O, no database.

Money is integer cents everywhere internally (DECISIONS.md D1). Parsing
happens once, at the boundary where receipt text becomes structured data, and
every downstream calculation is integer arithmetic.

The parser is deliberately strict: it refuses input it does not fully
understand rather than guessing. A misread price is worse than an unresolved
one (principle 2).
"""

import re
from decimal import Decimal, InvalidOperation

# Currency symbols and codes that appear on the fixture receipts.
_CURRENCY_CHARS = "$€£¥₹R"

# Optional leading sign, optional currency, digits with optional separators,
# optional decimal part, optional trailing sign (Whole Foods prints "2.00-").
_MONEY = re.compile(
    rf"""
    ^\s*
    (?P<lead_sign>[-+])?\s*
    (?:[{_CURRENCY_CHARS}])?\s*
    (?P<lead_sign2>[-+])?\s*
    (?P<int>\d{{1,3}}(?:[,\s]\d{{3}})*|\d+)
    (?:[.](?P<frac>\d{{1,2}}))?
    \s*
    (?P<trail_sign>-)?
    \s*$
    """,
    re.VERBOSE,
)


class MoneyParseError(ValueError):
    """The text could not be read as an unambiguous monetary amount."""


def parse_money_to_cents(text: str) -> int:
    """Parse a printed monetary amount into signed integer cents.

    Handles the forms that actually appear on receipts: a leading minus
    (`-15.00`), a trailing minus (`2.00-`, Whole Foods), currency symbols and
    the South African `R` prefix, and thousands separators.

    Raises MoneyParseError on anything ambiguous — including a value with more
    than two decimal places, which is a sign the text is not a price.
    """
    if text is None:
        raise MoneyParseError("No amount given.")

    raw = str(text).strip()
    if not raw:
        raise MoneyParseError("No amount given.")

    match = _MONEY.match(raw)
    if match is None:
        raise MoneyParseError(f"Could not read {raw!r} as a monetary amount.")

    negatives = [
        match.group("lead_sign"),
        match.group("lead_sign2"),
        match.group("trail_sign"),
    ]
    signs = [s for s in negatives if s == "-"]
    if len(signs) > 1:
        raise MoneyParseError(f"{raw!r} has more than one minus sign.")

    integer_part = re.sub(r"[,\s]", "", match.group("int"))
    frac_part = (match.group("frac") or "").ljust(2, "0")

    try:
        cents = int(integer_part) * 100 + int(frac_part)
    except (ValueError, InvalidOperation) as exc:  # pragma: no cover - regex guards this
        raise MoneyParseError(f"Could not read {raw!r} as a monetary amount.") from exc

    return -cents if signs else cents


def cents_to_decimal(cents: int) -> Decimal:
    """For display and for rate calculations. Never used for accumulation."""
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


def format_cents(cents: int, currency: str = "USD") -> str:
    """Human-readable amount. Units are always shown (DESIGN.md)."""
    symbol = {"USD": "$", "AUD": "$", "ZAR": "R", "EUR": "€", "GBP": "£"}.get(currency, "")
    sign = "-" if cents < 0 else ""
    return f"{sign}{symbol}{abs(cents) // 100}.{abs(cents) % 100:02d}"
