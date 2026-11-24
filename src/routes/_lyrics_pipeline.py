"""Shared lyrics pipeline for the v1 and v2 lyrics routes (ported from v2-beta).

- ``_get_raw_lyrics`` resolves the richest-tier raw cache entry for a song,
  with single-flight coalescing and a negative cache for misses.
- ``_cached_response`` returns a pre-serialized JSON body with a strong ETag
  and ``304 Not Modified`` support.
"""

import hashlib

from flask import Response, current_app, request

from src.async_bridge import run_async
from src.cache import load_raw, make_lyrics_cache_key, save_raw
from src.core.negative_cache import negative_cache
from src.core.singleflight import singleflight
from src.core.tiers import detect_tier, requested_tier, tier_rank
from src.fetch_controller import fetch_lyrics_controller
from src.logger import get_logger

logger = get_logger("router")


def _serialize_json(obj) -> str:
    """Serialize with the app's orjson provider (fallback: stdlib json)."""
    try:
        return current_app.json.dumps(obj)
    except Exception:
        import json
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _cached_response(body: str, *, status: int = 200, cache_label: str = "HIT") -> Response:
    """Return a pre-serialized body with a strong ETag and 304 support."""
    etag = '"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32] + '"'
    inm = request.headers.get("If-None-Match", "")
    candidates = {part.strip() for part in inm.split(",") if part.strip()}
    if "*" in candidates or etag in candidates:
        return Response(status=304, headers={"ETag": etag, "X-Cache": cache_label})
    response = Response(body, status=status, mimetype="application/json")
    response.headers["ETag"] = etag
    response.headers["X-Cache"] = cache_label
    return response


def _has_lyrics_content(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    data = result.get("data", result)
    if not isinstance(data, dict):
        return False
    if data.get("lyrics") or data.get("plain_lyrics") or data.get("lyrics_text"):
        return True
    return bool(data.get("timed_lyrics"))


def _get_raw_lyrics(
    artist: str,
    song: str,
    *,
    timestamps: bool,
    pass_param: bool,
    sequence,
    fast_mode: bool,
    fast_timeout: int,
    word_level: bool,
    syllabus: bool,
    restrict_word_sources: bool = False,
):
    """Resolve the richest-tier raw entry with negative caching + singleflight.

    Returns ``(result, tier, raw_key, meta)``. ``result`` is ``None`` when no
    provider had the song; ``meta["outcome"]`` distinguishes a clean absence
    from an error. Raises ``TimeoutError`` / ``Exception`` so the route can map
    them to 504/500 exactly as before.

    ``restrict_word_sources`` is passed to the fetch controller; v1 leaves it
    off so word requests keep a line-level fallback (see #40).
    """
    raw_key = make_lyrics_cache_key(artist, song, sequence, pass_param)
    wanted = requested_tier(timestamps, word_level, syllabus)

    cached = load_raw(raw_key)
    if cached and tier_rank(cached[1]) >= tier_rank(wanted):
        return cached[0], cached[1], raw_key, {"outcome": "hit"}

    lower_tier_fallback = cached
    upgrade_marker = f"{raw_key}:tier:{wanted}"

    # A lower-tier entry already exists and an upgrade was attempted recently:
    # serve what we have instead of hammering providers on every request.
    if cached and negative_cache.get(upgrade_marker):
        return cached[0], cached[1], raw_key, {"outcome": "hit"}

    if negative_cache.get(raw_key):
        return None, None, raw_key, {"outcome": "not_found"}

    with singleflight.run(raw_key, timeout=70) as leader:
        if not leader:
            # The leader finished while we waited: re-check the shared cache
            # and the negative cache so duplicate requests get the same answer.
            cached = load_raw(raw_key)
            if cached:
                return cached[0], cached[1], raw_key, {"outcome": "hit"}
            if negative_cache.get(raw_key):
                return None, None, raw_key, {"outcome": "not_found"}
            return None, None, raw_key, {"outcome": "incomplete"}

        # Re-check inside the leader slot: another worker may have filled it
        # between the first lookup and acquiring the singleflight key.
        cached = load_raw(raw_key)
        if cached and tier_rank(cached[1]) >= tier_rank(wanted):
            return cached[0], cached[1], raw_key, {"outcome": "hit"}
        if not cached and negative_cache.get(raw_key):
            return None, None, raw_key, {"outcome": "not_found"}

        meta: dict = {}
        result = run_async(
            fetch_lyrics_controller(
                artist,
                song,
                timestamps=timestamps,
                pass_param=pass_param,
                sequence=sequence,
                fast_mode=fast_mode,
                fast_timeout=fast_timeout,
                word_level=word_level,
                syllabus=syllabus,
                meta=meta,
                restrict_word_sources=restrict_word_sources,
            ),
            timeout=60,
        )

        if result is not None and not isinstance(result, dict):
            meta["outcome"] = "invalid"
            return None, None, raw_key, meta

        if isinstance(result, dict) and result.get("status") == "success" and _has_lyrics_content(result):
            detected = detect_tier(result)
            # Never let a lower-tier upgrade attempt overwrite a richer entry.
            # This can happen when a word request falls back to a plain result
            # from a provider that is not the one that supplied the line entry.
            existing = load_raw(raw_key)
            if existing and tier_rank(existing[1]) >= tier_rank(detected):
                result, detected = existing[0], existing[1]
            else:
                try:
                    save_raw(raw_key, result, detected)
                except Exception as exc:
                    logger.warning("Raw cache save failed for %r - %r: %s", artist, song, exc)
            if tier_rank(detected) < tier_rank(wanted):
                negative_cache.set(upgrade_marker, "tier_unavailable")
            return result, detected, raw_key, meta

        if meta.get("outcome") == "not_found":
            negative_cache.set(raw_key, "not_found")
        elif meta.get("outcome") == "error":
            logger.warning(
                "All providers errored for %r - %r (attempts=%s, errors=%s)",
                artist,
                song,
                meta.get("attempts"),
                len(meta.get("errors", [])),
            )

        if lower_tier_fallback:
            # Remember that this tier could not be upgraded so the next request
            # for it serves the lower tier without another upstream call.
            negative_cache.set(upgrade_marker, "tier_unavailable")
            return lower_tier_fallback[0], lower_tier_fallback[1], raw_key, meta
        return None, None, raw_key, meta
