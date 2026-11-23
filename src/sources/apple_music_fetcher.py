"""
src/sources/apple_music_fetcher.py

Fetches syllable-timed lyrics from Apple Music's private AMP API.

Adapted from the LyricaAME-py project. This is a self-contained, dependency-free
native integration — no external package required. It searches the Apple Music
catalog by artist + song title, resolves the song ID, then fetches the
syllable-lyrics TTML document from the private AMP endpoint.

Apple Music provides three granularities of synced lyrics:
  1. Syllable-level — per-syllable timing (requires &syllabus=true)
  2. Word-level     — per-word timing (requires &word=true&timestamps=true)
  3. Line-level     — per-line timing (requires &timestamps=true)

Apple Music does NOT provide plain (unsynced) lyrics, so this fetcher always
returns None when timestamps=False.

Environment variables:
  APPLE_MUSIC_DEVELOPER_TOKEN (or DEVELOPER_TOKEN) — Apple Music JWT developer
                                                      token (required to use this source)
  MUSIC_USER_TOKEN                                   — Music User Token (optional;
                                                      enables storefront auto-resolution)
  APPLE_STOREFRONT                                   — default storefront code (default: us)
  APPLE_LYRICS_LANGUAGE                              — lyrics localization (default: en-gb)
  APPLE_LYRICS_SCRIPT                                — lyrics script variant (default: en-Latn)

Usage in sequence parameter:
  8, apple_music, or APPLE_MUSIC (case-insensitive)
"""

import asyncio
import os
from typing import Any

import httpx

from src.logger import get_logger
from src.proxy_manager import get_proxy_manager

from .apple_music.api import (
    _AMP_BASE_URL,
    _UA,
    _am_request,
    _am_resolve_storefront,
    _am_search_songs,
    _build_am_headers,
    _extract_ttml_from_response,
)
from .apple_music.auth import (
    _resolve_developer_token,
    _resolve_lyrics_language,
    _resolve_lyrics_script,
    _resolve_storefront,
    _resolve_user_token,
)
from .apple_music.ttml import _TTMLConverter
from .base_fetcher import BaseFetcher, build_result

logger = get_logger("apple_music_fetcher")

# ── Fetcher ──────────────────────────────────────────────────────────────────

