"""Audio extraction from arbitrary container formats into a 16 kHz mono
WAV that the Whisper backends can consume.

Quality features applied at extraction time:

- **Center-channel extraction on 5.1+ sources.** In every professional
  surround mix, dialogue is mixed into the FC (front-center) channel.
  Pulling JUST that channel gives Whisper a near-clean dialogue
  signal, completely free of score / SFX / ambience that would
  otherwise need a Demucs pass to separate. On Inception's final
  reel (where Zimmer's score buries the dialog in the stereo
  downmix), this single change recovers most of the coverage gap
  that vocal-isolation was trying to close — and it's deterministic,
  artifact-free, and ~5 s of ffmpeg work instead of 15-30 min of
  Demucs. Stereo and mono sources fall through to a standard mono
  downmix.

- **EBU R128 loudness normalization (-23 LUFS).** Whisper was trained
  on audio normalized to roughly this range. Cinema-mastered tracks
  often sit at -8 to -18 LUFS (much louder than training), and music
  videos / talks at -14 LUFS (loud) — both are out-of-distribution
  enough that WER climbs. The ffmpeg ``loudnorm`` filter targets
  -23 LUFS in a single pass (less accurate than two-pass but fast
  and "close enough" for Whisper's robustness).

Why a temp WAV (not a pipe): both STT backends want a ``pathlib.Path``
they can pass to soundfile.SoundFile / faster_whisper. Piping
ffmpeg→stdin→soundfile is doable but the cleanup path on cancel/
timeout is fragile, and the disk roundtrip is cheap (~250 MB write
at sequential IO speeds for a 2 h film). Acceptable.

Why temp file lives under settings.cache_dir, NOT /tmp:
A 2 h mono-16 kHz 16-bit WAV is ~250 MB. On TrueNAS Scale, /tmp is
often backed by tmpfs (or a tiny system dataset) — every temp wav
we put there counts against host memory AND can collide with the
container's 6 GB cgroup limit if multiple jobs queue up. Putting
them in ``<cache_dir>/tmp`` lands them on the same persistent volume
the user already sized for the model cache.
"""
import json
import logging
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.config import settings


_log = logging.getLogger("subtitle_this")


# Hard cap on how long audio extraction can run. A typical 2 h film
# extracts in 1-2 min; we set this generously at 60 min so even slow
# network mounts or huge episodics (multi-hour concert recordings)
# don't hit the wall. The job-level wall-clock timeout is the real
# fence — this is just defense-in-depth against a wedged ffmpeg.
_AUDIO_EXTRACT_TIMEOUT_SECONDS = 3600

# Loudnorm target. EBU R128 calls for I=-23 LUFS, LRA=11 LU,
# TP=-1.5 dB. Single-pass is "close enough" for our use case (we're
# not delivering broadcast-spec, we're feeding Whisper) and avoids
# the 2× IO of the two-pass mode.
_LOUDNORM_FILTER = "loudnorm=I=-23:LRA=11:TP=-1.5"


@dataclass
class ChannelInfo:
    """What ``ffprobe`` reports about a specific audio track's
    channel layout. Used to decide between center-channel extraction
    (5.1+ → has_center=True) and the standard mono downmix path."""
    channels: int
    layout: str | None
    has_center: bool


