"""Unit tests for the Lyrica v2 core building blocks (src/core/).

Ported from the v2-beta suite (Test/test_v2_core.py on upstream
v2-beta-rollout), plus tests for the LRC parser and LRCLIB fetcher. Tests
for the cache, fetch controller and /v2 routes are added as those parts are
ported (#12). No network access is required.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import src.fetch_controller as fetch_controller
from src.cache import (
    clear_cache,
    load_from_cache,
    load_raw,
    load_view,
    make_cache_key,
    make_lyrics_cache_key,
    make_view_cache_key,
    save_raw,
    save_view,
)
from src.core import providers
from src.core.negative_cache import negative_cache
from src.core.normalizer import normalize_identity
from src.core.singleflight import singleflight
from src.core.tiers import derive_view, detect_tier, to_lean
from src.core.verifier import verify_result
from src.sources import ALL_FETCHERS
from src.sources.apple_music_fetcher import AppleMusicFetcher
from src.sources.base_fetcher import parse_lrc
from src.sources.lrclib_fetcher import LRCLIBFetcher
from src.sources.lrcmux_fetcher import LrcmuxFetcher


class _FakeFetcher:
    def __init__(self, result=None, delay: float = 0.0):
        self.result = result
        self.delay = delay
        self.calls = 0

    async def fetch(self, artist, song, timestamps=False, **kwargs):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


@pytest.fixture()
def client():
    from src.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
    return app.test_client()


@pytest.fixture()
def registry(monkeypatch):
    """Replace the provider registry with fakes (restored afterwards)."""
    saved = dict(ALL_FETCHERS)
    ALL_FETCHERS.clear()
    monkeypatch.setattr(fetch_controller, "_SOURCE_ORDER", ["fake"])
    providers.reset()
    negative_cache.clear()
    try:
        yield ALL_FETCHERS
    finally:
        ALL_FETCHERS.clear()
        ALL_FETCHERS.update(saved)
        providers.reset()
        negative_cache.clear()


def _line_result(artist="Test Artist", title="Test Song"):
    return {
        "source": "fake",
        "artist": artist,
        "title": title,
        "lyrics": "line one\nline two",
        "hasTimestamps": True,
        "duration": 30,
        "timed_lyrics": [
            {"id": "lrc_0", "text": "line one", "start_time": 1000, "end_time": 5000},
            {"id": "lrc_1", "text": "line two", "start_time": 5000, "end_time": 9000},
        ],
    }


def _word_result():
    return {
        "source": "fake",
        "artist": "Test Artist",
        "title": "Test Song",
        "lyrics": "line one",
        "hasTimestamps": True,
        "sync_level": "syllable",
        "timed_lyrics": [{
            "id": "lrc_0",
            "text": "line one",
            "start_time": 1000,
            "end_time": 3000,
            "words": [{
                "text": "line",
                "start": 1000,
                "end": 2000,
                "syllables": [
                    {"text": "li", "start": 1000, "end": 1500},
                    {"text": "ne", "start": 1500, "end": 2000},
                ],
            }],
        }],
    }


# ── Normalizer / verifier ────────────────────────────────────────────────────

def test_normalizer_collapses_edition_noise():
    assert normalize_identity("The  Beatles", "Hey Jude (Remastered)") == ("the beatles", "hey jude")
    assert normalize_identity("Daft Punk", "Get Lucky (feat. Pharrell Williams)") == ("daft punk", "get lucky")
    assert normalize_identity("ARTIST", "Song - Live")[1] == "song"
    assert normalize_identity("ARTIST", "Song (Official Video)")[1] == "song"
    assert normalize_identity("ARTIST", "Song - Official Music Video")[1] == "song"
    assert normalize_identity("ARTIST", "Song - 2011 Remaster")[1] == "song"
    # Legitimate titles that merely contain a noise keyword must survive.
    assert normalize_identity("Artist", "Live Forever") == ("artist", "live forever")
    assert normalize_identity("Artist", "I Live") == ("artist", "i live")


def test_verifier_rejects_wrong_song():
    verdict = verify_result(
        "Talwiinder",
        "Nasha",
        {"artist": "Ed Sheeran", "title": "Shape of You", "lyrics": "The club isn't the best place"},
    )
    assert verdict["accepted"] is False
    assert verdict["confidence"] < 0.6


def test_verifier_accepts_correct_result():
    verdict = verify_result("Coldplay", "Yellow", _line_result("Coldplay", "Yellow"))
    assert verdict["accepted"] is True
    assert verdict["confidence"] >= 0.85


def test_verifier_rejects_boilerplate_and_html():
    boilerplate = {"artist": "A", "title": "T", "lyrics": "Lyrics not available"}
    html = {"artist": "A", "title": "T", "lyrics": "<html><body>502</body></html>"}
    assert verify_result("A", "T", boilerplate)["accepted"] is False
    assert verify_result("A", "T", html)["accepted"] is False


# ── Tiers / lean payload ─────────────────────────────────────────────────────

def test_tier_derivation_never_upgrades():
    word = {"status": "success", "data": _word_result()}
    assert detect_tier(word) == "syllable"

    line = derive_view(word, "line")
    assert "words" not in line["data"]["timed_lyrics"][0]

    plain = derive_view(word, "plain")
    assert plain["data"]["hasTimestamps"] is False
    assert "timed_lyrics" not in plain["data"]
    assert plain["data"]["lyrics"]

    # A plain entry can never be turned into timed lines.
    plain_raw = {"status": "success", "data": {"source": "x", "lyrics": "a\nb"}}
    assert "timed_lyrics" not in derive_view(plain_raw, "line")["data"]


def test_lean_payload_is_small_and_compact():
    lines = []
    for i in range(60):
        lines.append({
            "id": f"lrc_{i}",
            "text": f"lyric line number {i}",
            "start_time": i * 3000,
            "end_time": (i + 1) * 3000,
        })
    result = {"status": "success", "data": {
        "source": "fake", "artist": "A", "title": "T", "duration": 180,
        "hasTimestamps": True, "timed_lyrics": lines,
    }}
    lean = to_lean(result, "line")
    assert lean["lines"][0] == [0, "lyric line number 0"]
    assert "id" not in lean["lines"][0]
    assert len(lean["lines"]) == 60
    assert len(json.dumps(lean)) < 5000


def test_lean_word_and_syllable_nesting():
    lean = to_lean({"status": "success", "data": _word_result()}, "syllable")
    start, text, words = lean["lines"][0]
    assert (start, text) == (1000, "line one")
    assert words[0][1] == "line"
    assert words[0][2][0][1] == "li"


# ── LRC parser / LRCLIB fetcher ──────────────────────────────────────────────

def test_parse_lrc_multiple_tags_offset_and_sorting():
    lrc = "[ar:Artist]\n[offset:500]\n[00:01.00][00:10.50]Chorus\n[00:05.2]Verse"
    parsed = parse_lrc(lrc)
    assert [entry["start_time"] for entry in parsed] == [1500, 5700, 11000]
    assert parsed[0]["end_time"] == 5700
    assert parsed[-1]["end_time"] == 15000  # last line start + 4s


def test_parse_lrc_repeated_tags_do_not_leak_into_text():
    parsed = parse_lrc("[00:10.00][01:20.00]Chorus line")
    assert [(e["start_time"], e["text"]) for e in parsed] == [
        (10000, "Chorus line"),
        (80000, "Chorus line"),
    ]


@pytest.mark.asyncio
async def test_lrclib_identifies_requests_without_changing_shared_client(monkeypatch):
    requests = []

    def handle(request):
        requests.append((request.url.path, request.headers["User-Agent"]))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), headers={"User-Agent": "shared"}) as client:
        monkeypatch.setattr("src.sources.lrclib_fetcher.get_http_client", lambda: client)
        await LRCLIBFetcher().fetch("Artist", "Song")
        assert client.headers["User-Agent"] == "shared"

    assert [path for path, _ in requests] == ["/api/get", "/api/search"]
    assert all(agent.startswith("Lyrica/") for _, agent in requests)


def _lrclib_fetcher(data_by_artist):
    """LRCLIBFetcher whose /api/get returns data only for known artists."""
    fetcher = LRCLIBFetcher()
    calls = []

    async def fake_get(client, params):
        calls.append(params)
        return data_by_artist.get(params.get("artist_name"))

    fetcher._get = fake_get
    fetcher._search = AsyncMock(return_value=[])
    return fetcher, calls


def test_lrclib_plain_request_strips_timestamps():
    """A plain request (timestamps=False) with only syncedLyrics must strip tags."""
    fetcher, _ = _lrclib_fetcher({"Test": {
        "artistName": "Test",
        "trackName": "Song",
        "duration": 30,
        "syncedLyrics": "[00:01.00]Hello world\n[00:05.00]Second line",
        "plainLyrics": None,
        "instrumental": False,
    }})

    result = asyncio.run(fetcher._fetch_with_client(MagicMock(), "Test", "Song", timestamps=False))
    assert result is not None
    assert result["hasTimestamps"] is False
    assert "timed_lyrics" not in result
    assert result["lyrics"] == "Hello world\nSecond line"


def test_lrclib_timed_request_has_clean_text_and_real_duration():
    """lyrics is plain text (no LRC tags); the last line ends at the song's end.

    LRCLIB reports duration in seconds. It used to be passed to parse_lrc as
    milliseconds, so the last line of a 30 s song "ended" at 30 ms.
    """
    fetcher, _ = _lrclib_fetcher({"Test": {
        "artistName": "Test",
        "trackName": "Song",
        "duration": 30,
        "syncedLyrics": "[00:01.00]Hello world\n[00:05.00]Second line",
        "plainLyrics": None,
    }})

    result = asyncio.run(fetcher._fetch_with_client(MagicMock(), "Test", "Song", timestamps=True))
    assert result["hasTimestamps"] is True
    assert result["lyrics"] == "Hello world\nSecond line"
    assert result["timed_lyrics"][-1]["end_time"] == 30000


def test_lrclib_search_fallback_picks_best_candidate_not_first():
    good = {"artistName": "Coldplay", "trackName": "Yellow", "plainLyrics": "Look at the stars"}
    fetcher, calls = _lrclib_fetcher({"Coldplay": good})
    # Exact lookup misses (requested artist is misspelled), so search runs.
    fetcher._search = AsyncMock(return_value=[
        {"trackName": "Yellow Submarine", "artistName": "The Beatles"},
        {"trackName": "Yellow", "artistName": "Coldplay"},
    ])

    result = asyncio.run(fetcher._fetch_with_client(MagicMock(), "Coldpaly", "Yellow", timestamps=False))
    assert result is not None and result["artist"] == "Coldplay"
    assert calls[1]["artist_name"] == "Coldplay"  # best match tried first


# ── Cache: raw records and views ─────────────────────────────────────────────

def test_raw_cache_key_ignores_view_flags():
    a = make_lyrics_cache_key("The  Beatles", "Hey Jude", sequence=None)
    b = make_lyrics_cache_key("the beatles", "hey jude", sequence=None)
    assert a == b
    # The rendered view key must still distinguish flags.
    v1 = make_view_cache_key("The Beatles", "Hey Jude", timestamps=True)
    v2 = make_view_cache_key("The Beatles", "Hey Jude", timestamps=False)
    assert v1 != v2


def test_v1_cache_key_ignores_fast_and_edition_noise():
    """The v1 route still calls make_cache_key(artist, song, timestamps,
    sequence, fast, mood, metadata, ...). fast only changes latency, so it
    must not split the cache; edition suffixes map to the same song."""
    base = make_cache_key("The Beatles", "Hey Jude", True, None, False, False, False)
    assert make_cache_key("The Beatles", "Hey Jude", True, None, True, False, False) == base
    assert make_cache_key("The Beatles", "Hey Jude (Remastered)", True, None, False, False, False) == base
    assert make_cache_key("The Beatles", "Hey Jude", False, None, False, False, False) != base


def test_raw_and_view_records_round_trip():
    result = {"status": "success", "data": {"source": "fake", "lyrics": "a\nb"}}

    raw_key = make_lyrics_cache_key("Round Trip Artist", "Raw Song")
    save_raw(raw_key, result, tier="word")
    assert load_raw(raw_key) == (result, "word")
    # Old-style readers still see the plain result.
    assert load_from_cache(raw_key) == result

    view_key = make_view_cache_key("Round Trip Artist", "View Song", timestamps=True)
    save_view(view_key, result)
    loaded, body = load_view(view_key)
    assert loaded == result
    assert json.loads(body) == result


def test_clear_cache_clears_negative_entries():
    negative_cache.set("some-key", "not_found")
    assert negative_cache.get("some-key") is not None
    result = clear_cache()
    assert negative_cache.get("some-key") is None
    assert "negative_items_cleared" in result


# ── Fetch controller ─────────────────────────────────────────────────────────

def _phase_sources(monkeypatch, **controller_kwargs):
    """Run the controller with three fakes and return each phase's sources."""
    ALL_FETCHERS["lrcmux"] = _FakeFetcher(None)
    ALL_FETCHERS["lrclib"] = _FakeFetcher(None)
    ALL_FETCHERS["genius"] = _FakeFetcher(None)
    monkeypatch.setattr(fetch_controller, "_SOURCE_ORDER", ["lrclib", "lrcmux", "genius"])

    seen: list[list[str]] = []

    async def fake_try_phase(phase, *args, **kwargs):
        seen.append(list(phase["sources"]))
        return None

    monkeypatch.setattr(fetch_controller, "_try_phase", fake_try_phase)
    asyncio.run(fetch_controller.fetch_lyrics_controller(
        "WordArtist", "WordSong", timestamps=True, word_level=True, **controller_kwargs
    ))
    return seen


