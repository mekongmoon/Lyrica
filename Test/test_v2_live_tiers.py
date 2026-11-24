"""Live end-to-end checks for the v2 sync tiers (#13).

These go through the real app, fetch controller and providers, so they only
run with LYRICA_RUN_INTEGRATION=1 (see conftest.py and the Integration
workflow). They exist because the beta's word/syllable tiers were never seen
working: lrcmux failed every call and Apple Music could not load or convert
word timing. The songs below had word timing on lrcmux when these tests were
written (Deezer/KuGou sources).
"""

import os

import pytest

from src.app import create_app

pytestmark = pytest.mark.integration

_HAS_APPLE_TOKEN = bool(os.getenv("APPLE_MUSIC_DEVELOPER_TOKEN") or os.getenv("DEVELOPER_TOKEN"))


@pytest.fixture(scope="module")
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _has_word_timing(lines) -> bool:
    """v2 lean lines are [start_ms, text] or [start_ms, text, words]."""
    return any(len(line) >= 3 and line[2] for line in lines)


def test_live_v2_line_tier(client):
    response = client.get("/v2/lyrics?artist=Queen&song=Bohemian Rhapsody&tier=line")
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["tier"] == "line"
    assert len(payload["lines"]) > 10


def test_live_v2_word_tier_strict(client):
    """allow_lower=false: must be real word timing, or 404 tier_unavailable."""
    response = client.get(
        "/v2/lyrics?artist=Rick Astley&song=Never Gonna Give You Up"
        "&tier=word&allow_lower=false"
    )
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["tier"] == "word"
    assert _has_word_timing(payload["lines"])


def test_live_v1_word_request_returns_words(client):
    response = client.get("/lyrics/?artist=Coldplay&song=Yellow&timestamps=true&word=true")
    body = response.get_json()
    assert body["status"] == "success", body
    timed = body["data"]["timed_lyrics"]
    assert any(entry.get("words") for entry in timed)


@pytest.mark.skipif(not _HAS_APPLE_TOKEN, reason="needs APPLE_MUSIC_DEVELOPER_TOKEN")
def test_live_v2_syllable_tier_strict(client):
    response = client.get(
        "/v2/lyrics?artist=Adele&song=Hello&tier=syllable&allow_lower=false"
    )
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["tier"] == "syllable"
    # Syllable lines nest one level deeper: [start, text, [[start, word, [[start, syl], ...]], ...]]
    assert any(
        len(line) >= 3 and any(len(word) >= 3 and word[2] for word in line[2])
        for line in payload["lines"]
    )
