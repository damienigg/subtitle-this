"""Tests for the Emby/Jellyfin shared client + the neutral MediaItem
abstraction. The two server types share an implementation because their
REST APIs are functionally identical."""
import pytest

from app.server.base import MediaItem, MediaStream
from app.server.emby_jellyfin import EmbyJellyfinClient, _stream_from_payload
from app.server import MediaServerError


def _subtitle_stream(language: str) -> MediaStream:
    return MediaStream(type="subtitle", language=language)


def test_has_subtitle_track_two_letter_match():
    item = MediaItem(id="1", name="x", path="/x.mkv", type="Movie",
                    streams=[_subtitle_stream("en")])
    assert item.has_subtitle_track("en") is True
    assert item.has_subtitle_track("fr") is False


def test_has_subtitle_track_three_letter_match():
    """Emby and Jellyfin commonly tag subs with ISO 639-2 codes ('eng' not 'en')."""
    item = MediaItem(id="1", name="x", path="/x.mkv", type="Movie",
                    streams=[_subtitle_stream("eng")])
    assert item.has_subtitle_track("en") is True


def test_has_subtitle_track_case_insensitive():
    item = MediaItem(id="1", name="x", path="/x.mkv", type="Movie",
                    streams=[_subtitle_stream("ENG")])
    assert item.has_subtitle_track("en") is True


def test_has_subtitle_track_no_subs():
    item = MediaItem(id="1", name="x", path="/x.mkv", type="Movie", streams=[])
    assert item.has_subtitle_track("en") is False


def test_has_subtitle_track_only_audio_streams():
    item = MediaItem(id="1", name="x", path="/x.mkv", type="Movie",
                    streams=[MediaStream(type="audio", language="en")])
    assert item.has_subtitle_track("en") is False


def test_client_rejects_missing_creds():
    with pytest.raises(MediaServerError):
        EmbyJellyfinClient("", "")
    with pytest.raises(MediaServerError):
        EmbyJellyfinClient("http://x", "")


def test_stream_from_payload_normalizes_type():
    """Emby's MediaStreams field uses capitalized type names ('Audio',
    'Subtitle'); we lowercase them on ingest so MediaItem.has_subtitle_track
    works regardless of casing."""
    s = _stream_from_payload({"Type": "Subtitle", "Language": "fra"})
    assert s.type == "subtitle"
    assert s.language == "fra"


def test_stream_from_payload_unknown_type_becomes_other():
    """Defensive: if Emby ever returns a stream type we don't model
    (e.g. 'Data', 'Attachment'), categorize it as 'other' so the rest of
    the pipeline ignores it cleanly."""
    s = _stream_from_payload({"Type": "Attachment", "Language": "und"})
    assert s.type == "other"