class AppleMusicFetcher(BaseFetcher):
    source_name = "apple_music"

    def __init__(self):
        self._developer_token = _resolve_developer_token()
        self._user_token = _resolve_user_token()
        self._storefront = _resolve_storefront()
        self._lyrics_language = _resolve_lyrics_language()
        self._lyrics_script = _resolve_lyrics_script()

        if self._developer_token:
            logger.info(
                f"Apple Music: developer token configured "
                f"(storefront={self._storefront})"
            )
        else:
            logger.warning(
                "Apple Music: APPLE_MUSIC_DEVELOPER_TOKEN (or DEVELOPER_TOKEN) "
                "not set — this source will be skipped. "
                "Set the env var to enable Apple Music lyrics."
            )

    async def fetch(
        self,
        artist: str,
        song: str,
        timestamps: bool = False,
        word_level: bool = False,
        syllabus: bool = False,
    ):
        """Fetch lyrics from Apple Music.

        Args:
            artist:      Artist name.
            song:        Song title.
            timestamps:  Must be True — Apple Music does not provide plain
                        lyrics. Returns None when False.
            word_level:  If True (and timestamps=True), include per-word timing.
            syllabus:    If True (and timestamps=True), include per-syllable
                        timing. Apple Music is the only source that supports this.

        Returns:
            build_result dict or None.
        """
        # Apple Music only provides synced (TTML) lyrics — no plain text.
        if not timestamps:
            logger.debug("Apple Music: skipped (timestamps=false — no plain lyrics)")
            return None

        if not self._developer_token:
            logger.debug("Apple Music: skipped (no developer token)")
            return None

        # Resolve storefront: from env, or auto-resolve via user token
        storefront = self._storefront
        headers = _build_am_headers(self._developer_token, self._user_token)

        proxy = get_proxy_manager().get_next()
        transport = httpx.AsyncHTTPTransport(retries=3)
        client_kwargs: dict[str, Any] = {
            "transport": transport,
            "timeout": httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=2.0),
            "headers": {"User-Agent": _UA},
            "follow_redirects": True,
        }
        if proxy:
            client_kwargs["proxy"] = proxy

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                # ── Auto-resolve storefront if not explicitly set ────────────
                if not os.getenv("APPLE_STOREFRONT") and not self._storefront:
                    storefront = await _am_resolve_storefront(client, headers)

                # ── Step 1: Search catalog for the song ──────────────────────
                term = f"{song} {artist}"
                logger.info(
                    f"Apple Music: searching '{term}' "
                    f"(storefront={storefront})"
                )

                songs = await _am_search_songs(client, headers, storefront, term, limit=5)
                if not songs:
                    logger.info(f"Apple Music: no results for '{term}'")
                    return None

                # ── Step 2: Try each search result for lyrics ────────────────
                # Prefer exact artist match
                artist_lower = artist.lower()
                ranked = []
                for s in songs:
                    s_artist = s.get("artistName", "").lower()
                    if artist_lower in s_artist:
                        ranked.insert(0, s)
                    else:
                        ranked.append(s)

                ttml_text = None
                matched_song = None
                for song_info in ranked[:3]:
                    song_id = song_info.get("id")
                    if not song_id:
                        continue

                    # Build per-request headers (storefront may change)
                    req_headers = _build_am_headers(
                        self._developer_token, self._user_token
                    )

                    url = (
                        f"{_AMP_BASE_URL}/catalog/{storefront}/songs/{song_id}"
                        "/syllable-lyrics"
                    )
                    params = {
                        "l[lyrics]": self._lyrics_language,
                        "l[script]": self._lyrics_script,
                        "extend": "ttmlLocalizations",
                    }

                    data = await _am_request(client, url, req_headers, params)
                    if not data:
                        continue

                    ttml_text = _extract_ttml_from_response(data)
                    if ttml_text:
                        matched_song = song_info
                        break

                if not ttml_text:
                    logger.info(
                        f"Apple Music: no syllable lyrics found for '{artist} - {song}'"
                    )
                    return None

                # ── Step 3: Parse TTML into Lyrica format ────────────────────
                converter = _TTMLConverter()
                plain_text, timed_lyrics, sync_level = converter.convert(
                    ttml_text,
                    word_level=word_level,
                    syllabus=syllabus,
                )

                if not plain_text:
                    logger.info(f"Apple Music: parsed lyrics are empty for '{artist} - {song}'")
                    return None

                # If syllable requested but not available, downgrade
                if syllabus and sync_level != "syllable":
                    logger.info(
                        f"Apple Music: syllable-level not available "
                        f"(sync_level={sync_level}), returning best available"
                    )

                # If word-level requested but not available, downgrade
                if word_level and sync_level == "line":
                    logger.info(
                        "Apple Music: word-level not available, "
                        "returning line-level"
                    )

                result = build_result(
                    source="apple_music",
                    artist=matched_song.get("artistName", artist) if matched_song else artist,
                    title=matched_song.get("name", song) if matched_song else song,
                    lyrics=plain_text,
                    timed_lyrics=timed_lyrics if timestamps else None,
                    has_timestamps=bool(timed_lyrics),
                    album=matched_song.get("albumName") if matched_song else None,
                    song_id=matched_song.get("id") if matched_song else None,
                    storefront=storefront,
                    sync_level=sync_level,
                )

                logger.info(
                    f"Apple Music: success for '{artist} - {song}' "
                    f"(sync_level={sync_level}, "
                    f"lines={len(timed_lyrics)})"
                )
                return result

        except asyncio.TimeoutError:
            logger.warning(f"Apple Music timeout for '{artist} - {song}'")
            return None
        except httpx.TimeoutException:
            logger.warning(f"Apple Music HTTP timeout for '{artist} - {song}'")
            return None
        except httpx.ConnectError as e:
            logger.error(f"Apple Music connection error: {e}")
            if proxy:
                get_proxy_manager().mark_failure(proxy)
            return None
        except Exception as e:
            logger.error(f"Apple Music error: {e}")
            if proxy:
                get_proxy_manager().mark_failure(proxy)
            return None
