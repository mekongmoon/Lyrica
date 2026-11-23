"""Apple Music AMP API requests: headers, retries, catalog search, TTML extraction."""

import asyncio
import json
import random
from typing import Any

import httpx

from src.logger import get_logger

from .auth import _resolve_storefront

logger = get_logger("apple_music_fetcher")

# ── Constants ────────────────────────────────────────────────────────────────
_AMP_BASE_URL = "https://amp-api.music.apple.com/v1"
_PUBLIC_BASE_URL = "https://api.music.apple.com/v1"

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 3

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

_UA = "Lyrica/1.0 (https://github.com/Wilooper/Lyrica)"


def _build_am_headers(developer_token: str, user_token: str | None = None) -> dict[str, str]:
    """Construct request headers for the Apple Music AMP API."""
    headers = {
        "Origin": "https://music.apple.com",
        "Accept": "application/json",
        "User-Agent": _UA,
        "Authorization": f"Bearer {developer_token}",
    }
    if user_token:
        headers["Music-User-Token"] = user_token
    return headers


def _backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Exponential backoff with jitter (seconds)."""
    delay = min(cap, base * (2 ** attempt))
    return delay + random.uniform(0, delay * 0.25)


async def _am_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> dict | None:
    """Send an AMP API request with retries, backoff, and rate-limit handling.

    Returns parsed JSON dict or None on failure.
    """
    attempt = 0
    while True:
        try:
            response = await client.request("GET", url, headers=headers, params=params)
        except httpx.TimeoutException:
            if attempt >= max_retries:
                logger.error(f"Apple Music: request timeout for {url}")
                return None
            attempt += 1
            await asyncio.sleep(_backoff_delay(attempt))
            continue
        except httpx.HTTPError as exc:
            if attempt >= max_retries:
                logger.error(f"Apple Music: transport error for {url}: {exc}")
                return None
            attempt += 1
            await asyncio.sleep(_backoff_delay(attempt))
            continue

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after and retry_after.replace(".", "", 1).isdigit()
                else _backoff_delay(attempt)
            )
            if attempt >= max_retries:
                logger.warning(f"Apple Music: rate limited (429) after {attempt + 1} attempts")
                return None
            attempt += 1
            await asyncio.sleep(delay)
            continue

        if response.status_code in _RETRYABLE_STATUS_CODES:
            if attempt >= max_retries:
                logger.warning(
                    f"Apple Music: HTTP {response.status_code} for {url} "
                    f"after {attempt + 1} attempts"
                )
                return None
            attempt += 1
            await asyncio.sleep(_backoff_delay(attempt))
            continue

        if response.status_code == 404:
            logger.info(f"Apple Music: no lyrics found (404)")
            return None

        if response.status_code != 200:
            logger.warning(f"Apple Music: HTTP {response.status_code} for {url}")
            return None

        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            logger.error(f"Apple Music: invalid JSON response: {exc}")
            return None


async def _am_search_songs(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    storefront: str,
    term: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search the Apple Music catalog for songs matching a query term.

    Returns a list of {"id", "name", "artistName", "albumName"} dicts.
    """
    url = f"{_PUBLIC_BASE_URL}/catalog/{storefront}/search"
    params = {"term": term, "types": "songs", "limit": limit}
    data = await _am_request(client, url, headers, params)
    if not data:
        return []

    songs = (data.get("results") or {}).get("songs") or {}
    results: list[dict[str, Any]] = []
    for song in songs.get("data") or []:
        attrs = song.get("attributes") or {}
        if not song.get("id") or not isinstance(attrs, dict):
            continue
        results.append({
            "id": str(song["id"]),
            "name": str(attrs.get("name", "")),
            "artistName": str(attrs.get("artistName", "")),
            "albumName": attrs.get("albumName"),
        })
    return results


async def _am_resolve_storefront(
    client: httpx.AsyncClient,
    headers: dict[str, str],
) -> str:
    """Resolve the storefront from /v1/me/storefront (requires user token)."""
    url = f"{_PUBLIC_BASE_URL}/me/storefront"
    data = await _am_request(client, url, headers)
    if not data:
        return _resolve_storefront()
    storefronts = data.get("data") or []
    if storefronts and isinstance(storefronts[0], dict):
        sf = storefronts[0].get("id")
        if sf:
            return str(sf).lower()
    return _resolve_storefront()


def _extract_ttml_from_response(body: dict) -> str | None:
    """Extract the TTML document string from a syllable-lyrics JSON envelope."""
    errors = body.get("errors")
    if errors:
        logger.warning(f"Apple Music: API error in response: {errors}")
        return None

    data = body.get("data")
    if not isinstance(data, list) or not data:
        return None

    first = data[0]
    if not isinstance(first, dict):
        return None

    attributes = first.get("attributes") or {}
    ttml = attributes.get("ttml")
    if ttml is None:
        ttml = _extract_ttml_from_localizations(attributes)
    if not isinstance(ttml, str) or not ttml.strip():
        return None
    return ttml


def _extract_ttml_from_localizations(attributes: dict) -> str | None:
    """Resolve TTML from attributes.ttmlLocalizations (dict or JSON string)."""
    value = attributes.get("ttmlLocalizations")
    if value is None:
        return None
    if isinstance(value, dict):
        for v in value.values():
            if isinstance(v, str) and v.strip():
                return v
        return None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.startswith("<"):
        return value
    try:
        decoded = json.loads(stripped)
        if isinstance(decoded, dict):
            for v in decoded.values():
                if isinstance(v, str) and v.strip():
                    return v
    except json.JSONDecodeError:
        return value
    return value
