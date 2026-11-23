"""GET /api/jiosaavn/* — JioSaavn search and stream URLs."""

import asyncio
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from src.async_bridge import run_async
from src.logger import get_logger
from src.routes._helpers import _error_details, _sanitize_text
from src.sources.jiosaavan_fetcher import get_jiosaavn_stream, search_jiosaavn

logger = get_logger("router")

bp = Blueprint("jiosaavn", __name__)


@bp.route("/api/jiosaavn/search", methods=["GET"])
def jiosaavn_search():
    """Search for songs on JioSaavn"""
    query = _sanitize_text(request.args.get("q", ""))

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

    logger.info(f"JioSaavn search query: {query}")

    try:
        results = search_jiosaavn(query)
        if asyncio.iscoroutine(results):
            results = run_async(results, timeout=30)
        return jsonify({"status": "success", "results": results})
    except asyncio.TimeoutError:
        logger.error(f"Timeout searching JioSaavn for: {query}")
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Request timed out",
                    "details": "JioSaavn search took too long",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            504,
        )
    except Exception as e:
        logger.error(f"JioSaavn search error: {str(e)}")
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Failed to search JioSaavn",
                    **_error_details(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            500,
        )

@bp.route("/api/jiosaavn/play", methods=["GET"])
def jiosaavn_play():
    """Get playable stream URL from JioSaavn"""
    song_link = _sanitize_text(request.args.get("songLink", ""), 2048)

    if not song_link:
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "songLink parameter is required",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            400,
        )

    logger.info(f"JioSaavn play request for: {song_link}")

    try:
        data = get_jiosaavn_stream(song_link)
        if asyncio.iscoroutine(data):
            data = run_async(data, timeout=30)

        if not data or not isinstance(data, dict):
            return (
                jsonify({
                    "status": "error",
                    "error": {
                        "message": "Invalid response from JioSaavn",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                }),
                500,
            )

        if not data.get("stream_url"):
            return (
                jsonify({
                    "status": "error",
                    "error": {
                        "message": "Unable to fetch stream URL",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                }),
                500,
            )

        return jsonify({"status": "success", "data": data})
    except asyncio.TimeoutError:
        logger.error(f"Timeout fetching stream for: {song_link}")
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Request timed out",
                    "details": "Stream fetch took too long",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            504,
        )
    except Exception as e:
        logger.error(f"JioSaavn play error: {str(e)}")
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Failed to fetch stream",
                    **_error_details(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            500,
        )
