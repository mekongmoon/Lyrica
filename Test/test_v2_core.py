"""Unit tests for the Lyrica v2 core building blocks (src/core/).

Ported from the v2-beta suite (Test/test_v2_core.py on upstream
v2-beta-rollout), plus tests for the LRC parser and LRCLIB fetcher. Tests
for the cache, fetch controller and /v2 routes are added as those parts are
ported (#12). No network access is required.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock

from src.core import providers
from src.core.normalizer import normalize_identity
from src.core.singleflight import singleflight
from src.core.tiers import derive_view, detect_tier, to_lean
from src.core.verifier import verify_result
from src.sources.base_fetcher import parse_lrc
from src.sources.lrclib_fetcher import LRCLIBFetcher


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
