"""Plex Media Server client.

Plex's REST API is structurally different from Emby/Jellyfin. Verified
against the official Plex Media Server API docs at developer.plex.tv/pms
and python-plexapi (the de-facto reference Python wrapper) on the
endpoints we use:

- Auth: `X-Plex-Token: <token>` header. Most endpoints 401 without it
  EXCEPT the public discovery ones (`/`, `/identity`) which return
  200 to anonymous callers — that's why `health()` below probes
  `/library/sections` instead, so a green pill actually means
  "URL + token both work".
- Default response is XML; we send `Accept: application/json` to get
  JSON. Both formats wrap data in a `MediaContainer` root object.
- No unified recursive query — library items live per *section*
  (`/library/sections/{key}/all`). We discover sections first via
  `/library/sections`. list_videos aggregates across all video
  sections, querying each with the right type filter for that
  section's content kind (movie sections → type=1 movies, show
  sections → type=4 episodes).
- Each item id is `ratingKey` (Plex's stable id), exposed to callers
  as `MediaItem.id`.
- Disk path lives at `Media[0].Part[0].file`; streams at
  `Media[0].Part[0].Stream[]` with `streamType` 1/2/3
  (video/audio/subtitle).
- Pagination via `X-Plex-Container-Start` / `X-Plex-Container-Size`,
  accepted as either headers or query params per the docs (we use
  query params for clean httpx integration).
- Refresh: `PUT /library/metadata/{ratingKey}/refresh` — verified in
  python-plexapi's PlexPartialObject.refresh().

For language matching, Plex tags streams with `languageCode` (ISO 639-2)
and sometimes `languageTag` (BCP 47); we feed whichever is present to
lang.normalize() so MediaItem.has_subtitle_track works identically for
Emby/Jellyfin and Plex.
"""
import threading
from typing import Any

import httpx

from app.server.base import (
    MediaItem,
    MediaLibrary,
    MediaPage,
    MediaServerClient,
    MediaServerError,
    MediaStream,
)


# Module-level cache for the list of video-bearing sections, keyed by
# (base_url, token). PlexClient is constructed fresh per request in
# manage.py:media_server_client(), so a per-instance cache (which the
# previous implementation had) was always cold. The section list is
# small (a handful of entries) and rarely changes — caching at module
# scope means subsequent requests skip the /library/sections roundtrip.
# Lock guards the dict against concurrent first-time-write races.
_VIDEO_SECTIONS_CACHE: dict[tuple[str, str], list[tuple[str, int]]] = {}
_VIDEO_SECTIONS_LOCK = threading.Lock()


def _clear_video_sections_cache() -> None:
    """Test hook — drop the module cache so tests get fresh /library/sections
    behavior. Production code never calls this."""
    with _VIDEO_SECTIONS_LOCK:
        _VIDEO_SECTIONS_CACHE.clear()


# Plex content type codes (from the official API enum).
_TYPE_MOVIE = 1
_TYPE_SHOW = 2
_TYPE_SEASON = 3
_TYPE_EPISODE = 4

# For each Plex *section* type, the *content* type we want when listing
# items for subtitling. Movie sections → individual movies; show sections
# → individual episodes (NOT shows — shows are folders, we want the
# leaf media items with actual video files).
_SECTION_TO_VIDEO_TYPE: dict[str, int] = {
    "movie": _TYPE_MOVIE,
    "show": _TYPE_EPISODE,
}

