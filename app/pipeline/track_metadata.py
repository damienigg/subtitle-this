"""Write a detected language tag back into the source media's audio track.

When Whisper detects a language for an audio track that ffprobe reported as
untagged, we persist that detection back into the file's container metadata
so Emby's next probe (and any other downstream tool) sees the correct
language.

**Matroska only.** We deliberately restrict the write-back to MKV / MKA /
WebM via `mkvpropedit`. That tool edits ONLY the EBML header (track-name,
language, disposition flags) and never touches a single byte of the audio
or video data sections. No re-encode, no remux, no temporal manipulation
— it's the safest possible tagging operation.

We do NOT attempt to tag MP4 / MOV / AVI / etc. via `ffmpeg -c copy`. While
that would technically preserve the audio bitstream byte-for-byte, it
rewrites the entire file (so the original file gets replaced atomically
after a full I/O pass) and there are documented edge cases — timestamp
re-derivation on weird MP4s, lost custom chapters/attachments, subtle
container-chunk reorganization — that a media library should not have to
worry about. For non-Matroska containers we report the limitation and
leave the source file untouched. Detection still runs upstream so
transcription itself is correct; only the persistence-back-to-Emby step
is skipped.

ISO 639-2 (3-letter) is the canonical language tag in Matroska metadata.
We map from Whisper's ISO 639-1 output via app.pipeline.lang.to_iso6392.

This module is best-effort by design — callers should treat MetadataWriteError
as non-fatal because the .vtt is the user-visible artifact and the tag write
is a polish on the source file.
"""
import json
import subprocess
from pathlib import Path

from app.pipeline.lang import to_iso6392


_MATROSKA_EXTS = {".mkv", ".mka", ".webm"}


class MetadataWriteError(Exception):
    pass


def write_audio_language(
    media_path: Path, track_index: int, lang_iso6391: str
) -> None:
    """Tag the audio stream at `track_index` (the absolute ffprobe index) with
    the language `lang_iso6391` (Whisper's short code, e.g. 'fr'). Mutates the
    source file in place via mkvpropedit. Raises MetadataWriteError on any
    failure, including non-Matroska containers (we don't risk a full ffmpeg
    remux just to tag a track).
    """
    iso6392 = to_iso6392(lang_iso6391)
    if not iso6392:
        raise MetadataWriteError(
            f"no ISO 639-2 mapping for {lang_iso6391!r} — skipping tag write"
        )

    ext = media_path.suffix.lower()
    if ext not in _MATROSKA_EXTS:
        raise MetadataWriteError(
            f"tag write-back is MKV/MKA/WebM only ({ext} not supported); "
            "the source file is left untouched. Detection still drives "
            "transcription correctness — only the persist-to-Emby step is skipped."
        )

    audio_pos = _audio_position(media_path, track_index)
    if audio_pos is None:
        raise MetadataWriteError(
            f"stream {track_index} in {media_path} is not an audio stream"
        )

    _write_mkv(media_path, audio_pos, iso6392)


def _audio_position(media_path: Path, absolute_index: int) -> int | None:
    """Map an absolute stream index (the one ffprobe reports) to its 1-based
    position within the file's audio streams — the form `mkvpropedit`'s
    `track:aN` syntax expects.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "json",
                str(media_path),
            ],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    streams = json.loads(proc.stdout or "{}").get("streams", [])
    for i, s in enumerate(streams):
        if s.get("index") == absolute_index:
            return i + 1
    return None


def _write_mkv(media_path: Path, audio_pos: int, iso6392: str) -> None:
    """`mkvpropedit` references audio tracks by their 1-based position within
    the audio streams (track:a1, track:a2, ...). It modifies the EBML header
    in place — no remux, no temp file, no audio data touched.

    Failure modes (all caught by caller as best-effort):
    - mkvtoolnix not installed → FileNotFoundError → wrapped error
    - permission denied → non-zero exit → wrapped error
    - rare: not enough void padding in the header for the new tag → mkvpropedit
      either uses what's there or refuses; either way we surface the error
    """
    try:
        proc = subprocess.run(
            [
                "mkvpropedit", str(media_path),
                "--edit", f"track:a{audio_pos}",
                "--set", f"language={iso6392}",
            ],
            capture_output=True, text=True,
        )
    except FileNotFoundError as e:
        raise MetadataWriteError(
            "mkvpropedit not installed (need mkvtoolnix-cli)"
        ) from e
    if proc.returncode != 0:
        raise MetadataWriteError(
            f"mkvpropedit exit {proc.returncode}: {proc.stderr.strip()[:200]}"
        )
