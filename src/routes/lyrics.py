"""GET /lyrics/ — fetch lyrics, with optional mood, metadata and translation.

Translation and romanization are not separate endpoints: they are steps of
the /lyrics/ request (translate=true / romanize=true), so they live here.

Lyrics come from the shared raw cache entry for the song (see
src/routes/_lyrics_pipeline.py); the requested sync tier is derived from it
and the fully rendered response is cached as a view with an ETag.
"""

import asyncio
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from src.async_bridge import run_async
from src.cache import load_view, make_view_cache_key, save_view
from src.config import GROQ_MODEL
from src.core.tiers import derive_view, requested_tier
from src.groq_key_manager import get_key_manager
from src.groq_processor import process_lyrics
from src.logger import get_logger
from src.metadata_extractor import enhance_lyrics_with_metadata
from src.routes._helpers import _IS_PRODUCTION, _MAX_TEXT_LEN, _error_details, _sanitize_text
from src.routes._lyrics_pipeline import _cached_response, _get_raw_lyrics, _serialize_json
from src.routes.trending import trending_engine
from src.sentiment_analyzer import analyze_sentiment, analyze_word_frequency, extract_lyrics_text
from src.translation_cache import (
    load_translation_cache,
    make_translation_cache_key,
    save_translation_cache,
)
from src.user_config import get_user_config

logger = get_logger("router")

bp = Blueprint("lyrics", __name__)


