"""Plex Media Server client.

Plex's REST API is structurally different from Emby/Jellyfin:

- Auth is `X-Plex-Token: <token>` (never `X-Emby-Token`).
- Default response is XML; we send `Accept: application/json` everywhere
  to get JSON-shaped responses.
- There's no single `/Items?Recursive=true` endpoint. Library items live
  per *section* (`/library/sections/{key}/all`), and we have to discover
  the video-bearing sections first via `/library/sections`. list_videos
  aggregates across all video sections.
- Each item identifier is `ratingKey` (Plex's stable item id), exposed
  to callers as `MediaItem.id`.
- Disk path lives at `Media[].Part[].file`, streams at
  `Media[].Part[].Stream[]` with `streamType` 1/2/3 (video/audio/subtitle).
- Pagination uses `X-Plex-Container-Start` / `X-Plex-Container-Size`
  query params (also accepted as headers).
- Refresh trigger: `PUT /library/metadata/{ratingKey}/refresh`.

For language matching, Plex tags streams with `languageCode` (ISO 639-2)
and sometimes `languageTag` (BCP 47); we feed whichever we get to
lang.normalize() so MediaItem.has_subtitle_track works the same way as
for Emby/Jellyfin.
"""
from typing import Any

import httpx

from app.server.base import (
    MediaItem,
    MediaPage,
    MediaServerClient,
    MediaServerError,
    MediaStream,
)


# Plex content type codes — see https://plexapi.dev/api/library
_TYPE_MOVIE = 1
_TYPE_EPISODE = 4
# Comma-separated for the `type` filter on /library/sections/{key}/all
_VIDEO_TYPES_PARAM = f"{_TYPE_MOVIE},{_TYPE_EPISODE}"

# Plex stream type codes
_STREAM_VIDEO = 1
_STREAM_AUDIO = 2
_STREAM_SUBTITLE = 3


