"""/v2 API: lean lyrics, capabilities and provider health (ported from v2-beta).

The v1 endpoint /lyrics/ is unchanged; /v2 adds a compact schema, explicit
sync tiers, ETag/304 and small error envelopes. Both share the raw lyrics
cache and pipeline in src/routes/_lyrics_pipeline.py.
"""

from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from src.async_bridge import run_async
from src.cache import cache_stats, load_view, make_view_cache_key, save_view
from src.core import providers
from src.core.negative_cache import negative_cache
from src.core.singleflight import singleflight
from src.core.tiers import TIERS, derive_view, detect_tier, normalize_tier, tier_rank, to_lean
from src.groq_key_manager import get_key_manager
from src.groq_processor import process_lyrics
from src.logger import get_logger
from src.routes._helpers import _sanitize_text
from src.routes._lyrics_pipeline import _cached_response, _get_raw_lyrics, _serialize_json
from src.sources import ALL_FETCHERS

logger = get_logger("router")

bp = Blueprint("v2", __name__)


def _run_ai_lines(data: dict, translate: bool, romanize: bool, language: str):
    """Run Groq on the lyric lines and return aligned (translated, romanized)."""
    timed = data.get("timed_lyrics") or []
    if timed:
        lines = [str(entry.get("text", "")) for entry in timed if isinstance(entry, dict)]
    else:
        raw = data.get("lyrics") or data.get("plain_lyrics") or ""
        lines = str(raw).split("\n") if raw else []
    if not lines:
        return None, None
    groq_result = run_async(
        process_lyrics(
            lyrics_lines=lines,
            target_language=language,
            translate=translate,
            romanize=romanize,
        ),
        timeout=120,
    )
    return groq_result.get("translated"), groq_result.get("romanized")


@bp.route("/v2/lyrics", methods=["GET"])
def v2_lyrics():
    """Lean v2 lyrics endpoint with ETag/304 and tier derivation."""
    raw_artist = request.args.get("artist", "")
    raw_song = request.args.get("song", "")
    artist = _sanitize_text(raw_artist)
    song = _sanitize_text(raw_song)
    if not artist or not song:
        return jsonify({"err": "missing_artist_or_song"}), 400

    tier = normalize_tier(request.args.get("tier", "line"))
    if tier is None:
        return jsonify({"err": "bad_tier", "allowed": list(TIERS)}), 400

    allow_lower = request.args.get("allow_lower", "true").lower() != "false"
    lite = request.args.get("lite", "false").lower() == "true"
    fields_raw = request.args.get("fields", "")
    fields = {part.strip() for part in fields_raw.split(",") if part.strip()} or None
    do_translate = request.args.get("translate", "false").lower() == "true"
    do_romanize = request.args.get("romanize", "false").lower() == "true"
    target_language = _sanitize_text(request.args.get("lang", "en"), 40).lower() or "en"
    fast_mode = request.args.get("fast", "false").lower() == "true"

    timestamps = tier != "plain"
    word_level = tier in ("word", "syllable")
    syllabus = tier == "syllable"

    view_key = make_view_cache_key(
        artist,
        song,
        timestamps=timestamps,
        mood=False,
        metadata=False,
        translate=do_translate,
        romanize=do_romanize,
        language=target_language,
        word_level=word_level,
        syllabus=syllabus,
        extra={
            "v2": 1,
            "tier": tier,
            "lite": lite,
            "fields": sorted(fields) if fields else [],
        },
    )
    cached_view = load_view(view_key)
    if cached_view:
        return _cached_response(cached_view[1])

    try:
        # restrict_word_sources stays off: word/syllable requests fall back to
        # line lyrics from every source, so allow_lower=true means "best
        # available anywhere". Pending decision in #40.
        raw_result, _raw_tier, _raw_key, raw_meta = _get_raw_lyrics(
            artist,
            song,
            timestamps=timestamps,
            pass_param=False,
            sequence=None,
            fast_mode=fast_mode,
            fast_timeout=20,
            word_level=word_level,
            syllabus=syllabus,
            restrict_word_sources=False,
        )
    except TimeoutError:
        return jsonify({"err": "timeout"}), 504
    except Exception as exc:
        logger.error("v2 fetch failed for %r - %r: %s", artist, song, exc)
        return jsonify({"err": "internal"}), 500

    if raw_result is None:
        if raw_meta.get("outcome") == "not_found":
            return jsonify({"err": "not_found"}), 404
        return jsonify({"err": "upstream_unavailable"}), 503

    actual = detect_tier(raw_result)
    if tier_rank(actual) < tier_rank(tier) and not allow_lower:
        return jsonify({"err": "tier_unavailable", "tier": actual}), 404

    derived = derive_view(raw_result, tier)
    effective_tier = tier if tier_rank(tier) <= tier_rank(actual) else actual

    translated = romanized = None
    if do_translate or do_romanize:
        if not get_key_manager().has_keys:
            return jsonify({"err": "ai_unavailable"}), 503
        try:
            translated, romanized = _run_ai_lines(
                derived.get("data", derived),
                do_translate,
                do_romanize,
                target_language,
            )
        except Exception as exc:
            logger.error("v2 AI processing failed for %r - %r: %s", artist, song, exc)
            return jsonify({"err": "ai_failed"}), 502

    payload = to_lean(derived, effective_tier, lite=lite, fields=fields)
    if translated:
        payload["tr"] = translated
    if romanized:
        payload["rom"] = romanized

    body = _serialize_json(payload)
    try:
        save_view(view_key, payload, body)
    except Exception as exc:
        logger.warning("v2 view cache save failed: %s", exc)
    return _cached_response(body, cache_label="MISS")

@bp.route("/v2/capabilities", methods=["GET"])
def v2_capabilities():
    """Describe what this instance supports."""
    try:
        ai_available = get_key_manager().has_keys
    except Exception:
        ai_available = False
    return jsonify({
        "v": 2,
        "tiers": list(TIERS),
        "providers": sorted(ALL_FETCHERS.keys()),
        "ai": ai_available,
        "byok": False,
        # src/app.py always enables Flask-Compress. (The beta made it opt-in
        # via LYRICA_COMPRESS; that default is still being decided in #17.)
        "compression": True,
    })

@bp.route("/v2/health", methods=["GET"])
def v2_health():
    """Provider breaker/quota state, queue stats, and cache stats."""
    stats = cache_stats()
    stats.pop("cache_dir", None)
    return jsonify({
        "v": 2,
        "status": "ok",
        "providers": providers.snapshot(),
        "singleflight": singleflight.stats(),
        "negative_cache": negative_cache.stats(),
        "cache": stats,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
