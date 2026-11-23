"""Unit tests for the Apple Music source's offline helpers.

Covers TTML timecode parsing, TTML extraction from AMP API responses and
TTML -> timed_lyrics conversion. No network access or token needed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.sources.apple_music_fetcher import (  # noqa: E402
    _extract_ttml_from_response,
    _parse_timecode,
    _TTMLConverter,
)

# Two lines. Line 1 is three syllable spans forming two words ("Hel"+"lo",
# then "world,"); line 2 uses an hh:mm:ss timecode.
SAMPLE_TTML = (
    '<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
    '<p begin="00:01.000" end="00:03.500">'
    '<span begin="00:01.000" end="00:01.400">Hel</span>'
    '<span begin="00:01.400" end="00:02.000">lo</span> '
    '<span begin="00:02.100" end="00:03.500">world,</span>'
    "</p>"
    '<p begin="1:00:04.25" end="1:00:05"><span begin="1:00:04.25" end="1:00:05">Bye</span></p>'
    "</div></body></tt>"
)


def test_parse_timecode_formats():
    assert _parse_timecode("4.5") == 4.5
    assert _parse_timecode("01:02.5") == 62.5
    assert _parse_timecode("1:00:04.25") == 3604.25
    assert _parse_timecode("1,5") == 1.5
    assert _parse_timecode("") is None
    assert _parse_timecode(None) is None
    assert _parse_timecode("x:01") is None


def test_extract_ttml_from_response():
    assert _extract_ttml_from_response({"data": [{"attributes": {"ttml": "<tt/>"}}]}) == "<tt/>"
    assert _extract_ttml_from_response({"errors": [{"status": "404"}]}) is None
    assert _extract_ttml_from_response({"data": []}) is None
    localized = {"data": [{"attributes": {"ttmlLocalizations": {"en": "<tt>en</tt>"}}}]}
    assert _extract_ttml_from_response(localized) == "<tt>en</tt>"
    as_json = {"data": [{"attributes": {"ttmlLocalizations": '{"en": "<tt>j</tt>"}'}}]}
    assert _extract_ttml_from_response(as_json) == "<tt>j</tt>"


def test_convert_line_level():
    plain, timed, sync_level = _TTMLConverter().convert(SAMPLE_TTML)

    assert plain == "Hello world,\nBye"
    assert sync_level == "line"
    assert timed == [
        {"id": "am_0", "text": "Hello world,", "start_time": 1000, "end_time": 3500},
        {"id": "am_1", "text": "Bye", "start_time": 3604250, "end_time": 3605000},
    ]


def test_convert_empty_or_invalid_ttml():
    assert _TTMLConverter().convert("") == ("", [], "line")
    assert _TTMLConverter().convert("<not-xml") == ("", [], "line")