# Plex stream type codes (also from the official API enum).
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
        # Token serves as the cache key alongside base_url so different
        # users hitting different Plex servers don't share section lists.
        self._token = token

    # ── Public API ────────────────────────────────────────────────────────────

    def health(self) -> bool:
        # Probe an auth-required endpoint so a green pill confirms BOTH
        # "server reachable" AND "X-Plex-Token works". The previously-used
        # `/identity` and `/` endpoints both return 200 to anonymous calls
        # per the Plex docs, so they couldn't catch a wrong token.
        # `/library/sections` is the natural choice — it's auth-required
        # AND it's exactly what we'd hit on the next real call anyway.
        try:
            r = self._http.get(f"{self._base}/library/sections")
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

    def list_libraries(self) -> list[MediaLibrary]:
        """Surface the video-bearing sections (movie + show) for the Library
        page filter. Music ('artist') and photo sections are skipped — a
        subtitle tool has no business listing them."""
        r = self._http.get(f"{self._base}/library/sections")
        if r.status_code != 200:
            raise MediaServerError(
                f"GET /library/sections → HTTP {r.status_code}: {r.text[:200]}"
            )
        directories = (r.json().get("MediaContainer") or {}).get("Directory") or []
        out: list[MediaLibrary] = []
        for d in directories:
            section_type = (d.get("type") or "").lower()
            if section_type not in _SECTION_TO_VIDEO_TYPE:
                continue
            # Map Plex's section types to the same labels Emby uses so the
            # frontend doesn't need backend-specific branches.
            ct = "movies" if section_type == "movie" else "tvshows"
            out.append(MediaLibrary(
                id=str(d.get("key") or ""),
                name=d.get("title") or "",
                type=ct,
            ))
        return out

    def list_videos(
        self,
        *,
        start_index: int = 0,
        limit: int = 200,
        search_term: str | None = None,
        library_id: str | None = None,
    ) -> MediaPage:
        """Aggregate one page across all video sections. Plex has no unified
        recursive query, so we issue one call per (section, content-type)
        pair: movie sections fetch type=1 (movies), show sections fetch
        type=4 (episodes — the leaf items with actual video files, NOT
        the show folders). Search-by-title is the fast path; the
        unfiltered listing pulls everything per section so later pages
        don't require expensive offsets.

        When library_id is set, scope to just that section."""
        if library_id:
            # User picked a specific section. Find its content type by looking
            # it up in the cached video-sections list; if it's not video-
            # bearing (music/photo) we have nothing to return.
            type_code = next(
                (t for k, t in self._video_sections() if k == library_id),
                None,
            )
            if type_code is None:
                return MediaPage(items=[], total=0)
            # Pass start_index / limit straight through to Plex's
            # X-Plex-Container-Start / X-Plex-Container-Size headers so
            # the SERVER does the pagination, not us. Previously we
            # fetched 10 000 items per page render and sliced in Python
            # — fine for small libraries, catastrophic for a 50 k-episode
            # show section.
            return self._section_page(
                library_id,
                type_code=type_code,
                start_index=start_index,
                limit=limit,
                search_term=search_term,
            )

        # Multi-section aggregate. Plex has no recursive query so we still
        # need one call per section, but we only fetch as many items as
        # this page actually needs — `start_index + limit` items from each
        # section is the upper bound (worst case: the requested page is
        # entirely from one section). For typical homelab libraries with
        # 1-2 video sections this is fine; pathological cases with many
        # large sections still pay more than the single-section path.
        all_items: list[MediaItem] = []
        total = 0
        per_section_cap = start_index + limit
        for section_key, type_code in self._video_sections():
            page = self._section_page(
                section_key,
                type_code=type_code,
                start_index=0,
                limit=per_section_cap,
                search_term=search_term,
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

    def _video_sections(self) -> list[tuple[str, int]]:
        """Discover (and cache) the video-bearing library sections, paired
        with the content-type filter to use against each.

        Returns a list of (section_key, video_type_code) tuples:
        - movie sections → type=1 (individual movies)
        - show sections → type=4 (individual episodes, not shows/seasons)

        Music ('artist') and photo ('photo') sections are skipped — we
        only subtitle videos.

        Cache lives at module scope keyed on (base_url, token) so
        successive PlexClient instances (built fresh per request in
        manage.py) reuse it. Operators who change their library layout
        need to restart the container — section adds/removes are rare
        enough that an explicit invalidation path isn't worth the
        complexity here.
        """
        cache_key = (self._base, self._token)
        cached = _VIDEO_SECTIONS_CACHE.get(cache_key)
        if cached is not None:
            return cached
        r = self._http.get(f"{self._base}/library/sections")
        if r.status_code != 200:
            raise MediaServerError(
                f"GET /library/sections → HTTP {r.status_code}: {r.text[:200]}"
            )
        directories = (r.json().get("MediaContainer") or {}).get("Directory") or []
        pairs: list[tuple[str, int]] = []
        for d in directories:
            section_type = d.get("type")
            video_type = _SECTION_TO_VIDEO_TYPE.get(section_type)
            key = d.get("key")
            if video_type is not None and key is not None:
                pairs.append((str(key), video_type))
        with _VIDEO_SECTIONS_LOCK:
            _VIDEO_SECTIONS_CACHE[cache_key] = pairs
        return pairs

    def _section_page(
        self,
        section_key: str,
        *,
        type_code: int,
        start_index: int,
        limit: int,
        search_term: str | None = None,
    ) -> MediaPage:
        """Fetch one page of a specific content type from a single section.
        type_code is one of _TYPE_MOVIE / _TYPE_EPISODE — Plex's per-call
        filter only accepts a single integer (no comma-separated lists).
        """
        params: dict[str, Any] = {
            "type": type_code,
            "X-Plex-Container-Start": start_index,
            "X-Plex-Container-Size": limit,
        }
        if search_term:
            # Plex's per-section title filter for substring match.
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
        # totalSize = the section's full count for this query; size = the
        # number returned in this page. list_videos aggregates totals
        # across (section, type) pairs.
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
