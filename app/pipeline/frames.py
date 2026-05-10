"""In-memory single-frame JPEG extraction via ffmpeg. No temp files —
returns the encoded bytes directly so callers can base64-encode them for
multimodal API calls.

Two seek modes:

- **fast** (default): `-ss <ts> -i <file>` uses ffmpeg's input-seek, which
  snaps to the nearest keyframe. Quick (no decode of intervening frames)
  but with a ~keyframe-interval (usually 1-3 s) error on the requested
  timestamp. Fine for scene-bible keyframes — the description doesn't
  care if it's the literal middle of the shot or a frame or two away.

- **accurate**: `-ss <ts-5> -i <file> -ss 5 -frames:v 1` does a fast
  input seek to ~5s before the target, then an accurate output seek of
  5s (which decodes 5s of intervening video). Frame-accurate at the
  cost of decoding a few seconds of video per cue. Worth it only when
  the frame's exact instant matters (lip-sync verification, fine
  on-screen text OCR).

The settings flag `cinematic_frame_accurate_seek` toggles which mode the
cinematic per-cue extraction uses. Scene-bible keyframes always use fast.
"""
import subprocess


# How many seconds before the target to start fast-seek when running in
# accurate mode. 5s is a safe upper bound on the worst-case keyframe
# interval for modern h.264/h.265 encodes; raise if you're handling
# pathological streams.
_ACCURATE_SEEK_PRE_ROLL = 5.0


def _scale_filter(max_size: int) -> str:
    """Long-edge-N scale filter (max_size px on whichever side is longer,
    proportional on the other, even dimensions for libjpeg-turbo)."""
    return (
        f"scale='if(gt(iw,ih),min({max_size},iw),-2)':"
        f"'if(gt(iw,ih),-2,min({max_size},ih))'"
    )


def extract_frame_bytes(
    media_path: str,
    timestamp: float,
    max_size: int = 1024,
    *,
    accurate: bool = False,
) -> bytes:
    """Extract one JPEG frame at `timestamp` seconds.

    `accurate=False` (default) uses fast keyframe-snap seek — fine for
    scene-bible keyframes and for cinematic when keyframe accuracy is
    enough. `accurate=True` uses combined fast+accurate seek (decodes
    ~5s of intervening video) for frame-accurate output. See the
    module docstring for the trade-off.
    """
    pre_roll = timestamp - _ACCURATE_SEEK_PRE_ROLL
    if accurate and pre_roll > 0:
        # Fast input seek to ~5s before the target, then accurate output
        # seek of the remaining 5s. Decodes 5s of intervening video.
        output_seek = timestamp - pre_roll
        seek_args = [
            "-ss", f"{pre_roll:.3f}",
            "-i", media_path,
            "-ss", f"{output_seek:.3f}",
        ]
    else:
        # Fast keyframe-snap seek. Also the fallback when accurate=True
        # with timestamp < _ACCURATE_SEEK_PRE_ROLL — there's no benefit
        # to a `-ss 0 -i ... -ss <ts>` combo in that range; just do the
        # straight fast seek.
        seek_args = [
            "-ss", f"{timestamp:.3f}",
            "-i", media_path,
        ]

    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error",
         *seek_args,
         "-frames:v", "1",
         "-vf", _scale_filter(max_size),
         "-q:v", "3",
         "-f", "image2pipe",
         "-vcodec", "mjpeg",
         "-"],
        capture_output=True, check=True,
    )
    return result.stdout
