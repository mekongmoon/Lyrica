"""API index, web app page and favicon."""

from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template

from src import __version__
from src.logger import get_logger

logger = get_logger("router")

bp = Blueprint("core", __name__)


@bp.route("/")
def home():
    """Main API documentation endpoint"""
    return jsonify(
        {
            "api": "Lyrica",
            "version": current_app.config.get("VERSION", __version__),
            "status": "active",
            "description": "A comprehensive lyrics API with mood analysis, metadata extraction, and trending insights",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "endpoints": {
                "lyrics": {
                    "url": "/lyrics/",
                    "method": "GET",
                    "description": "Fetch lyrics for a song",
                    "examples": [
                        "/lyrics/?artist=The Beatles&song=Imagine",
                        "/lyrics/?artist=The Beatles&song=Imagine&timestamps=true",
                        "/lyrics/?artist=The Beatles&song=Imagine&mood=true",
                        "/lyrics/?artist=The Beatles&song=Imagine&metadata=true",
                        "/lyrics/?artist=The Beatles&song=Imagine&translate=true&language=en",
                        "/lyrics/?artist=The Beatles&song=Imagine&romanize=true&language=hindi",
                        "/lyrics/?artist=The Beatles&song=Imagine&timestamps=true&translate=true&romanize=true&language=en",
                        "/lyrics/?artist=The Beatles&song=Imagine&fast=true&timestamps=true&mood=true&metadata=true"
                    ]
                },
                "metadata_only": {
                    "url": "/metadata/",
                    "method": "GET",
                    "description": "Get song metadata without lyrics",
                    "examples": [
                        "/metadata/?artist=The Beatles&song=Imagine"
                    ]
                },
                "trending": {
                    "url": "/trending/",
                    "method": "GET",
                    "description": "Get trending songs by country",
                    "examples": [
                        "/trending/?country=US&limit=20",
                        "/trending/?country=IN",
                        "/trending/?countries=US,GB,IN&limit=10"
                    ]
                },
                "top_queries": {
                    "url": "/analytics/top-queries/",
                    "method": "GET",
                    "description": "Get top user queries globally or by country",
                    "examples": [
                        "/analytics/top-queries/?limit=20",
                        "/analytics/top-queries/?country=US&limit=10",
                        "/analytics/top-queries/?country=US&days=7&limit=15"
                    ]
                },
                "trending_by_country": {
                    "url": "/analytics/trending-by-country/",
                    "method": "GET",
                    "description": "Get top queries for each country",
                    "examples": [
                        "/analytics/trending-by-country/?limit=10"
                    ]
                },
                "trending_vs_queries": {
                    "url": "/analytics/trending-vs-queries/",
                    "method": "GET",
                    "description": "Compare trending songs with top user queries",
                    "examples": [
                        "/analytics/trending-vs-queries/?country=US&limit=10"
                    ]
                },
                "trending_intersection": {
                    "url": "/analytics/trending-intersection/",
                    "method": "GET",
                    "description": "Find queries that match trending songs",
                    "examples": [
                        "/analytics/trending-intersection/?country=US&limit=10"
                    ]
                },
                "jiosaavn_search": {
                    "url": "/api/jiosaavn/search",
                    "method": "GET",
                    "description": "Search for songs on JioSaavn",
                    "examples": [
                        "/api/jiosaavn/search?q=Imagine"
                    ]
                },
                "jiosaavn_play": {
                    "url": "/api/jiosaavn/play",
                    "method": "GET",
                    "description": "Get playable stream URL from JioSaavn",
                    "examples": [
                        "/api/jiosaavn/play?songLink=<song_link>"
                    ]
                },
                "cache_stats": {
                    "url": "/cache/stats",
                    "method": "GET",
                    "description": "Get cache statistics"
                },
                "suggestion": {
                    "url": "/suggestion",
                    "method": "GET",
                    "description": "Search for songs by name and return matching titles with their artist",
                    "examples": [
                        "/suggestion?q=Imagine",
                        "/suggestion?q=Imagine&limit=5"
                    ]
                },
                "music_app": {
                    "url": "/app",
                    "method": "GET",
                    "description": "Access the web-based music application"
                }
            },
            "parameters": {
                "artist": {
                    "type": "string",
                    "required": True,
                    "description": "Artist name"
                },
                "song": {
                    "type": "string",
                    "required": True,
                    "description": "Song title"
                },
                "country": {
                    "type": "string",
                    "required": False,
                    "description": "Country code (US, GB, IN, BR, JP, DE, FR, CA, AU, MX)"
                },
                "countries": {
                    "type": "string",
                    "required": False,
                    "description": "Comma-separated country codes"
                },
                "limit": {
                    "type": "integer",
                    "required": False,
                    "default": 20,
                    "description": "Number of results to return"
                },
                "days": {
                    "type": "integer",
                    "required": False,
                    "description": "Time window in days for query analytics"
                },
                "timestamps": {
                    "type": "boolean",
                    "required": False,
                    "default": False,
                    "description": "Include synchronized timestamps with lyrics"
                },
                "mood": {
                    "type": "boolean",
                    "required": False,
                    "default": False,
                    "description": "Analyze song mood/sentiment and top words"
                },
                "metadata": {
                    "type": "boolean",
                    "required": False,
                    "default": False,
                    "description": "Include song metadata (cover art, duration, genre, etc.)"
                },
                "fast": {
                    "type": "boolean",
                    "required": False,
                    "default": False,
                    "description": "Use parallel fetching for faster results"
                },
                "translate": {
                    "type": "boolean",
                    "required": False,
                    "default": False,
                    "description": "Translate lyrics to target language (requires GROQ_API_KEY)"
                },
                "romanize": {
                    "type": "boolean",
                    "required": False,
                    "default": False,
                    "description": "Romanize/transliterate lyrics to target language script (requires GROQ_API_KEY)"
                },
                "language": {
                    "type": "string",
                    "required": False,
                    "default": "en",
                    "description": "Target language for translate/romanize (e.g., en, hindi, spanish, japanese)"
                },
                "word": {
                    "type": "boolean",
                    "required": False,
                    "default": False,
                    "description": "Request word-level synced lyrics (e.g., from Lrcmux or Apple Music)"
                },
                "syllabus": {
                    "type": "boolean",
                    "required": False,
                    "default": False,
                    "description": "Request syllable-level synced lyrics (Apple Music only)"
                }
            },
            "fetchers": {
                "1": "Genius (optional GENIUS_TOKEN; falls back to public search)",
                "2": "LRCLIB",
                "3": "YouTube Music (3-layer: ytmusicapi [authenticated] / transcript-api / yt-dlp)",
                "4": "NetEase (via syncedlyrics, synced LRC)",
                "5": "Megalobiz (via syncedlyrics, synced LRC)",
                "6": "Musixmatch (via syncedlyrics; requires MUSIXMATCH_TOKEN, otherwise skipped)",
                "7": "Lrcmux (via api.lrcmux.dev: Deezer, KuGou and YouTube Music lyrics; supports word-level sync with &word=true)",
                "8": "Apple Music (requires APPLE_MUSIC_DEVELOPER_TOKEN; syllable-level with &syllabus=true, word-level with &word=true)",
            }
        }
    )


@bp.route("/app", methods=["GET"])
def app_page():
    """Serve the web-based music application"""
    try:
        return render_template("index.html")
    except Exception as e:
        logger.error(f"Failed to render app page: {str(e)}")
        return jsonify({
            "status": "error",
            "error": {
                "message": "Failed to load application",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }), 500


@bp.route("/favicon.ico", methods=["GET"])
def favicon():
    """Favicon endpoint"""
    return "", 204
