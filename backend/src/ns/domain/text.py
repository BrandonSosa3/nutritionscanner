"""Receipt text normalisation. Pure — no I/O, no database.

Turns printed item text into the stable key that corrections and the resolver
match on. The brief scopes this precisely: strip SKUs, tax flags, and per-unit
pricing fragments.

What is deliberately *not* stripped is as important. Brand and quality markers
stay: `OG` means organic, which maps to a different USDA food than the
conventional variant, and `KS` identifies a store brand whose product may
genuinely differ. Aggressive stripping would collapse distinct foods onto one
correction key, and a correction is permanent — a wrong merge propagates to
every future receipt.

The version constant below is stored on every LineItem and EvalExample. Bump
it whenever these rules change, so an accuracy comparison can never silently
span two different definitions of "normalised".
"""

import re

# Bump on any behavioural change to normalise().
NORMALIZER_VERSION = "v1"

# A leading or trailing single letter used as a tax or department flag:
# Costco's "E FF BS BREAST", Whole Foods' "PL TORTILLA'S B".
_LEADING_FLAG = re.compile(r"^\s*[A-Z]\s+(?=[A-Z0-9])")
_TRAILING_FLAG = re.compile(r"\s+[A-Z]\s*$")

# A leading SKU or PLU printed in the item column: "673919 FF BS BREAST".
# Requires at least four digits so a genuine leading quantity survives.
_LEADING_CODE = re.compile(r"^\s*\d{4,}\s+")

# Per-unit pricing fragments: "@ 0.69/LB", "@ $5.99/kg", "@ 1.99 /lb".
_UNIT_PRICE = re.compile(r"@\s*[$€£R]?\s*\d+(?:[.,]\d+)?\s*/\s*[a-zA-Z#]+", re.IGNORECASE)

# Size and weight fragments riding along on the item line: "0.778kg NET",
# "1.08 lb", "MONT JACK 2#", "SPAR COOKING OIL 375ML".
#
# Sizes are stripped from the *key* on purpose. The food in a 375ml bottle and
# a 750ml bottle is the same food; the amount differs, and the amount is
# carried in grams rather than in the correction key. Leaving the size in
# would force a separate correction per package size for one product.
# `units.extract_package_size` reads the size from the raw text before this
# runs, so nothing is lost.
_SIZE_FRAGMENT = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*"
    r"(?:kg|kgs|g|gr|gram|grams|mg|lb|lbs|oz|ml|mls|cl|dl|l|lt|ltr|litre|liter)\b"
    r"\s*(?:net)?",
    re.IGNORECASE,
)
# The pound sign has no word boundary after it, so it needs its own pattern.
_POUND_FRAGMENT = re.compile(r"\b\d+(?:[.,]\d+)?\s*#")
_TARE = re.compile(r"\btare\s*=?\s*[.\d]*", re.IGNORECASE)

# Multi-buy and quantity prefixes: "3 @ 4.29", "1 @ 2 FOR 6.00".
_MULTIBUY = re.compile(r"^\s*\d+\s*@\s*(?:\d+\s*for\s*)?[\d.,]+\s*", re.IGNORECASE)

# Currency amounts left in the text.
_TRAILING_AMOUNT = re.compile(r"\s*[$€£R]?\s*\d+[.,]\d{2}\s*-?\s*$")

_PUNCTUATION = re.compile(r"[^\w\s%'/-]+")
_WHITESPACE = re.compile(r"\s+")


def normalise(raw_text: str) -> str:
    """Reduce printed item text to a stable matching key.

    Lowercased and whitespace-collapsed, with SKUs, tax flags, unit-price
    fragments, and weight continuations removed. Returns an empty string if
    nothing meaningful survives, which the caller treats as unresolvable
    rather than matching on "".
    """
    if not raw_text:
        return ""

    text = str(raw_text).strip()

    text = _MULTIBUY.sub(" ", text)
    text = _LEADING_CODE.sub(" ", text)
    text = _LEADING_FLAG.sub(" ", text)
    text = _UNIT_PRICE.sub(" ", text)
    text = _TARE.sub(" ", text)
    text = _POUND_FRAGMENT.sub(" ", text)
    text = _SIZE_FRAGMENT.sub(" ", text)
    text = _TRAILING_AMOUNT.sub(" ", text)
    text = _TRAILING_FLAG.sub(" ", text)

    text = _PUNCTUATION.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip().lower()

    return text


def looks_like_noise(normalised_text: str) -> bool:
    """True when the text carries no food signal worth resolving.

    Bare numbers, single characters, and empty strings should never become a
    correction key: they would match unrelated lines on future receipts and a
    correction is permanent.
    """
    if not normalised_text:
        return True
    if len(normalised_text) < 2:
        return True
    return not any(character.isalpha() for character in normalised_text)
