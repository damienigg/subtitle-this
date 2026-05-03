"""Tests for the Whisper-output parser used by the OpenVINO STT backend.

The OVModel.generate() output is a token sequence which the tokenizer
decodes into a string with embedded <|X.XX|> timestamp markers around
each segment. _parse_segments() walks the markers in pairs and extracts
clean (start, end, text) cues. This file exercises the boundary cases:
basic pairing, the back-to-back-marker pattern Whisper uses for short
gaps, the time_offset shift used by chunked transcription, and rejection
of empty/zero-duration segments.
"""
from app.pipeline.stt_openvino import _parse_segments


def test_parse_basic_two_segments():
    decoded = "<|0.00|> Hello world<|2.50|><|2.50|> How are you<|5.00|>"
    cues = _parse_segments(decoded, 0.0)
    assert cues == [
        (0.0, 2.5, "Hello world"),
        (2.5, 5.0, "How are you"),
    ]


def test_parse_drops_empty_segments():
    """Whisper sometimes emits <|X|><|X|> back-to-back with no text in
    between (e.g. between sentences). Those mustn't become cues."""
    decoded = "<|0.00|><|2.50|> Hello<|5.00|>"
    cues = _parse_segments(decoded, 0.0)
    assert cues == [(2.5, 5.0, "Hello")]


def test_parse_drops_zero_duration_segments():
    """If two timestamp markers are identical, the segment between them has
    zero duration — drop it rather than emit a (start == end) cue that the
    .vtt writer would render as a flicker."""
    decoded = "<|3.00|> phantom<|3.00|><|3.00|> real<|6.00|>"
    cues = _parse_segments(decoded, 0.0)
    assert cues == [(3.0, 6.0, "real")]


def test_parse_applies_time_offset():
    """Chunked transcription: the parser is called once per 30s chunk and
    must shift all timestamps by the chunk's start offset so the final cue
    list reflects absolute film positions, not chunk-local positions."""
    decoded = "<|0.00|> Second chunk<|10.00|>"
    cues = _parse_segments(decoded, 30.0)
    assert cues == [(30.0, 40.0, "Second chunk")]


def test_parse_ignores_non_numeric_tokens():
    """Whisper emits <|en|>, <|transcribe|>, <|notimestamps|> control tokens
    interleaved with the real timestamp markers. The parser's regex must
    match only numeric markers — otherwise these tokens would corrupt cue
    boundaries (e.g. someone pairing <|0.00|> with <|en|>)."""
    decoded = "<|en|><|transcribe|><|0.00|> Real text<|2.00|>"
    cues = _parse_segments(decoded, 0.0)
    assert cues == [(0.0, 2.0, "Real text")]


def test_parse_handles_no_timestamps():
    """Some chunks return no <|X|> markers at all (silence, or
    notimestamps mode). Don't crash; return empty."""
    assert _parse_segments("", 0.0) == []
    assert _parse_segments("<|notimestamps|> some text", 0.0) == []


def test_parse_strips_whitespace():
    """Trailing/leading spaces in segment text are normalized so cues
    don't render with awkward leading spaces."""
    decoded = "<|0.00|>     Hello world      <|2.00|>"
    cues = _parse_segments(decoded, 0.0)
    assert cues == [(0.0, 2.0, "Hello world")]
