"""Client for Emby and Jellyfin servers.

Jellyfin is a fork of Emby and their REST APIs are functionally identical for
everything we need (/Items, /Items/{id}, /Items/{id}/Refresh, /System/Info/Public).
Both accept the same X-Emby-Token auth header (Jellyfin keeps it for legacy
compat alongside its newer Authorization: MediaBrowser scheme), so a single
client implementation serves both — the user just picks their server type in
Settings for the badge label and a couple of UI hints.
"""
import httpx

from app.pipeline.lang import normalize as _normalize_lang
from app.server.base import (
    MediaItem,
    MediaLibrary,
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
        # 60 s timeout: large Emby/Jellyfin libraries (100 k+ items)
        # can take >30 s to serve /Items?Recursive=true on slow storage.
        # 30 s was too tight on real deployments — we'd see spurious
        # ReadTimeout errors on Library page renders.
        self._http = httpx.Client(
            headers={"X-Emby-Token": api_key, "Accept": "application/json"},
            timeout=60.0,
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

    def list_libraries(self) -> list[MediaLibrary]:
        # `/Library/MediaFolders` returns the top-level user-facing libraries.
        # Each entry has Id, Name, and CollectionType ("movies" / "tvshows" /
        # "music" / "homevideos" / etc.). We filter to video-bearing kinds
        # (movies / tvshows / mixed / homevideos) so users don't see Music in
        # the library dropdown of a *subtitle* tool. Empty CollectionType
        # ("mixed" content) is allowed through — Emby uses it for folders the
        # user explicitly didn't classify, which still typically hold videos.
        r = self._http.get(f"{self._base}/Library/MediaFolders")
        if r.status_code != 200:
            raise MediaServerError(
                f"GET /Library/MediaFolders → HTTP {r.status_code}: {r.text[:200]}"
            )
        out: list[MediaLibrary] = []
        for it in (r.json().get("Items") or []):
            ct = (it.get("CollectionType") or "").lower()
            if ct and ct not in ("movies", "tvshows", "homevideos", "mixed"):
                continue
            out.append(MediaLibrary(
                id=str(it.get("Id") or ""),
                name=it.get("Name") or "",
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
        params: dict = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "Fields": "Path,MediaStreams",
            "StartIndex": start_index,
            "Limit": limit,
        }
        if search_term:
            params["SearchTerm"] = search_term
        if library_id:
            # ParentId scopes the recursive query to one top-level library.
            # Combined with Recursive=true this descends through seasons/folders
            # so episodes still surface as flat list entries.
            params["ParentId"] = library_id
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
    # Emby/Jellyfin emit 3-letter ISO 639-2 codes (eng, fre, ita) in
    # the Language field. The rest of the app expects ISO 639-1
    # (en, fr, it) and the Plex adapter already normalizes — so do
    # the same here to keep MediaStream.language uniformly 2-letter
    # regardless of which server backs the client. Without this, the
    # Library page rendered 3-letter pills on Emby/Jellyfin items
    # while every other lang surface (target_lang chip row, embedded-
    # subs decision, etc.) used 2-letter codes. Codes outside the
    # known mapping degrade gracefully to None (then to "—" in the UI).
    return MediaStream(
        type=raw_type if raw_type in ("audio", "subtitle", "video") else "other",
        language=_normalize_lang(s.get("Language")),
        codec=s.get("Codec"),
        title=s.get("Title") or s.get("DisplayTitle"),
        is_default=bool(s.get("IsDefault")),
        is_forced=bool(s.get("IsForced")),
    )