def test_word_sync_restricted_to_word_capable_providers(registry, monkeypatch):
    seen = _phase_sources(monkeypatch, restrict_word_sources=True)
    assert seen
    assert all(set(sources) <= {"lrcmux", "apple_music"} for sources in seen)


def test_word_request_keeps_line_fallback_by_default(registry, monkeypatch):
    """v1 behaviour: a word request still ends with a line-level phase across
    all sources, so songs without word timing get line lyrics, not 404."""
    seen = _phase_sources(monkeypatch)
    assert seen[0] == ["lrcmux"]  # word-capable providers first
    assert seen[-1] == ["lrclib", "lrcmux", "genius"]  # then everyone, line level


def test_controller_rejects_wrong_song(registry):
    ALL_FETCHERS["fake"] = _FakeFetcher({
        "source": "fake", "artist": "Ed Sheeran", "title": "Shape of You",
        "lyrics": "The club isn't the best place to find a lover",
    })
    meta: dict = {}
    result = asyncio.run(fetch_controller.fetch_lyrics_controller("Talwiinder", "Nasha", meta=meta))
    assert result["status"] == "error"
    assert meta["outcome"] == "rejected"
    assert meta["rejected"][0]["provider"] == "fake"


def test_controller_skips_provider_with_open_breaker(registry):
    fake = _FakeFetcher({"source": "fake", "artist": "A", "title": "T", "lyrics": "x\ny\nz"})
    ALL_FETCHERS["fake"] = fake
    state = providers.get_state("fake")
    for _ in range(5):
        state.record_failure("boom")

    meta: dict = {}
    result = asyncio.run(fetch_controller.fetch_lyrics_controller("A", "T", meta=meta))
    assert result["status"] == "error"
    assert fake.calls == 0
    assert meta["outcome"] == "incomplete"
    assert meta["skipped"][0]["reason"] == "breaker_open"


