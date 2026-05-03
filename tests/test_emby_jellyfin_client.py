"""Tests for the Emby/Jellyfin shared client + the neutral MediaItem
abstraction. The two server types share an implementation because their
REST APIs are functionally identical."""
import httpx
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


def test_client_accepts_verify_ssl_kwarg():
    """The verify_ssl kwarg lets users with self-signed certs (or Plex on
    LAN IP) opt out of TLS verification. Construction succeeds in both
    modes; the actual verification behaviour is delegated to httpx."""
    c_default = EmbyJellyfinClient("https://emby.example.com", "k")
    c_insecure = EmbyJellyfinClient("https://192.168.1.10:8096", "k", verify_ssl=False)
    assert c_default is not None
    assert c_insecure is not None


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


# ── HTTP behaviour (mocked transport) ─────────────────────────────────────────


def _client_with_mock(handler):
    """Build an EmbyJellyfinClient whose underlying httpx.Client uses a
    MockTransport so we assert request shape without hitting a real server.
    Mirrors the helper in test_plex_client.py for symmetric coverage."""
    c = EmbyJellyfinClient("http://emby:8096", "fake-key")
    c._http = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"X-Emby-Token": "fake-key", "Accept": "application/json"},
        timeout=5.0,
        base_url="http://emby:8096",
    )
    return c


def test_health_returns_true_on_200():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={"ServerName": "emby", "Version": "4.x"})
    c = _client_with_mock(handler)
    assert c.health() is True
    assert seen == ["/System/Info/Public"]


def test_health_returns_false_on_500():
    def handler(req):
        return httpx.Response(500, text="boom")
    c = _client_with_mock(handler)
    assert c.health() is False


def test_get_item_404_raises_media_server_error():
    def handler(req):
        return httpx.Response(404, text="not found")
    c = _client_with_mock(handler)
    with pytest.raises(MediaServerError, match="HTTP 404"):
        c.get_item("nonexistent")


def test_get_item_uses_collection_query_not_path_style():
    """Critical: get_item MUST use /Items?Ids=... not /Items/{id} because
    the path-style endpoint isn't routed reliably across Emby versions
    (some return a static-file-style 404 on it). This test pins the
    request shape so a refactor doesn't accidentally regress it."""
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={
            "Items": [{
                "Id": "12345", "Name": "Casablanca", "Type": "Movie",
                "Path": "/data/Casablanca.mkv",
                "MediaStreams": [{"Type": "Subtitle", "Language": "fra"}],
            }],
        })
    c = _client_with_mock(handler)
    item = c.get_item("12345")
    assert seen["path"] == "/Items"
    assert seen["params"]["Ids"] == "12345"
    assert "Path" in seen["params"]["Fields"]
    assert item.id == "12345"
    assert item.path == "/data/Casablanca.mkv"


def test_get_item_empty_items_array_raises_not_found():
    """Distinct from a 200 with an empty list and a 404. /Items?Ids=X with
    a missing id returns 200 + Items=[] — must surface as MediaServerError
    so the API endpoint can map it to a clear UI error."""
    def handler(req):
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})
    c = _client_with_mock(handler)
    with pytest.raises(MediaServerError, match="not found in library"):
        c.get_item("nonexistent-id")


def test_get_item_falls_back_to_media_sources_for_path_and_streams():
    """When fetched via /Items?Ids=, some Emby versions populate Path and
    MediaStreams ONLY inside MediaSources[0] (the nested detailed source
    descriptor) rather than at the top level. Parser must handle both."""
    def handler(req):
        return httpx.Response(200, json={
            "Items": [{
                "Id": "1", "Name": "Movie", "Type": "Movie",
                # No top-level Path or MediaStreams — only in MediaSources
                "MediaSources": [{
                    "Path": "/Movies/buried-in-mediasources.mkv",
                    "MediaStreams": [
                        {"Type": "Audio", "Language": "eng"},
                        {"Type": "Subtitle", "Language": "fra"},
                    ],
                }],
            }],
        })
    c = _client_with_mock(handler)
    item = c.get_item("1")
    assert item.path == "/Movies/buried-in-mediasources.mkv"
    assert len(item.streams) == 2
    assert item.has_subtitle_track("fr") is True


def test_get_item_top_level_path_takes_precedence_over_media_sources():
    """When BOTH top-level and MediaSources[0] have a Path, prefer the
    top-level one — it's the authoritative answer from Fields=Path."""
    def handler(req):
        return httpx.Response(200, json={
            "Items": [{
                "Id": "1", "Name": "Movie", "Type": "Movie",
                "Path": "/canonical-top-level.mkv",
                "MediaStreams": [{"Type": "Subtitle", "Language": "fra"}],
                "MediaSources": [{"Path": "/different-fallback-path.mkv"}],
            }],
        })
    c = _client_with_mock(handler)
    item = c.get_item("1")
    assert item.path == "/canonical-top-level.mkv"


def test_list_videos_passes_pagination_and_search():
    """list_videos must thread start_index, limit, and search_term into the
    /Items query params, with the right Recursive + IncludeItemTypes filter."""
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={
            "Items": [
                {"Id": "1", "Name": "A", "Type": "Movie", "Path": "/a.mkv", "MediaStreams": []},
            ],
            "TotalRecordCount": 87,
        })
    c = _client_with_mock(handler)
    page = c.list_videos(start_index=20, limit=10, search_term="case")
    assert page.total == 87
    assert len(page.items) == 1
    assert seen["path"] == "/Items"
    assert seen["params"]["StartIndex"] == "20"
    assert seen["params"]["Limit"] == "10"
    assert seen["params"]["SearchTerm"] == "case"
    assert seen["params"]["Recursive"] == "true"
    assert seen["params"]["IncludeItemTypes"] == "Movie,Episode"


def test_refresh_item_uses_post():
    """Emby's metadata refresh trigger is POST /Items/{id}/Refresh —
    distinct from Plex's PUT shape. Asymmetry between the two clients
    is intentional and worth pinning down with a test."""
    seen = {}

    def handler(req):
        seen["method"] = req.method
        seen["path"] = req.url.path
        return httpx.Response(204)
    c = _client_with_mock(handler)
    c.refresh_item("12345")
    assert seen["method"] == "POST"
    assert seen["path"] == "/Items/12345/Refresh"


def test_refresh_item_500_raises():
    def handler(req):
        return httpx.Response(500, text="kaboom")
    c = _client_with_mock(handler)
    with pytest.raises(MediaServerError, match="HTTP 500"):
        c.refresh_item("12345")
