"""Client for Emby and Jellyfin servers.

Jellyfin is a fork of Emby and their REST APIs are functionally identical for
everything we need (/Items, /Items/{id}, /Items/{id}/Refresh, /System/Info/Public).
Both accept the same X-Emby-Token auth header (Jellyfin keeps it for legacy
compat alongside its newer Authorization: MediaBrowser scheme), so a single
client implementation serves both — the user just picks their server type in
Settings for the badge label and a couple of UI hints.
"""
import httpx

from app.server.base import (
    MediaItem,
    MediaPage,
    MediaServerClient,
    MediaServerError,
    MediaStream,
)


class EmbyJellyfinClient(MediaServerClient):
    def __init__(self, base_url: str, api_key: str, *, verify_ssl: bool = True) -> None:
        if not base_url or not api_key:
            raise MediaServerError("Server URL and API key are required")
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(
            headers={"X-Emby-Token": api_key, "Accept": "application/json"},
            timeout=30.0,
            verify=verify_ssl,
        )

    def health(self) -> bool:
        try:
            r = self._http.get(f"{self._base}/System/Info/Public")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def get_item(self, item_id: str) -> MediaItem:
        # Emby's /Items/{id} path-style endpoint isn't reliably routed across
        # versions and reverse-proxy setups — some return a static-file-style
        # 404 ("The file '/Items/X' could not be found"). The Ids= filter on
        # the collection endpoint is the universally-supported way to fetch
        # one item by id; works on both Emby and Jellyfin. Returns an Items
        # array of length 1 on success, length 0 when the id doesn't exist.
        r = self._http.get(
            f"{self._base}/Items",
            params={"Ids": item_id, "Fields": "Path,MediaStreams"},
        )
        if r.status_code != 200:
            raise MediaServerError(
                f"GET /Items?Ids={item_id} → HTTP {r.status_code}: {r.text[:200]}"
            )
        items = (r.json().get("Items") or [])
        if not items:
            raise MediaServerError(f"item {item_id!r} not found in library")
        return _item_from_payload(items[0])

    def list_videos(
        self,
        *,
        start_index: int = 0,
        limit: int = 200,
        search_term: str | None = None,
    ) -> MediaPage:
        params: dict = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "Fields": "Path,MediaStreams",
            "StartIndex": start_index,
            "Limit": limit,
        }
        if search_term:
            params["SearchTerm"] = search_term
        r = self._http.get(f"{self._base}/Items", params=params)
        if r.status_code != 200:
            raise MediaServerError(
                f"GET /Items → HTTP {r.status_code}: {r.text[:200]}"
            )
        body = r.json()
        items = [_item_from_payload(it) for it in body.get("Items") or []]
        return MediaPage(items=items, total=int(body.get("TotalRecordCount", len(items))))

    def refresh_item(self, item_id: str) -> None:
        r = self._http.post(
            f"{self._base}/Items/{item_id}/Refresh",
            params={
                "MetadataRefreshMode": "Default",
                "ImageRefreshMode": "Default",
            },
        )
        if r.status_code not in (200, 204):
            raise MediaServerError(
                f"POST /Items/{item_id}/Refresh → HTTP {r.status_code}: {r.text[:200]}"
            )


def _item_from_payload(d: dict) -> MediaItem:
    # Path and MediaStreams come back at the top level when explicitly
    # requested via Fields= (the typical /Items?Recursive=true list call).
    # When fetching one item via /Items?Ids=X, some Emby versions populate
    # them only in the nested MediaSources[0] structure. Fall back to that
    # so both shapes resolve to the same MediaItem.
    path = d.get("Path") or ""
    streams_raw = d.get("MediaStreams")
    if not path or streams_raw is None:
        sources = d.get("MediaSources") or []
        if sources:
            src = sources[0] or {}
            if not path:
                path = src.get("Path") or ""
            if streams_raw is None:
                streams_raw = src.get("MediaStreams")
    return MediaItem(
        id=str(d.get("Id") or ""),
        name=d.get("Name") or "",
        path=path,
        type=d.get("Type") or "",
        streams=[_stream_from_payload(s) for s in (streams_raw or [])],
    )


def _stream_from_payload(s: dict) -> MediaStream:
    raw_type = (s.get("Type") or "").lower()
    return MediaStream(
        type=raw_type if raw_type in ("audio", "subtitle", "video") else "other",
        language=s.get("Language") or None,
        codec=s.get("Codec"),
        title=s.get("Title") or s.get("DisplayTitle"),
        is_default=bool(s.get("IsDefault")),
        is_forced=bool(s.get("IsForced")),
    )