@pytest.mark.parametrize("fetcher_cls", [LrcmuxFetcher, AppleMusicFetcher])
def test_word_capable_fetchers_accept_controller_kwargs(fetcher_cls):
    """The controller calls lrcmux and Apple Music with word_level= and
    syllabus=. lrcmux used to lack syllabus, so every call raised TypeError
    and the source silently never worked through the API."""
    inspect.signature(fetcher_cls.fetch).bind(
        None, "Artist", "Song", timestamps=True, word_level=True, syllabus=False
    )


def test_musixmatch_not_loaded_without_token():
    if os.getenv("MUSIXMATCH_TOKEN"):
        pytest.skip("MUSIXMATCH_TOKEN is set in this environment")
    assert "musixmatch" not in ALL_FETCHERS


# ── v1 /lyrics/ on the raw cache ─────────────────────────────────────────────

def test_missing_song_is_negative_cached(client, registry):
    fake = _FakeFetcher(None)
    registry["fake"] = fake

    url = "/lyrics/?artist=NegCacheArtist&song=Missing&sequence=fake"
    first = client.get(url)
    assert first.status_code == 200
    assert first.get_json()["status"] == "error"
    assert fake.calls == 1

    second = client.get(url)
    assert second.status_code == 200
    assert fake.calls == 1  # zero upstream calls on the second request


