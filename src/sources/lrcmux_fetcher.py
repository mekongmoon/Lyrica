"""
src/sources/lrcmux_fetcher.py

Fetches lyrics from the lrcmux API (https://api.lrcmux.dev), an aggregator
that returns line- and word-synchronized lyrics without a token.

lrcmux picks the upstream provider itself (currently Deezer, KuGou or
YouTube Music; see GET /sources). Set LRCMUX_SOURCES to restrict it. A
hard-coded filter broke when lrcmux dropped its Musixmatch provider: every
request then failed with HTTP 400 "no providers match the requested filter".

Sync levels:
  - timestamps=True, word_level=False (default) → requests line-level sync (level=line)
  - timestamps=True, word_level=True            → requests word-level sync  (level=word)
  - timestamps=False                            → plain text only (no level restriction)
"""

import httpx
from src.config import LRCMUX_API_URL, LRCMUX_SOURCES
from src.logger import get_logger
from src.proxy_manager import get_proxy_manager
from .base_fetcher import BaseFetcher, build_result

logger = get_logger("lrcmux_fetcher")

_UA = "Lyrica/1.0 (https://github.com/Wilooper/Lyrica)"


class LrcmuxFetcher(BaseFetcher):
    source_name = "lrcmux"

    async def fetch(self, artist: str, song: str, timestamps: bool = False, word_level: bool = False):
        """
        Fetch lyrics from lrcmux.

        Args:
            artist:     Artist name
            song:       Song title
            timestamps: If True, include synchronized timed lyrics
            word_level: If True (and timestamps=True), request word-level sync.
                        If False (default), request line-level sync.
        """
        proxy = get_proxy_manager().get_next()
        transport = httpx.AsyncHTTPTransport(retries=3)
        client_kwargs = {
            "transport": transport,
            "timeout": httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=2.0),
            "headers": {"User-Agent": _UA},
            "follow_redirects": True,
        }
        if proxy:
            client_kwargs["proxy"] = proxy

        url = f"{LRCMUX_API_URL.rstrip('/')}/get"

        params: dict = {
            "artist": artist,
            "title": song,
            "format": "json",
        }
        if LRCMUX_SOURCES:
            params["sources"] = LRCMUX_SOURCES

        # Only restrict sync level when timestamps are requested
        if timestamps:
            params["level"] = "word" if word_level else "line"

        sync_label = ("word" if word_level else "line") if timestamps else "none"

        async with httpx.AsyncClient(**client_kwargs) as client:
            try:
                logger.info(
                    f"Lrcmux: fetching '{artist} \u2013 {song}' "
                    f"(sync={sync_label}, proxy={proxy is not None})"
                )

                resp = await client.get(url, params=params)

                if resp.status_code == 404:
                    logger.info("Lrcmux: no lyrics found")
                    return None
                if resp.status_code != 200:
                    # lrcmux returns RFC 7807 problem JSON; log the reason so
                    # API changes are diagnosable from logs alone.
                    logger.warning(
                        f"Lrcmux returned status code {resp.status_code}: {resp.text[:200]}"
                    )
                    return None

                data = resp.json()
                lines_data = data.get("lines") or []
                if not lines_data:
                    logger.info("Lrcmux: no lyrics content in response")
                    return None

                # ── Extract plain text + timed lyrics ────────────────────────
                plain_lines = []
                timed_lines = []

                for i, line in enumerate(lines_data):
                    text = line.get("text") or ""
                    plain_lines.append(text)

                    start = line.get("start")
                    end = line.get("end")

                    if start is not None:
                        timed_line = {
                            "text": text,
                            "start_time": start,
                            "end_time": end if end is not None else (start + 4000),
                            "id": f"lrc_{i}",
                        }
                        # Preserve word-level data when present
                        if line.get("words") is not None:
                            timed_line["words"] = line["words"]
                        timed_lines.append(timed_line)

                plain_lyrics = "\n".join(plain_lines)
                if not plain_lyrics.strip():
                    logger.info("Lrcmux: parsed lyrics content is empty")
                    return None

                use_timed = timed_lines if (timestamps and timed_lines) else None

                track_info = data.get("track") or {}
                meta = data.get("meta") or {}

                result = build_result(
                    source="lrcmux",
                    artist=track_info.get("artist") or artist,
                    title=track_info.get("title") or song,
                    lyrics=plain_lyrics,
                    timed_lyrics=use_timed,
                    has_timestamps=bool(use_timed),
                    album=track_info.get("album"),
                    duration=track_info.get("duration"),
                    isrc=track_info.get("isrc"),
                    sync_level=meta.get("level"),
                )

                logger.info(
                    f"Lrcmux: success (hasTimestamps={result['hasTimestamps']}, "
                    f"sync_level={meta.get('level', 'unknown')})"
                )
                return result

            except httpx.TimeoutException:
                logger.error("Lrcmux timeout")
                if proxy:
                    get_proxy_manager().mark_failure(proxy)
                return None
            except httpx.ConnectError as e:
                logger.error(f"Lrcmux connection error: {e}")
                if proxy:
                    get_proxy_manager().mark_failure(proxy)
                return None
            except Exception as e:
                logger.error(f"Lrcmux error: {e}")
                if proxy:
                    get_proxy_manager().mark_failure(proxy)
                return None