def _tmp_dir() -> Path:
    """Return the directory we put temp wavs in, creating it if needed.
    Reads settings.cache_dir each call so test fixtures that swap the
    cache_dir work without restart."""
    d = Path(settings.cache_dir) / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def probe_channel_layout(media_path: str, track_index: int) -> ChannelInfo:
    """Probe channel count + layout for ONE audio track.

    Returns ``ChannelInfo(channels, layout, has_center)``. ``has_center``
    is True when channels ≥ 6 (i.e. a 5.1, 6.1, 7.1, or Atmos-as-7.1
    layout — all of which have a dedicated FC channel by industry
    convention). This is the conservative gate: 5.0 and 3.0 mixes
    technically also have an FC channel but are rare enough in
    cinema/TV content that we don't bother detecting them; they fall
    through to the standard downmix path. The user gets the better
    result on the 99% case (5.1+) and identical-to-pre-0.7.33
    behaviour on the rest.

    On any ffprobe error, returns ``ChannelInfo(channels=0,
    layout=None, has_center=False)`` — the caller falls back to the
    standard downmix path, which is the safe default."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", str(track_index),
                "-show_entries", "stream=channels,channel_layout",
                "-of", "json",
                media_path,
            ],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ChannelInfo(channels=0, layout=None, has_center=False)

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return ChannelInfo(channels=0, layout=None, has_center=False)

    streams = data.get("streams") or []
    if not streams:
        return ChannelInfo(channels=0, layout=None, has_center=False)
    s = streams[0]
    channels = int(s.get("channels") or 0)
    layout = s.get("channel_layout") or None
    has_center = channels >= 6
    return ChannelInfo(channels=channels, layout=layout, has_center=has_center)


def _build_filter_chain(info: ChannelInfo) -> tuple[list[str], list[str]]:
    """Return ``(af_filter_list, extra_encoder_flags)``.

    The filter list is joined with commas to make ffmpeg's ``-af``
    argument. ``extra_encoder_flags`` includes ``-ac 1`` ONLY when the
    filter chain isn't already producing mono — the pan filter for
    center extraction is its own mono output, so a redundant ``-ac 1``
    would still work but is removed for clarity."""
    filters: list[str] = []
    extra_flags: list[str] = []
    if info.has_center:
        # ``pan=mono|c0=FC`` selects ONLY the front-center channel and
        # outputs mono. Equivalent named-channel form: c0=1.0*FC.
        # Referencing by name (FC) rather than position (c2) is robust
        # to non-standard channel orders in odd remuxes.
        filters.append("pan=mono|c0=FC")
    else:
        # Standard mono downmix at the encoder level — preserves the
        # current behaviour for stereo / mono tracks.
        extra_flags.extend(["-ac", "1"])
    filters.append(_LOUDNORM_FILTER)
    return filters, extra_flags


def _run_ffmpeg_extract(
    media_path: str, track_index: int, out_path: Path,
    filters: list[str], extra_flags: list[str],
) -> None:
    """Run ffmpeg with a specific filter chain. Raises
    ``subprocess.CalledProcessError`` on non-zero exit."""
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-i", media_path,
            "-map", f"0:{track_index}",
            *extra_flags,
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            "-af", ",".join(filters),
            str(out_path),
        ],
        check=True,
        timeout=_AUDIO_EXTRACT_TIMEOUT_SECONDS,
    )


@contextmanager
def extract_audio(media_path: str, track_index: int):
    """Extract a single audio track to a 16 kHz mono WAV temp file
    under settings.cache_dir/tmp/. Yields the path; deletes it on
    context exit even when the caller raised.

    Applies center-channel extraction (5.1+ sources) and EBU R128
    loudness normalization automatically — see the module docstring
    for the rationale on each.

    **Safety net**: if the optimised filter chain (pan=FC + loudnorm)
    fails — e.g. ffprobe claims 5.1 but the actual stream has a
    non-standard layout with no FC channel — we retry with a bare
    downmix-only command. The user still gets a valid 16 kHz mono
    WAV, just without the center-channel optimisation. Better than
    failing the whole job over an edge-case mux."""
    info = probe_channel_layout(media_path, track_index)
    filters, extra_flags = _build_filter_chain(info)
    if info.has_center:
        _log.info(
            "audio prep: source has %d channels (layout=%s) → center-channel "
            "extraction (FC). Skipping the stereo-downmix path; the FC "
            "channel is dialogue-only by mix convention.",
            info.channels, info.layout,
        )

    # delete=False so we control teardown in the `finally` (the with-block
    # would clobber the path on __exit__ before we yield).
    with tempfile.NamedTemporaryFile(
        suffix=".wav", delete=False, dir=str(_tmp_dir()),
    ) as tmp:
        out_path = Path(tmp.name)
    try:
        try:
            _run_ffmpeg_extract(media_path, track_index, out_path, filters, extra_flags)
        except subprocess.CalledProcessError as e:
            if info.has_center:
                # Optimised path failed — almost certainly the
                # ``pan=mono|c0=FC`` filter rejected the layout. Fall
                # back to the standard downmix path so the user gets
                # a job result instead of an error.
                _log.warning(
                    "audio prep: optimised filter chain failed (ffmpeg "
                    "exit %d) — retrying with standard stereo downmix. "
                    "This usually means the source claims %d channels "
                    "but has a non-standard layout missing FC.",
                    e.returncode, info.channels,
                )
                fallback_filters = [_LOUDNORM_FILTER]
                fallback_flags = ["-ac", "1"]
                _run_ffmpeg_extract(
                    media_path, track_index, out_path,
                    fallback_filters, fallback_flags,
                )
            else:
                # Standard path failed and there's no safer fallback
                # left to try. Bubble the error up.
                raise
        yield out_path
    finally:
        out_path.unlink(missing_ok=True)