def test_wrong_song_result_is_rejected_and_not_cached(client, registry):
    wrong = _FakeFetcher({
        "source": "fake",
        "artist": "Someone Else",
        "title": "A Different Song",
        "lyrics": "completely unrelated words here",
        "hasTimestamps": False,
    })
    registry["fake"] = wrong

    artist, song = "WrongSongArtist", "Wanted Song"
    response = client.get(f"/lyrics/?artist={artist}&song={song}&sequence=fake")
    assert response.get_json()["status"] == "error"
    assert wrong.calls == 1
    assert load_raw(make_lyrics_cache_key(artist, song, "fake")) is None


def test_one_raw_fetch_serves_plain_and_line_views(client, registry):
    fake = _FakeFetcher(_line_result("CacheArtist", "CacheSong"))
    registry["fake"] = fake

    base = "/lyrics/?artist=CacheArtist&song=CacheSong&sequence=fake"
    timed = client.get(base + "&timestamps=true")
    assert timed.get_json()["data"]["hasTimestamps"] is True
    assert fake.calls == 1

    plain = client.get(base)
    data = plain.get_json()["data"]
    assert data["hasTimestamps"] is False
    assert "timed_lyrics" not in data
    assert data["lyrics"] == "line one\nline two"
    assert fake.calls == 1  # the raw entry was derived, not refetched


