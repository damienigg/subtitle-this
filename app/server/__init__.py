"""Media-server client factory. Picks the implementation based on
settings.media_server_type. Used by app/api/manage.py and the Library
template."""
from app.config import settings
from app.server.base import (
    MediaItem,
    MediaLibrary,
    MediaPage,
    MediaServerClient,
    MediaServerError,
    MediaStream,
)


_SUPPORTED_TYPES = ("emby", "jellyfin", "plex")


def media_server_client() -> MediaServerClient:
    """Build a fresh client from the currently-saved Settings. Raises
    MediaServerError if the server isn't configured (URL or key missing) or
    the type is unknown."""
    server_type = (settings.media_server_type or "").lower()
    url = settings.media_server_url
    key = settings.media_server_api_key
    verify_ssl = bool(settings.media_server_verify_ssl)

    if not url or not key:
        raise MediaServerError(
            "Media server URL and API key are not configured (set them in Settings)"
        )

    if server_type in ("emby", "jellyfin"):
        from app.server.emby_jellyfin import EmbyJellyfinClient
        return EmbyJellyfinClient(url, key, verify_ssl=verify_ssl)
    if server_type == "plex":
        from app.server.plex import PlexClient
        return PlexClient(url, key, verify_ssl=verify_ssl)

    raise MediaServerError(
        f"Unknown media_server_type {server_type!r} (expected one of {_SUPPORTED_TYPES})"
    )


__all__ = [
    "MediaItem", "MediaLibrary", "MediaPage", "MediaStream",
    "MediaServerClient", "MediaServerError",
    "media_server_client",
]
