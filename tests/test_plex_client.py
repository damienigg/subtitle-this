"""Tests for the Plex client. Network calls are mocked via httpx.MockTransport;
we focus on the payload-shape translation (Plex JSON → neutral MediaItem)
and the section-discovery + per-section pagination logic."""
import json

import httpx
import pytest

from app.server import MediaServerError
from app.server.base import MediaItem, MediaStream
from app.server.plex import PlexClient, _item_from_metadata, _stream_from_plex


# ── Payload-shape unit tests (no HTTP) ────────────────────────────────────────


def test_stream_from_plex_translates_audio():
    s = _stream_from_plex({
        "streamType": 2,
        "codec": "ac3",
        "languageCode": "fra",
        "language": "French",
        "default": True,
    })
    assert s.type == "audio"
    assert s.language == "fra"
    assert s.codec == "ac3"
    assert s.is_default is True


def test_stream_from_plex_translates_subtitle():
    s = _stream_from_plex({
        "streamType": 3,
        "codec": "subrip",
        "languageCode": "eng",
        "forced": False,
    })
    assert s.type == "subtitle"
    assert s.language == "eng"


def test_stream_from_plex_unknown_streamtype_becomes_other():
    """Defensive: any streamType we don't know about (e.g. attachments)
    becomes 'other' so MediaItem.has_subtitle_track ignores it."""
    s = _stream_from_plex({"streamType": 99, "languageCode": "und"})
    assert s.type == "other"


def test_stream_from_plex_falls_back_through_language_fields():
    """Plex tags streams with several language fields of varying reliability.
    We prefer languageCode (639-2), fall through to languageTag (BCP-47),
    then language (human label)."""
    just_tag = _stream_from_plex({"streamType": 3, "languageTag": "en"})
    assert just_tag.language == "en"
    just_label = _stream_from_plex({"streamType": 3, "language": "English"})
    assert just_label.language == "English"
    none_at_all = _stream_from_plex({"streamType": 3})
    assert none_at_all.language is None


def test_item_from_metadata_extracts_path_and_streams():
    item = _item_from_metadata({
        "ratingKey": "12345",
        "title": "Casablanca",
        "type": "movie",
        "Media": [{
            "Part": [{
                "file": "/data/movies/Casablanca/Casablanca.mkv",
                "Stream": [
                    {"streamType": 1, "codec": "h264"},
                    {"streamType": 2, "codec": "ac3", "languageCode": "eng"},
                    {"streamType": 3, "codec": "subrip", "languageCode": "fra"},
                ],
            }],
        }],
    })
    assert item.id == "12345"
    assert item.name == "Casablanca"
    assert item.type == "Movie"
    assert item.path == "/data/movies/Casablanca/Casablanca.mkv"
    assert len(item.streams) == 3
    assert item.has_subtitle_track("fr") is True
    assert item.has_subtitle_track("de") is False


def test_item_from_metadata_handles_missing_media():
    """Plex sometimes returns metadata without a Media array (e.g. for
    placeholder entries during a scan). We should produce a MediaItem with
    empty path + streams rather than raising."""
    item = _item_from_metadata({
        "ratingKey": "1",
        "title": "x",
        "type": "movie",
    })
    assert item.path == ""
    assert item.streams == []


# ── Constructor sanity ────────────────────────────────────────────────────────


def test_client_rejects_missing_creds():
    with pytest.raises(MediaServerError):
        PlexClient("", "")
    with pytest.raises(MediaServerError):
        PlexClient("http://plex:32400", "")


def test_client_accepts_verify_ssl_kwarg():
    """Plex's bundled cert is for *.plex.direct so LAN-IP HTTPS access
    fails verification by default. The verify_ssl kwarg lets the factory
    propagate the user's Settings choice into httpx."""
    c_default = PlexClient("https://plex.example.com:32400", "tok")
    c_insecure = PlexClient("https://192.168.1.10:32400", "tok", verify_ssl=False)
    assert c_default is not None
    assert c_insecure is not None


# ── HTTP behaviour (mocked transport) ─────────────────────────────────────────


def _client_with_mock(handler):
    """Build a PlexClient whose underlying httpx.Client uses a MockTransport
    so we can assert request shape without hitting a real server."""
    pc = PlexClient("http://plex:32400", "fake-token")
    pc._http = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"X-Plex-Token": "fake-token", "Accept": "application/json"},
        timeout=5.0,
        base_url="http://plex:32400",
    )
    return pc