def test_100_concurrent_identical_requests_fetch_once(client, registry):
    fake = _FakeFetcher(_line_result("StampedeArtist", "StampedeSong"), delay=0.2)
    registry["fake"] = fake
    url = "/lyrics/?artist=StampedeArtist&song=StampedeSong&sequence=fake&timestamps=true"
    statuses: list[int] = []
    lock = threading.Lock()

    def worker():
        local_client = client.application.test_client()
        response = local_client.get(url)
        with lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert fake.calls == 1
    assert statuses == [200] * 100


def test_v1_view_cache_etag_and_304(client, registry):
    fake = _FakeFetcher(_line_result("EtagArtist", "EtagSong"))
    registry["fake"] = fake
    url = "/lyrics/?artist=EtagArtist&song=EtagSong&sequence=fake&timestamps=true"

    first = client.get(url)
    assert first.status_code == 200
    assert first.headers["ETag"]
    second = client.get(url)
    assert second.headers.get("X-Cache") == "HIT"
    assert second.get_json() == first.get_json()

    cached = client.get(url, headers={"If-None-Match": first.headers["ETag"]})
    assert cached.status_code == 304


def test_v1_word_request_falls_back_to_line_from_other_source(client, registry):
    """No word timing anywhere: v1 still returns line lyrics (see #40).

    The sequence is explicit because the route otherwise uses the config
    default (IDs 1-8), of which only lrcmux exists in this fake registry.
    """
    registry["lrcmux"] = _FakeFetcher(None)
    registry["fake"] = _FakeFetcher(_line_result("FallbackArtist", "FallbackSong"))

    response = client.get(
        "/lyrics/?artist=FallbackArtist&song=FallbackSong"
        "&timestamps=true&word=true&sequence=lrcmux,fake"
    )
    body = response.get_json()
    assert body["status"] == "success"
    assert body["data"]["source"] == "fake"
    assert body["data"]["hasTimestamps"] is True


# ── /v2 routes ───────────────────────────────────────────────────────────────

