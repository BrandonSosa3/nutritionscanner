"""FoodData Central HTTP client.

Free, key-gated, and rate-limited per hour. Two endpoints are used: search, to
turn a canonical food name into candidates, and detail, to fetch the full
nutrient set for the one chosen.

Search results are deliberately not trusted as final. The API's relevance
score is a text match, and `chicken breast` matching `Chicken breast tenders,
breaded, frozen` is a text match too. Candidate selection is a separate
decision made against a scored shortlist, not "take the first hit".

Every response is cached on disk by request, so re-running enrichment during
development costs no quota and works offline. That cache is also what lets the
test suite exercise real payloads without a key.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from ns.config import get_settings
from ns.logging import get_logger

log = get_logger(__name__)

BASE_URL = "https://api.nal.usda.gov/fdc/v1"

# Generic ingredients with refuse percentages, in preference order.
#
# Branded is excluded deliberately: a branded row is one manufacturer's
# product, and resolving `tomatoes, diced, canned` to a specific brand's label
# would assert something the receipt never said.
#
# `Survey (FNDDS)` is excluded for a duller reason — the API rejected it with
# a 400 when sent on its own, and DEMO_KEY hit its rate limit before that could
# be pinned down. Both values below were verified to return 200 on 2026-08-18.
# Survey data is modelled for dietary recall studies rather than analytically
# measured, so excluding it costs little; if it turns out to be accepted, it
# belongs last in this tuple.
GENERIC_DATA_TYPES = ("Foundation", "SR Legacy")

TIMEOUT_SECONDS = 20.0


class UsdaError(RuntimeError):
    """FoodData Central could not be reached or returned an error."""


class MissingUsdaKeyError(UsdaError):
    """No USDA_API_KEY configured."""


def _cache_root() -> Path:
    return get_settings().receipt_storage_path.parent / "usda-cache"


def _cache_path(kind: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return _cache_root() / kind / f"{digest}.json"


def read_cache(kind: str, key: str) -> dict[str, Any] | None:
    path = _cache_path(kind, key)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("usda.cache_unreadable", path=str(path))
        return None
    return data if isinstance(data, dict) else None


def write_cache(kind: str, key: str, payload: dict[str, Any]) -> None:
    path = _cache_path(kind, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written through a temporary file so an interrupted write cannot leave a
    # truncated document that later reads as a cache hit.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def _api_key() -> str:
    key = get_settings().usda_api_key
    value = key.get_secret_value().strip() if key else ""
    if not value:
        raise MissingUsdaKeyError(
            "USDA_API_KEY is not set. Get a free key at "
            "https://fdc.nal.usda.gov/api-key-signup.html and add it to .env. "
            "Foods already resolved keep their identity; only their nutrient "
            "values are waiting."
        )
    return value


async def _get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(f"{BASE_URL}{path}", params=params)
        except httpx.HTTPError as exc:
            raise UsdaError(f"Could not reach FoodData Central: {exc}") from exc

    if response.status_code == 429:
        raise UsdaError(
            "FoodData Central rate limit reached. It resets hourly; enrichment "
            "can be resumed then without losing anything."
        )
    if response.status_code >= 400:
        raise UsdaError(f"FoodData Central returned {response.status_code} for {path}.")

    payload = response.json()
    if not isinstance(payload, dict):
        raise UsdaError(f"FoodData Central returned an unexpected payload for {path}.")
    return payload


async def search_foods(
    query: str, *, page_size: int = 10, data_types: tuple[str, ...] = GENERIC_DATA_TYPES
) -> dict[str, Any]:
    """Search for candidate foods. Cached by query and data types."""
    cache_key = f"{query}|{page_size}|{','.join(data_types)}"
    cached = read_cache("search", cache_key)
    if cached is not None:
        return cached

    payload = await _get(
        "/foods/search",
        {
            "query": query,
            "pageSize": page_size,
            "dataType": ",".join(data_types),
            "api_key": _api_key(),
        },
    )
    write_cache("search", cache_key, payload)
    log.info("usda.searched", query=query, hits=payload.get("totalHits"))
    return payload


async def get_food(fdc_id: int) -> dict[str, Any]:
    """Fetch one food's full record. Cached by id.

    Worth a second request even though search already returned nutrients: this
    is the authoritative record, and it carries `foodPortions` (gram weights
    per household measure) and `foodCategory`, which search omits.

    It is *not* fetched because search is nutritionally abbreviated — for the
    two foods checked on 2026-08-18 the tracked nutrient sets were identical
    (15 for Foundation 2646170, from 22 search entries and 26 detail entries).
    USDA documents search as returning a subset, so relying on the detail call
    is still the safe default; it just is not a gap that has been observed.
    """
    cached = read_cache("food", str(fdc_id))
    if cached is not None:
        return cached

    payload = await _get(f"/food/{fdc_id}", {"api_key": _api_key()})
    write_cache("food", str(fdc_id), payload)
    log.info("usda.fetched", fdc_id=fdc_id, description=payload.get("description"))
    return payload
