"""GET /metadata/ and GET /suggestion — song metadata and title search."""

import asyncio
from datetime import datetime, timezone

import httpx as _httpx
from flask import Blueprint, jsonify, request

from src.async_bridge import run_async
from src.logger import get_logger
from src.metadata_extractor import get_metadata_only
from src.routes._helpers import _MAX_TEXT_LEN, _error_details, _int_arg, _sanitize_text

logger = get_logger("router")

bp = Blueprint("metadata", __name__)

# Shared blocking client for the one legacy sync endpoint (/suggestion).
# httpx.Client is thread-safe; reuse avoids a TCP/TLS handshake per request.
_SUGGESTION_CLIENT = _httpx.Client(
    timeout=_httpx.Timeout(connect=3.0, read=7.0, write=3.0, pool=2.0),
    headers={"User-Agent": "Lyrica/1.5.0 (https://github.com/Wilooper/Lyrica)"},
    follow_redirects=True,
)


@bp.route("/metadata/", methods=["GET"])
def metadata():
    """Get song metadata only (without lyrics)"""
    raw_artist = request.args.get("artist", "")
    raw_song = request.args.get("song", "")
    artist = _sanitize_text(raw_artist)
    song = _sanitize_text(raw_song)

    if len(raw_artist.strip()) > _MAX_TEXT_LEN or len(raw_song.strip()) > _MAX_TEXT_LEN:
        return jsonify({
            "status": "error",
            "error": {
                "message": f"artist and song must each be at most {_MAX_TEXT_LEN} characters",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }), 400

    if not artist or not song:
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Artist and song name are required",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            400,
        )

    logger.info(f"Metadata request for {artist} - {song}")
    
    try:
        result = get_metadata_only(artist, song)
        if asyncio.iscoroutine(result):
            result = run_async(result, timeout=30)
    except TimeoutError:
        logger.error(f"Timeout fetching metadata for {artist} - {song}")
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Request timed out",
                    "details": "Metadata fetch took too long",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            504,
        )
    except Exception as e:
        logger.error(f"Metadata fetch error: {str(e)}")
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Failed to fetch metadata",
                    **_error_details(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            500,
        )

    return jsonify(result)


@bp.route("/suggestion", methods=["GET"])
def suggestion():
    """Search MusicBrainz for songs matching a query and return title + artist"""
    query = _sanitize_text(request.args.get("q", ""))
    limit = _int_arg("limit", 10, 1, 100)

    if not query:
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Query parameter 'q' is required",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            400,
        )

    logger.info(f"Suggestion request: q={query}, limit={limit}")

    try:
        resp = _SUGGESTION_CLIENT.get(
            "https://musicbrainz.org/ws/2/recording/",
            params={"query": query, "fmt": "json", "limit": limit},
        )
        resp.raise_for_status()
        data = resp.json()
    except _httpx.TimeoutException:
        logger.error(f"Timeout querying MusicBrainz for: {query}")
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Request timed out while contacting MusicBrainz",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            504,
        )
    except Exception as e:
        logger.error(f"MusicBrainz suggestion error: {str(e)}")
        # The client request was valid; the upstream service failed. A 5xx
        # here is expected and must not look like an unhandled application
        # crash (500), so surface it as 502 Bad Gateway.
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Suggestion service is temporarily unavailable",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            502,
        )

    recordings = data.get("recordings", [])
    results = []
    for rec in recordings:
        title = rec.get("title", "")
        artist_credits = rec.get("artist-credit", [])
        artist_parts = []
        for credit in artist_credits:
            if isinstance(credit, dict) and "artist" in credit:
                artist_parts.append(credit["artist"].get("name", ""))
                artist_parts.append(credit.get("joinphrase", ""))
            elif isinstance(credit, str):
                artist_parts.append(credit)
        artist = "".join(artist_parts).strip() or "Unknown Artist"
        results.append({"title": title, "artist": artist})

    return jsonify({
        "status": "success",
        "query": query,
        "limit": limit,
        "total": len(results),
        "results": results,
    })