def test_concurrent_missing_song_gets_consistent_not_found(client, registry):
    fake = _FakeFetcher(None, delay=0.2)
    registry["fake"] = fake
    url = "/v2/lyrics?artist=ConcurrentMissing&song=Nothing"
    codes: list[int] = []
    lock = threading.Lock()

    def worker():
        response = client.application.test_client().get(url)
        with lock:
            codes.append(response.status_code)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert fake.calls == 1
    assert codes == [404] * 20


def test_lower_tier_upgrade_does_not_overwrite_richer_cache(client, registry, monkeypatch):
    artist, song = "MonotonicArtist", "MonotonicSong"
    key = make_lyrics_cache_key(artist, song)
    save_raw(key, {"status": "success", "data": _line_result(artist, song)}, "line")

    # A word request reaches lrcmux, which only returns plain text here.
    plain = {
        "source": "fake",
        "artist": artist,
        "title": song,
        "lyrics": "plain only lyrics that are long enough",
        "hasTimestamps": False,
    }
    registry["lrcmux"] = _FakeFetcher(plain)
    monkeypatch.setattr(fetch_controller, "_SOURCE_ORDER", ["lrcmux"])

    response = client.get(f"/v2/lyrics?artist={artist}&song={song}&tier=word")
    assert response.status_code == 200
    assert response.get_json()["tier"] == "line"
    assert registry["lrcmux"].calls >= 1  # the upgrade was really attempted
    cached = load_raw(key)
    assert cached is not None and cached[1] == "line"


def test_v2_lean_payload_and_tier_guard(client, registry):
    artist, song = "V2Artist", "V2Song"
    result = {"status": "success", "data": _line_result(artist, song)}
    save_raw(make_lyrics_cache_key(artist, song), result, "line")

    response = client.get(f"/v2/lyrics?artist={artist}&song={song}&tier=line")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["v"] == 2
    assert payload["tier"] == "line"
    assert payload["lines"][0] == [1000, "line one"]
    assert "timed_lyrics" not in payload
    assert response.headers["ETag"]

    not_modified = client.get(
        f"/v2/lyrics?artist={artist}&song={song}&tier=line",
        headers={"If-None-Match": response.headers["ETag"]},
    )
    assert not_modified.status_code == 304

    # A plain-only cached entry cannot satisfy a word request unless allowed.
    plain_artist, plain_song = "V2PlainArtist", "V2PlainSong"
    plain = {"status": "success", "data": {
        "source": "fake", "artist": plain_artist, "title": plain_song, "lyrics": "a\nb",
    }}
    save_raw(make_lyrics_cache_key(plain_artist, plain_song), plain, "plain")
    registry["fake"] = _FakeFetcher(None)
    blocked = client.get(
        f"/v2/lyrics?artist={plain_artist}&song={plain_song}&tier=word&allow_lower=false"
    )
    assert blocked.status_code == 404
    assert blocked.get_json()["err"] == "tier_unavailable"

    # The failed upgrade is remembered briefly; no repeated upstream call.
    client.get(
        f"/v2/lyrics?artist={plain_artist}&song={plain_song}&tier=word&allow_lower=false"
    )
    assert registry["fake"].calls == 1


def test_v2_rejects_bad_input(client):
    missing = client.get("/v2/lyrics?artist=OnlyArtist")
    assert missing.status_code == 400
    assert missing.get_json() == {"err": "missing_artist_or_song"}

    bad_tier = client.get("/v2/lyrics?artist=A&song=B&tier=karaoke")
    assert bad_tier.status_code == 400
    assert bad_tier.get_json()["err"] == "bad_tier"


def test_v2_capabilities_match_server_behaviour(client):
    caps = client.get("/v2/capabilities").get_json()
    assert caps["v"] == 2
    assert caps["tiers"] == ["plain", "line", "word", "syllable"]
    assert caps["providers"] == sorted(ALL_FETCHERS)

    # The compression flag must describe what the server actually does.
    index = client.get("/", headers={"Accept-Encoding": "gzip"})
    compressed = index.headers.get("Content-Encoding") == "gzip"
    assert caps["compression"] is compressed


