"""Plex Media Server client. Implementation lives in the next commit; this
module's factory entry is wired up but raises a clear NotImplementedError
until then."""
from typing import Iterator

from app.server.base import MediaItem, MediaPage, MediaServerClient, MediaServerError


class PlexClient(MediaServerClient):
    def __init__(self, base_url: str, token: str) -> None:
        if not base_url or not token:
            raise MediaServerError("Plex URL and token are required")
        raise NotImplementedError(
            "Plex client lands in the next commit. Pick 'emby' or 'jellyfin' "
            "for now in Settings → Media server → Server type."
        )

    def health(self) -> bool:
        raise NotImplementedError

    def get_item(self, item_id: str) -> MediaItem:
        raise NotImplementedError

    def list_videos(
        self, *, start_index: int = 0, limit: int = 200,
        search_term: str | None = None,
    ) -> MediaPage:
        raise NotImplementedError

    def iter_videos(
        self, *, page_size: int = 200, max_items: int | None = None,
    ) -> Iterator[MediaItem]:
        raise NotImplementedError

    def refresh_item(self, item_id: str) -> None:
        raise NotImplementedError