class PlexClient(MediaServerClient):
    def __init__(self, base_url: str, token: str, *, verify_ssl: bool = True) -> None:
        if not base_url or not token:
            raise MediaServerError("Plex URL and token are required")
        self._base = base_url.rstrip("/")
        # NOTE: Plex's bundled certificate is issued for *.plex.direct only;
        # accessing the server by LAN IP over HTTPS fails verification by
        # default. Users on that setup should toggle "Verify SSL" off in
        # Settings (passes verify_ssl=False here).
        self._http = httpx.Client(
            headers={
                "X-Plex-Token": token,
                "Accept": "application/json",
            },
            timeout=30.0,
            verify=verify_ssl,
        )
        # Cached after first discovery — Plex section list is small and stable.
        self._video_section_keys: list[str] | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def health(self) -> bool:
        # `/identity` is the canonical "who are you?" probe and 401s on a
        # bad token, so a 200 means BOTH "server reachable" AND "our auth
        # works". `/` would 200 even with no/bad token, which made the
        # health pill green for misconfigured users.
        try:
            r = self._http.get(f"{self._base}/identity")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def get_item(self, item_id: str) -> MediaItem:
        r = self._http.get(f"{self._base}/library/metadata/{item_id}")
        if r.status_code != 200:
            raise MediaServerError(
                f"GET /library/metadata/{item_id} → HTTP {r.status_code}: {r.text[:200]}"
            )
        items = (r.json().get("MediaContainer") or {}).get("Metadata") or []
        if not items:
            raise MediaServerError(f"Plex item {item_id!r} not found")
        return _item_from_metadata(items[0])

    def list_videos(
        self,
        *,
        start_index: int = 0,
        limit: int = 200,
        search_term: str | None = None,
    ) -> MediaPage:
        """Aggregate one page across all video sections. Plex has no unified
        recursive query, so we sum totals across sections and slice the
        concatenated items at the requested window. For huge libraries the
        non-search path can be slow on later pages — search-by-title is the
        fast path, served directly by Plex's per-section title filter."""
        all_items: list[MediaItem] = []
        total = 0
        for section_key in self._video_sections():
            page = self._section_page(
                section_key, start_index=0, limit=10_000, search_term=search_term,
            )
            all_items.extend(page.items)
            total += page.total
        sliced = all_items[start_index : start_index + limit]
        return MediaPage(items=sliced, total=total)

    def refresh_item(self, item_id: str) -> None:
        """Plex uses PUT for the per-item refresh trigger."""
        r = self._http.put(f"{self._base}/library/metadata/{item_id}/refresh")
        if r.status_code not in (200, 201, 204):
            raise MediaServerError(
                f"PUT /library/metadata/{item_id}/refresh → HTTP {r.status_code}: {r.text[:200]}"
            )

    # ── Internals ─────────────────────────────────────────────────────────────

    def _video_sections(self) -> list[str]:
        """Discover (and cache) the keys of library sections that contain
        videos — Plex section types `movie` and `show`."""
        if self._video_section_keys is not None:
            return self._video_section_keys
        r = self._http.get(f"{self._base}/library/sections")
        if r.status_code != 200:
            raise MediaServerError(
                f"GET /library/sections → HTTP {r.status_code}: {r.text[:200]}"
            )
        directories = (r.json().get("MediaContainer") or {}).get("Directory") or []
        keys = [
            str(d["key"]) for d in directories
            if d.get("type") in ("movie", "show", "video") and d.get("key")
        ]
        self._video_section_keys = keys
        return keys

    def _section_page(
        self,
        section_key: str,
        *,
        start_index: int,
        limit: int,
        search_term: str | None = None,
    ) -> MediaPage:
        """Fetch one page from a single section, filtered to movies + episodes."""
        params: dict[str, Any] = {
            "type": _VIDEO_TYPES_PARAM,
            "X-Plex-Container-Start": start_index,
            "X-Plex-Container-Size": limit,
        }
        if search_term:
            # Plex's per-section filter for substring title match.
            params["title"] = search_term

        r = self._http.get(
            f"{self._base}/library/sections/{section_key}/all",
            params=params,
        )
        if r.status_code != 200:
            raise MediaServerError(
                f"GET /library/sections/{section_key}/all → HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )
        body = (r.json().get("MediaContainer") or {})
        metadata = body.get("Metadata") or []
        items = [_item_from_metadata(m) for m in metadata]
        # Plex's totalSize is the unfiltered section count; size is the
        # number of items in this response. list_videos sums totals across
        # sections so the slight imprecision is fine for the UI.
        total = int(body.get("totalSize") or body.get("size") or len(items))
        return MediaPage(items=items, total=total)


# ── Payload → neutral dataclass conversion ────────────────────────────────────


def _item_from_metadata(m: dict) -> MediaItem:
    """Convert one Plex Metadata blob into a neutral MediaItem.
    Plex puts the disk path inside Media[0].Part[0].file and per-stream info
    inside Media[0].Part[0].Stream[]."""
    media = m.get("Media") or []
    part = (media[0].get("Part") or [{}])[0] if media else {}
    streams_raw = part.get("Stream") or []

    # Plex's `type` field on a Metadata entry is the content kind (movie /
    # episode / show). We surface it title-cased to match Emby's convention.
    raw_type = (m.get("type") or "").lower()
    type_label = {
        "movie": "Movie",
        "episode": "Episode",
        "show": "Series",
    }.get(raw_type, raw_type.capitalize() if raw_type else "")

    return MediaItem(
        id=str(m.get("ratingKey") or ""),
        name=m.get("title") or "",
        path=part.get("file") or "",
        type=type_label,
        streams=[_stream_from_plex(s) for s in streams_raw],
    )


def _stream_from_plex(s: dict) -> MediaStream:
    stream_type_code = s.get("streamType")
    type_label = {
        _STREAM_VIDEO: "video",
        _STREAM_AUDIO: "audio",
        _STREAM_SUBTITLE: "subtitle",
    }.get(stream_type_code, "other")

    # Plex tags streams with multiple language fields — try the most reliable
    # ones first (languageCode is ISO 639-2; languageTag is BCP-47 like "en";
    # language is a human label). lang.normalize() handles all three.
    lang = (
        s.get("languageCode")
        or s.get("languageTag")
        or s.get("language")
        or None
    )

    return MediaStream(
        type=type_label,
        language=lang,
        codec=s.get("codec"),
        title=s.get("title") or s.get("displayTitle"),
        is_default=bool(s.get("default")),
        is_forced=bool(s.get("forced")),
    )