def test_v2_health_reports_providers_and_hides_cache_dir(client):
    providers.reset()
    providers.get_state("health-probe").record_failure("boom")
    health = client.get("/v2/health").get_json()
    assert health["v"] == 2 and health["status"] == "ok"
    assert "health-probe" in health["providers"]
    assert {"singleflight", "negative_cache", "cache"} <= set(health)
    assert "cache_dir" not in health["cache"]


# ── v2 response contract (frozen for v2.0.0, see docs/api/v2.md) ────────────
#
# These tests pin the exact v2 payloads. If one fails, the change breaks
# existing v2 clients: after v2.0.0 that requires a v3, not a test update.

def _line_payload_source():
    return {"status": "success", "data": _line_result()}


def test_contract_line_payload():
    assert to_lean(_line_payload_source(), "line") == {
        "v": 2,
        "src": "fake",
        "tier": "line",
        "artist": "Test Artist",
        "title": "Test Song",
        "dur": 30000,
        "lines": [[1000, "line one"], [5000, "line two"]],
    }


def test_contract_plain_payload_uses_text():
    assert to_lean(_line_payload_source(), "plain") == {
        "v": 2,
        "src": "fake",
        "tier": "plain",
        "artist": "Test Artist",
        "title": "Test Song",
        "dur": 30000,
        "text": "line one\nline two",
    }


def test_contract_word_and_syllable_payloads():
    word_source = {"status": "success", "data": _word_result()}
    assert to_lean(word_source, "word") == {
        "v": 2,
        "src": "fake",
        "tier": "word",
        "artist": "Test Artist",
        "title": "Test Song",
        "lines": [[1000, "line one", [[1000, "line"]]]],
    }
    assert to_lean(word_source, "syllable")["lines"] == [
        [1000, "line one", [[1000, "line", [[1000, "li"], [1500, "ne"]]]]],
    ]


def test_contract_lite_fields_and_null_omission():
    assert to_lean(_line_payload_source(), "line", lite=True) == {
        "v": 2,
        "tier": "line",
        "lines": [[1000, "line one"], [5000, "line two"]],
    }
    # "v" and "tier" always survive a fields filter.
    assert to_lean(_line_payload_source(), "line", fields={"src"}) == {
        "v": 2, "src": "fake", "tier": "line",
    }
    # Missing optional fields are omitted, never sent as null.
    bare = {"status": "success", "data": {"timed_lyrics": [
        {"text": "only line", "start_time": 0, "end_time": 1000},
    ]}}
    assert to_lean(bare, "line") == {"v": 2, "tier": "line", "lines": [[0, "only line"]]}


class _RaisingFetcher:
    async def fetch(self, *args, **kwargs):
        raise RuntimeError("provider exploded")


def test_contract_error_envelopes(client, registry):
    assert client.get("/v2/lyrics?artist=OnlyArtist").get_json() == {"err": "missing_artist_or_song"}
    assert client.get("/v2/lyrics?artist=A&song=B&tier=karaoke").get_json() == {
        "err": "bad_tier", "allowed": ["plain", "line", "word", "syllable"],
    }

    registry["fake"] = _FakeFetcher(None)
    missing = client.get("/v2/lyrics?artist=ContractMissing&song=Nothing")
    assert (missing.status_code, missing.get_json()) == (404, {"err": "not_found"})

    registry["fake"] = _RaisingFetcher()
    broken = client.get("/v2/lyrics?artist=ContractBroken&song=Errors")
    assert (broken.status_code, broken.get_json()) == (503, {"err": "upstream_unavailable"})

    artist, song = "ContractPlain", "OnlyPlain"
    save_raw(make_lyrics_cache_key(artist, song), {"status": "success", "data": {
        "source": "fake", "artist": artist, "title": song, "lyrics": "a\nb",
    }}, "plain")
    strict = client.get(f"/v2/lyrics?artist={artist}&song={song}&tier=line&allow_lower=false")
    assert (strict.status_code, strict.get_json()) == (404, {"err": "tier_unavailable", "tier": "plain"})


