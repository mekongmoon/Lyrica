"""
sources/__init__.py

Exposes ALL_FETCHERS: a dict of fetcher name → fetcher instance.

Fetchers are instantiated lazily (at import time here, but each fetcher's
internal clients are lazy too) so a missing optional dependency like
lyricsgenius or ytmusicapi won't crash the whole server — it will just
make that fetcher silently unavailable.

Source status (as of 2026-07-26):
  ACTIVE:
    genius       — requires GENIUS_TOKEN env var (optional)
    lrclib       — free, synced + plain, very reliable
    youtube      — 3-layer (ytmusicapi [auth] -> transcript-api -> yt-dlp)
                    auto-detects headers_auth.json / cookies.txt for auth
                    all layers route through Webshare rotating proxy
    netease      — via syncedlyrics; synced LRC, large catalog
    megalobiz    — via syncedlyrics; synced LRC, user-contributed
    musixmatch   — via syncedlyrics; loaded only when MUSIXMATCH_TOKEN is set
    lrcmux       — api.lrcmux.dev aggregator (Deezer/KuGou/YT Music); line & word-level sync, no token
    apple_music  — requires APPLE_MUSIC_DEVELOPER_TOKEN (syllable/word/line sync)

  REMOVED (deleted; see git history): simpmusic, lyricsovh, chartlyrics,
  lyricsfreek — their upstream APIs were unreliable or offline.
"""
import os
from src.logger import get_logger

logger = get_logger("sources")

ALL_FETCHERS: dict = {}

def _try_import(name: str, module: str, cls: str):
    try:
        mod = __import__(module, fromlist=[cls])
        instance = getattr(mod, cls)()
        ALL_FETCHERS[name] = instance
        logger.info(f"Fetcher loaded: {name}")
    except Exception as e:
        logger.warning(f"Fetcher '{name}' unavailable: {e}")

# ── Active fetchers ──────────────────────────────────────────────────────────
_try_import("genius",     "src.sources.genius_fetcher",      "GeniusFetcher")
_try_import("lrclib",     "src.sources.lrclib_fetcher",      "LRCLIBFetcher")
_try_import("youtube",    "src.sources.youtube_fetcher",     "YoutubeFetcher")
_try_import("netease",    "src.sources.netease_fetcher",     "NetEaseFetcher")
_try_import("megalobiz",  "src.sources.megalobiz_fetcher",   "MegalobizFetcher")
_try_import("lrcmux",     "src.sources.lrcmux_fetcher",      "LrcmuxFetcher")

# ── Musixmatch (requires a token) ────────────────────────────────────────────
# The unauthenticated endpoint returns 401, so do not even instantiate the
# fetcher unless the operator supplied MUSIXMATCH_TOKEN.
if os.getenv("MUSIXMATCH_TOKEN"):
    _try_import("musixmatch", "src.sources.musixmatch_fetcher", "MusixmatchFetcher")
else:
    logger.info("Musixmatch fetcher skipped (set MUSIXMATCH_TOKEN to enable)")

# ── Apple Music (requires developer token) ───────────────────────────────────
# Only loaded when APPLE_MUSIC_DEVELOPER_TOKEN or DEVELOPER_TOKEN is set.
# Apple Music provides syllable-level, word-level, and line-level synced lyrics
# (no plain/unsynced lyrics). Requires an Apple Music developer JWT token.
if os.getenv("APPLE_MUSIC_DEVELOPER_TOKEN") or os.getenv("DEVELOPER_TOKEN"):
    _try_import("apple_music", "src.sources.apple_music_fetcher", "AppleMusicFetcher")
else:
    logger.info(
        "Apple Music fetcher skipped (set APPLE_MUSIC_DEVELOPER_TOKEN to enable)"
    )

logger.info(f"Active fetchers: {list(ALL_FETCHERS.keys())}")