@bp.route("/lyrics/", methods=["GET"])
def lyrics():
    """Fetch lyrics with optional mood analysis and metadata"""
    # ── Load config defaults (query params always win) ────────────────
    try:
        cfg = get_user_config()
    except Exception:
        cfg = None

    raw_artist = request.args.get("artist", "")
    raw_song = request.args.get("song", "")
    artist = _sanitize_text(raw_artist)
    song = _sanitize_text(raw_song)
    country = _sanitize_text(request.args.get("country", "US"), 8).upper() or "US"

    if len(raw_artist.strip()) > _MAX_TEXT_LEN or len(raw_song.strip()) > _MAX_TEXT_LEN:
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": f"artist and song must each be at most {_MAX_TEXT_LEN} characters",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            400,
        )

    # Apply config defaults when query param not explicitly supplied
    _ts_default = (cfg.default_timestamps if cfg else False)
    timestamps = (
        request.args.get("timestamps", str(_ts_default)).lower() == "true"
        or request.args.get("timestamp",  str(_ts_default)).lower() == "true"
    )
    pass_param = request.args.get("pass", "false").lower() == "true"
    sequence   = request.args.get("sequence", cfg.default_sequence if cfg else None)
    if isinstance(sequence, str) and len(sequence) > 500:
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "sequence parameter is too long",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            400,
        )
    fast_mode  = request.args.get("fast",  str(cfg.default_fast     if cfg else False)).lower() == "true"
    analyze_mood      = request.args.get("mood",     str(cfg.default_mood     if cfg else False)).lower() == "true"
    include_metadata  = request.args.get("metadata", str(cfg.default_metadata if cfg else False)).lower() == "true"
    do_translate = request.args.get("translate", str(cfg.default_translate if cfg else False)).lower() == "true"
    do_romanize  = request.args.get("romanize",  str(cfg.default_romanize  if cfg else False)).lower() == "true"
    word_level   = request.args.get("word",      str(cfg.default_word      if cfg else False)).lower() == "true"
    syllabus     = request.args.get("syllabus",  "false").lower() == "true"
    target_language = _sanitize_text(
        request.args.get("language", cfg.default_language if cfg else "en"), 40
    ).lower() or "en"
    _fast_timeout = cfg.fast_timeout if cfg else 20

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

    if pass_param and not sequence:
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Sequence parameter is required when pass=true",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            400,
        )

    logger.info(
        f"Lyrics request: {artist} - {song} (fast={fast_mode}, mood={analyze_mood}, "
        f"metadata={include_metadata}, translate={do_translate}, romanize={do_romanize}, "
        f"word={word_level}, syllabus={syllabus}, lang={target_language})"
    )

    # Record user query for analytics
    try:
        trending_engine.record_user_query(
            user_id=request.remote_addr,
            query=f"{artist} - {song}",
            country=country
        )
    except Exception as e:
        logger.warning(f"Failed to record user query: {str(e)}")

    # 1. Rendered-view cache. The body is stored pre-serialized so a hit
    #    skips tier derivation and JSON encoding entirely.
    view_key = make_view_cache_key(
        artist,
        song,
        timestamps=timestamps,
        sequence=sequence,
        mood=analyze_mood,
        metadata=include_metadata,
        translate=do_translate,
        romanize=do_romanize,
        language=target_language,
        word_level=word_level,
        syllabus=syllabus,
        pass_param=pass_param,
    )
    cached_view = load_view(view_key)
    if cached_view:
        _, cached_body = cached_view
        logger.info("View cache hit for %s - %s", artist, song)
        return _cached_response(cached_body)

    # 2. Raw lyrics entry, shared by every flag combination, with
    #    singleflight coalescing and a short negative cache.
    try:
        raw_result, _raw_tier, _raw_key, _raw_meta = _get_raw_lyrics(
            artist,
            song,
            timestamps=timestamps,
            pass_param=pass_param,
            sequence=sequence,
            fast_mode=fast_mode,
            fast_timeout=_fast_timeout,
            word_level=word_level,
            syllabus=syllabus,
        )
    except TimeoutError:
        logger.error(f"Timeout fetching lyrics for {artist} - {song}")
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Request timed out",
                    "details": "Lyrics fetch took too long",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            504,
        )
    except Exception as e:
        logger.error(f"Error fetching lyrics: {str(e)}")
        return (
            jsonify({
                "status": "error",
                "error": {
                    "message": "Failed to fetch lyrics",
                    **_error_details(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }),
            500,
        )

    if raw_result is None:
        if _raw_meta.get("outcome") == "invalid":
            return (
                jsonify({
                    "status": "error",
                    "error": {
                        "message": "Invalid response from lyrics fetcher",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                }),
                500,
            )
        logger.info("No lyrics found for %s - %s", artist, song)
        return jsonify({
            "status": "error",
            "error": {
                "message": "No lyrics found",
                "details": "All enabled fetchers returned no result",
            },
        })

    result = derive_view(
        raw_result,
        requested_tier(timestamps, word_level, syllabus),
    )

    # 3. Analyze mood if requested
    if analyze_mood and result.get("status") == "success":
        data = result.get("data", {})
        lyrics_text = extract_lyrics_text(data)

        if lyrics_text:
            try:
                sentiment = analyze_sentiment(lyrics_text)
                word_freq = analyze_word_frequency(lyrics_text, top_n=5)

                result["mood_analysis"] = {
                    "sentiment": sentiment,
                    "top_words": word_freq,
                }
                logger.info(f"Mood analysis completed for {artist} - {song}")
            except Exception as e:
                logger.warning(f"Mood analysis failed: {str(e)}")
                result["mood_analysis"] = {
                    "error": "Unable to perform mood analysis",
                    **_error_details(e),
                }
        else:
            logger.warning("Could not extract lyrics for mood analysis")
            result["mood_analysis"] = {"error": "Unable to extract lyrics for analysis"}

    # 4. Include metadata if requested
    if include_metadata and result.get("status") == "success":
        try:
            metadata_result = enhance_lyrics_with_metadata(result, artist, song)
            if asyncio.iscoroutine(metadata_result):
                metadata_result = run_async(metadata_result, timeout=30)
            result = metadata_result
            logger.info(f"Metadata enhanced for {artist} - {song}")
        except Exception as e:
            logger.warning(f"Metadata enhancement failed: {str(e)}")
            result["metadata_error"] = "Could not retrieve metadata"
            if not _IS_PRODUCTION:
                result["metadata_error"] += f": {e}"

    # 5. Translate / Romanize if requested
    if (do_translate or do_romanize) and result.get("status") == "success":
        data = result.get("data", {})

        # Check if Groq keys are configured
        if not get_key_manager().has_keys:
            return (
                jsonify({
                    "status": "error",
                    "error": {
                        "message": "Translation/romanization requires a Groq API key. "
                                   "Set GROQ_API_KEY in your .env file.",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                }),
                503,
            )

        # Check translation cache first
        trans_cache_key = make_translation_cache_key(
            artist, song,
            data.get("source", "unknown"),
            target_language,
            do_translate, do_romanize,
            data.get("hasTimestamps", False),
        )
        cached_translation = load_translation_cache(trans_cache_key)

        if cached_translation:
            # Merge cached translation into result
            result = cached_translation
            logger.info(f"Translation cache hit for {artist} - {song}")
        else:
            # Extract lyric lines for LLM processing
            has_timed = data.get("hasTimestamps", False) and data.get("timed_lyrics")

            if has_timed:
                lyrics_lines = [entry.get("text", "") for entry in data["timed_lyrics"]]
            else:
                raw_lyrics = data.get("lyrics", "")
                lyrics_lines = raw_lyrics.split("\n") if raw_lyrics else []

            if lyrics_lines:
                try:
                    groq_result = run_async(
                        process_lyrics(
                            lyrics_lines=lyrics_lines,
                            target_language=target_language,
                            translate=do_translate,
                            romanize=do_romanize,
                        ),
                        timeout=120,
                    )

                    translated_lines = groq_result.get("translated")
                    romanized_lines = groq_result.get("romanized")
                    metadata = groq_result.get("metadata", {})

                    # Add translation_metadata to data
                    data["translation_metadata"] = {
                        "target_language": metadata.get("target_language", target_language),
                        "processed_by": metadata.get("processed_by", f"groq/{GROQ_MODEL}"),
                        "cached_from": "fresh",
                    }


                    if has_timed:
                        # Synced lyrics: add romanized/translated to each timed_lyrics entry
                        for i, entry in enumerate(data["timed_lyrics"]):
                            if do_translate and translated_lines and i < len(translated_lines):
                                entry["translated"] = translated_lines[i]
                            if do_romanize and romanized_lines and i < len(romanized_lines):
                                entry["romanized"] = romanized_lines[i]
                    else:
                        # Unsynced lyrics: add as top-level strings
                        if do_translate and translated_lines:
                            data["translated_lyrics"] = "\n".join(translated_lines)
                        if do_romanize and romanized_lines:
                            data["romanized_lyrics"] = "\n".join(romanized_lines)

                    result["data"] = data

                    # Save to translation cache
                    try:
                        save_translation_cache(trans_cache_key, result)
                        logger.info(f"Translation cached for {artist} - {song}")
                    except Exception as e:
                        logger.warning(f"Translation cache save failed: {str(e)}")

                    logger.info(f"Translation/romanization completed for {artist} - {song}")

                except Exception as e:
                    logger.error(f"Translation/romanization failed: {str(e)}")
                    result["translation_error"] = "Translation processing failed"
                    if not _IS_PRODUCTION:
                        result["translation_error"] += f": {e}"

    # 6. Cache the rendered view (including mood/metadata/translation).
    if result.get("status") == "success":
        try:
            body = _serialize_json(result)
            save_view(view_key, result, body)
            logger.info(f"Result cached for {artist} - {song}")
            return _cached_response(body, cache_label="MISS")
        except Exception as e:
            logger.warning(f"Cache save failed: {str(e)}")

    return jsonify(result)