def _resp(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_health_probes_identity_endpoint():
    """/identity is the canonical Plex probe — 200 means token works,
    401 means bad/missing token. We don't want to call /  which 200s
    even for unauthenticated requests."""
    seen_paths = []

    def handler(req):
        seen_paths.append(req.url.path)
        return _resp({"MediaContainer": {"machineIdentifier": "abc"}})
    pc = _client_with_mock(handler)
    assert pc.health() is True
    assert seen_paths == ["/identity"]


def test_health_returns_false_on_401():
    """Bad token → /identity 401s → health is False (the old impl hit /
    which 200s for unauth, so a misconfigured user got a green pill)."""
    def handler(req):
        return httpx.Response(401, text="invalid token")
    pc = _client_with_mock(handler)
    assert pc.health() is False


def test_health_returns_false_on_500():
    def handler(req):
        return httpx.Response(500, text="boom")
    pc = _client_with_mock(handler)
    assert pc.health() is False


def test_video_section_discovery_caches_keys():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/library/sections":
            return _resp({
                "MediaContainer": {
                    "Directory": [
                        {"key": "1", "type": "movie", "title": "Films"},
                        {"key": "2", "type": "show", "title": "TV Shows"},
                        {"key": "3", "type": "artist", "title": "Music"},
                        {"key": "4", "type": "photo", "title": "Photos"},
                    ]
                }
            })
        raise AssertionError(f"unexpected request: {req.url.path}")

    pc = _client_with_mock(handler)
    keys1 = pc._video_sections()
    keys2 = pc._video_sections()
    # Music + photos filtered out, only movie/show kept.
    assert keys1 == ["1", "2"]
    # Second call hits the cache — only one HTTP request total.
    assert keys2 == keys1
    assert calls.count("/library/sections") == 1


def test_get_item_404_raises_media_server_error():
    def handler(req):
        return httpx.Response(404, text="not found")
    pc = _client_with_mock(handler)
    with pytest.raises(MediaServerError, match="HTTP 404"):
        pc.get_item("nonexistent")


def test_refresh_item_uses_put():
    seen = {}

    def handler(req):
        seen["method"] = req.method
        seen["path"] = req.url.path
        return httpx.Response(200, json={})

    pc = _client_with_mock(handler)
    pc.refresh_item("12345")
    assert seen["method"] == "PUT"
    assert seen["path"] == "/library/metadata/12345/refresh"


def test_list_videos_aggregates_across_sections():
    """Plex has no unified video query; we fetch from each video section
    and concatenate. start_index/limit slice the aggregated list."""
    def handler(req):
        if req.url.path == "/library/sections":
            return _resp({
                "MediaContainer": {
                    "Directory": [
                        {"key": "1", "type": "movie"},
                        {"key": "2", "type": "show"},
                    ]
                }
            })
        if req.url.path == "/library/sections/1/all":
            return _resp({
                "MediaContainer": {
                    "totalSize": 2,
                    "Metadata": [
                        {"ratingKey": "11", "title": "Movie A", "type": "movie",
                         "Media": [{"Part": [{"file": "/m/A.mkv", "Stream": []}]}]},
                        {"ratingKey": "12", "title": "Movie B", "type": "movie",
                         "Media": [{"Part": [{"file": "/m/B.mkv", "Stream": []}]}]},
                    ],
                }
            })
        if req.url.path == "/library/sections/2/all":
            return _resp({
                "MediaContainer": {
                    "totalSize": 1,
                    "Metadata": [
                        {"ratingKey": "21", "title": "Episode X", "type": "episode",
                         "Media": [{"Part": [{"file": "/tv/X.mkv", "Stream": []}]}]},
                    ],
                }
            })
        raise AssertionError(req.url.path)

    pc = _client_with_mock(handler)
    page = pc.list_videos(start_index=0, limit=10)
    assert page.total == 3
    assert [it.id for it in page.items] == ["11", "12", "21"]
    # And the aggregate slice respects start_index/limit
    page2 = pc.list_videos(start_index=1, limit=1)
    assert [it.id for it in page2.items] == ["12"]
