"""Server-agnostic media-server abstraction.

The pipeline (process, batch, library browser) talks to whatever media
server the user has via this protocol. Concrete implementations live in
sibling modules:

- emby_jellyfin.py — covers both Emby and Jellyfin (their REST APIs are
  near-identical; Jellyfin is a fork of Emby and keeps the same /Items,
  /System/Info/Public, etc. endpoints with the same X-Emby-Token auth).
- plex.py — Plex Media Server, with its own X-Plex-Token auth and entirely
  different /library/sections + /library/metadata/{ratingKey} endpoints.

All implementations return the neutral dataclasses defined here, never their
raw HTTP responses, so callers stay backend-agnostic.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.pipeline.lang import normalize


class MediaServerError(Exception):
    """Raised by any media-server client when an HTTP call or auth fails.
    Callers translate to a 502 (or 412 if the server isn't configured)."""


@dataclass
class MediaStream:
    """Neutral representation of an audio / subtitle / video stream. Each
    backend translates its raw stream descriptor into this shape so
    has_subtitle_track() and friends work the same regardless of server."""
    type: str                       # "audio" | "subtitle" | "video"
    language: str | None            # ISO 639-1 (normalized) or None
    codec: str | None = None
    title: str | None = None
    is_default: bool = False
    is_forced: bool = False


@dataclass
class MediaItem:
    id: str
    name: str
    path: str                       # disk path inside the container's mount
    type: str                       # "Movie" | "Episode" | "Video"
    streams: list[MediaStream] = field(default_factory=list)

    def has_subtitle_track(self, target_lang: str) -> bool:
        """True iff the item already has a subtitle stream in target_lang
        (matched after ISO 639-1/2 normalization on both sides)."""
        target = normalize(target_lang) or target_lang.lower()
        for s in self.streams:
            if s.type != "subtitle":
                continue
            stream_lang = normalize(s.language)
            if stream_lang == target:
                return True
        return False


@dataclass
class MediaPage:
    """One page of items + the total count the server reported for the query."""
    items: list[MediaItem]
    total: int


@dataclass
class MediaLibrary:
    """A top-level library/collection on the media server (e.g. "Movies",
    "TV Shows"). Used for the Library page filter — users with both a films
    library and a series library on the same Emby/Jellyfin/Plex server can
    scope the browser to just one."""
    id: str
    name: str
    type: str   # "movies" | "tvshows" | "mixed" | "" (server-reported, lowercased)


class MediaServerClient(ABC):
    """Protocol that every media-server backend implements. Stays minimal —
    just the surface the subtitling pipeline actually needs."""

    @abstractmethod
    def health(self) -> bool:
        """True iff the server responds to a cheap health probe with 200."""

    @abstractmethod
    def get_item(self, item_id: str) -> MediaItem:
        """Look up a single item by its server-native id. Raises
        MediaServerError on HTTP / auth failures."""

    @abstractmethod
    def list_libraries(self) -> list[MediaLibrary]:
        """The user-facing top-level libraries on this server. Used to
        populate the Library page's library filter dropdown."""

    @abstractmethod
    def list_videos(
        self,
        *,
        start_index: int = 0,
        limit: int = 200,
        search_term: str | None = None,
        library_id: str | None = None,
    ) -> MediaPage:
        """One page of video items + the server's total-count report.
        When library_id is set, the listing is scoped to just that library."""

    @abstractmethod
    def refresh_item(self, item_id: str) -> None:
        """Trigger a metadata refresh for the item — typically called after
        Babel writes a .vtt next to the media so the server picks it up.
        Raises MediaServerError on HTTP failure."""