def test_contract_etag_and_cache_headers(client, registry):
    artist, song = "ContractEtag", "Headers"
    save_raw(make_lyrics_cache_key(artist, song), _line_payload_source(), "line")
    url = f"/v2/lyrics?artist={artist}&song={song}"

    first = client.get(url)
    assert re.fullmatch(r'"[0-9a-f]{32}"', first.headers["ETag"])
    assert first.headers["X-Cache"] == "MISS"
    assert first.mimetype == "application/json"

    second = client.get(url)
    assert second.headers["X-Cache"] == "HIT"
    assert second.headers["ETag"] == first.headers["ETag"]

    not_modified = client.get(url, headers={"If-None-Match": first.headers["ETag"]})
    assert not_modified.status_code == 304
    assert not_modified.data == b""


def test_contract_translation_fields(client, registry, monkeypatch):
    import src.routes.v2 as v2_routes

    artist, song = "ContractTr", "Translated"
    save_raw(make_lyrics_cache_key(artist, song), _line_payload_source(), "line")

    monkeypatch.setattr(v2_routes, "get_key_manager", lambda: MagicMock(has_keys=False))
    unavailable = client.get(f"/v2/lyrics?artist={artist}&song={song}&translate=true")
    assert (unavailable.status_code, unavailable.get_json()) == (503, {"err": "ai_unavailable"})

    monkeypatch.setattr(v2_routes, "get_key_manager", lambda: MagicMock(has_keys=True))
    monkeypatch.setattr(
        v2_routes, "_run_ai_lines",
        lambda data, translate, romanize, language: (["uno", "dos"], ["ro one", "ro two"]),
    )
    payload = client.get(
        f"/v2/lyrics?artist={artist}&song={song}&translate=true&romanize=true&lang=es"
    ).get_json()
    # "tr" and "rom" are lists aligned one-to-one with "lines".
    assert payload["tr"] == ["uno", "dos"]
    assert payload["rom"] == ["ro one", "ro two"]
    assert len(payload["tr"]) == len(payload["lines"])


# ── Singleflight ─────────────────────────────────────────────────────────────

def test_singleflight_runs_leader_once():
    calls = 0
    lock = threading.Lock()

    def worker():
        nonlocal calls
        with singleflight.run("singleflight-test", timeout=5) as leader:
            if leader:
                with lock:
                    calls += 1
                time.sleep(0.1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == 1


# ── Provider safety ──────────────────────────────────────────────────────────

def test_breaker_opens_and_recovers_after_probes():
    providers.reset()
    state = providers.get_state("breaker-test")
    for _ in range(5):
        state.record_failure("boom")
    assert state.snapshot()["state"] == "open"

    allowed, reason = state.try_acquire()
    assert allowed is False and reason == "breaker_open"

    state.opened_at -= 3600  # simulate cooldown expiry
    allowed, _ = state.try_acquire()
    assert allowed is True  # half-open probe

    state.record_success(0.05)
    state.record_success(0.05)
    assert state.snapshot()["state"] == "closed"


def test_rejected_and_cancelled_probes_do_not_wedge_breaker():
    providers.reset()
    state = providers.get_state("probe-test")
    for _ in range(5):
        state.record_failure("boom")
    state.opened_at -= 3600

    assert state.try_acquire()[0] is True  # half-open probe
    state.record_rejected("wrong_song")
    assert state.try_acquire()[0] is True  # probe slot was released

    state.record_aborted()  # e.g. fast-mode task cancellation
    assert state.try_acquire()[0] is True

def test_provider_errors_are_redacted():
    providers.reset()
    state = providers.get_state("redact-test")
    state.record_failure("GET https://api.example/x?access_token=super-secret failed")
    last_error = state.snapshot()["last_error"]
    assert "super-secret" not in last_error
    assert "<redacted>" in last_error

    assert "abc123" not in providers.redact("Authorization: Bearer abc123")


def test_quota_bucket_stops_at_limit(monkeypatch):
    monkeypatch.setenv("LYRICA_PROVIDER_QUOTATEST_RPM", "1")
    providers.reset()
    state = providers.get_state("quotatest")
    assert state.try_acquire()[0] is True
    allowed, reason = state.try_acquire()
    assert allowed is False and reason == "quota_exhausted"
