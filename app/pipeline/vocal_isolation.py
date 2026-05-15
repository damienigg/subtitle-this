"""Demucs-based vocal isolation phase.

What this phase does
====================
Runs Demucs to split the source audio into stems (drums / bass / other /
vocals) and keeps only the **vocals** stem, which is then handed to
Whisper instead of the full mix. The rest of the pipeline doesn't care:
both ``audio.extract_audio`` and ``isolate_vocals`` yield the same shape
of artifact (a 16 kHz mono WAV at a ``Path``).

Why this exists
===============
Whisper-large on a full cinema mix routinely loses dialogue under score
and SFX. Silero-VAD compounds the problem by silencing whispered/quiet
lines that *do* exist but sit ≤ 12 dB above the music bed. The Inception
diagnostic run showed the climax + ending (130-145 min) had ~33-0 %
dialog coverage relative to the pro reference — almost certainly because
Hans Zimmer's score dominates the mix and the dialog is buried.

Isolating the vocals stem before STT closes most of that gap. The
silence between phrases in the isolated track is *real* silence, so VAD
becomes nearly redundant (it still runs, but rejects almost nothing).

Why we bypass ``demucs.api.Separator``
======================================
The PyPI release of ``demucs`` (4.0.1, June 2023) does not ship the
``demucs.api`` submodule — that wrapper landed in the GitHub master
branch after the 4.0.1 cut but Facebook never published a follow-up
release. ``from demucs.api import Separator`` raises ImportError against
every installable PyPI version.

This module instead calls the lower-level entry points that
``Separator`` itself wraps:
- ``demucs.pretrained.get_model(name)`` → loads weights
- ``demucs.apply.apply_model(model, mix, ...)`` → runs separation
These are stable and exist in 4.0.1. Behaviour is identical; we just
own the orchestration ourselves instead of going through the missing
high-level wrapper.

Phase-level RAM lifecycle
=========================
Demucs htdemucs weights load to ~1-2 GB of resident PyTorch state during
the apply_model() call. Holding that alongside a freshly-loaded
Whisper (~1.5 GB) + NLLB (~3 GB) blows past the typical 12 GB cgroup on
TrueNAS deployments.

The context manager below loads → runs → **explicitly releases** the
model BEFORE yielding the vocals WAV. By the time STT enters with a
``with`` block on the yielded path, Demucs occupies zero resident
memory. The vocals WAV file persists on disk through STT (where the
file is mmap'd by soundfile) and gets unlinked when the context exits.

This is the same pattern stt_openvino.release_model uses between STT
and translation — see app/pipeline/stt.py:try_malloc_trim for why
gc.collect() alone is insufficient on glibc.

Dependency
==========
Demucs is an **opt-in** dependency installed via the ``vocal-isolation``
extra (``pip install subtitle-this[vocal-isolation]``) or directly in
the Dockerfile when building the image. The import is lazy so the rest
of the app never pays for Demucs unless the feature is actually used.

Caching note
============
NOT cached on disk in this iteration — re-running the isolation costs
2-10 min of CPU per film. The transcript cache covers the common
recovery case (STT succeeded, translation failed → next run skips both
isolation and STT). The narrow window where isolation is wasted is
"isolation succeeded, STT crashed before completion" which is rare.
The transcript cache key now also includes ``vocal_isolation_enabled``
so toggling the feature ON/OFF properly invalidates.
"""
from __future__ import annotations

import gc
import logging
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from app.config import settings
from app.pipeline.stt import try_malloc_trim


_log = logging.getLogger("subtitle_this")


def _noop_progress(frac: float) -> None: ...
def _noop_cancel() -> None: ...


# Module-level state — lets release_model() find what to free without
# the caller threading a model handle through. Mirrors the per-backend
# cache pattern in stt_openvino / stt_faster_whisper.
_model = None
_model_name_cached: str | None = None


def is_available() -> tuple[bool, str | None]:
    """Probe the imports without raising. Returns ``(ok, error_message)``.
    Used by the Settings UI to render an inline warning if the user
    toggles ``vocal_isolation_enabled`` on an image that doesn't ship
    the ``demucs`` package.

    Probes the two entry points we actually use (``demucs.pretrained``
    and ``demucs.apply``) rather than ``demucs.api`` — see the module
    docstring for why."""
    try:
        from demucs.pretrained import get_model  # noqa: F401
        from demucs.apply import apply_model  # noqa: F401
        return True, None
    except ImportError as e:
        return False, str(e)


def _load_model(model_name: str):
    """Lazy-load and cache the Demucs model. Reuses the cached instance
    when ``model_name`` matches; otherwise releases the old one first so
    we never hold two model weight tensors simultaneously.

    Raises ImportError with an actionable message when the demucs
    package isn't installed on this image."""
    global _model, _model_name_cached
    if _model is not None and _model_name_cached == model_name:
        return _model

    try:
        from demucs.pretrained import get_model
    except ImportError as e:
        raise ImportError(
            "demucs is not installed (or installed but broken) in this "
            "image. Either turn off `vocal_isolation_enabled` in "
            "Settings, or pull a newer image from GHCR "
            "(`docker compose pull && docker compose up -d`). The "
            "vocal-isolation extra has shipped in the GHCR images "
            "since 0.7.23. Underlying error: " + str(e)
        ) from e

    # Drop any previously cached model before loading the new one —
    # keeps peak memory at one model's worth.
    if _model is not None:
        release_model()

    # device="cpu" because we don't bind Demucs to CUDA/iGPU yet. On a
    # 4-core capped TrueNAS deployment this runs ~3-8x realtime — a 2 h
    # film isolates in ~15-30 min. Acceptable as a quality-vs-time
    # trade for users who turn the feature on.
    model = get_model(model_name)
    model.cpu()
    try:
        model.eval()
    except AttributeError:
        # BagOfModels (the htdemucs default) doesn't expose .eval() at
        # the top level — its child Demucs models each handle eval mode
        # internally. Safe to skip.
        pass
    _model = model
    _model_name_cached = model_name
    return model


def release_model() -> None:
    """Evict the cached Demucs model + run gc.collect + malloc_trim.

    Called from inside ``isolate_vocals`` AFTER separation completes
    and BEFORE the context manager yields the vocals WAV path — so
    Whisper's load doesn't pile on top of an idle Demucs.

    Safe to call when no model is cached (no-op then). Cheap to call
    repeatedly."""
    global _model, _model_name_cached
    _model = None
    _model_name_cached = None
    gc.collect()
    try_malloc_trim()


def _ffmpeg_extract_for_demucs(media_path: str, track_index: int) -> Path:
    """Extract the chosen audio track as a 44.1 kHz stereo WAV that
    Demucs's pretrained models expect. Demucs *can* resample internally
    but doing it upfront via ffmpeg is faster and predictable.

    Lives under ``<cache_dir>/tmp/`` for the same reason audio.extract_audio
    does — keeps host /tmp clean and stays on the user's chosen volume."""
    tmp_dir = Path(settings.cache_dir) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        suffix=".demucs_in.wav", delete=False, dir=str(tmp_dir),
    ) as tmp:
        out = Path(tmp.name)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-i", media_path,
            "-map", f"0:{track_index}",
            "-ac", "2",          # stereo (Demucs trained on stereo)
            "-ar", "44100",      # 44.1 kHz (Demucs's source rate)
            "-c:a", "pcm_s16le",
            str(out),
        ],
        check=True,
        timeout=3600,
    )
    return out


def _separate_streaming(
    model,
    raw_wav: Path,
    out_path: Path,
    *,
    chunk_seconds: int = 300,
    progress_within_phase: Callable[[float], None] = _noop_progress,
    check_cancel: Callable[[], None] = _noop_cancel,
) -> float:
    """Run Demucs in chunks, mix the vocals stem to mono + resample to
    16 kHz on the fly, write the result incrementally to ``out_path``.
    Returns the total audio seconds processed.

    Why streaming (added 0.7.29 after Inception OOM): apply_model
    needs the input tensor AND a (sources × channels × samples) output
    tensor BOTH resident in RAM. For a 2.5 h film at 44.1 kHz stereo
    float32 that's 3.13 GB input + 12.5 GB output just for the 4-stem
    case — far above a 6 GB cgroup budget. Internal ``split=True``
    only helps the activation buffers, not the I/O tensors. The fix
    is to chunk the audio ourselves: read N seconds, apply_model,
    extract vocals, mono-mix + resample, write to disk, free, repeat.
    Peak per-chunk is ~423 MB on a 5-min chunk + model weights.

    Why we write 16 kHz mono directly here (instead of producing a
    44.1 k stereo intermediate and resampling in a second pass): the
    intermediate file for a 2.5 h film would be 3 GB on disk and need
    another 3 GB tensor on the resample pass. Fused chunking-and-
    resampling sidesteps both costs.

    The chunk seam artifact at outer-chunk boundaries is small
    (Demucs's internal ``overlap=0.25`` smooths it). For downstream
    STT use this is invisible — Whisper resyncs every 30 s window.

    Why ``soundfile`` and not ``torchaudio.load``: torchaudio 2.6+
    routes ``load()`` through ``load_with_torchcodec``, which raises
    ``ImportError: TorchCodec is required`` unless the separate
    ``torchcodec`` package is installed.

    Tests monkeypatch this function directly so they don't need to
    fake the demucs submodules — the lifecycle invariants (release
    before yield, cleanup on exit) live in ``isolate_vocals`` and are
    independent of how separation is actually performed."""
    import numpy as np
    import torch
    import torchaudio
    import soundfile as sf
    from demucs.apply import apply_model

    sources_list = list(getattr(model, "sources", []))
    if "vocals" not in sources_list:
        raise RuntimeError(
            f"Demucs model produced no 'vocals' stem "
            f"(found: {sources_list})"
        )
    vocals_idx = sources_list.index("vocals")

    with sf.SoundFile(str(raw_wav)) as src:
        sr = src.samplerate
        total_samples = src.frames
        chunk_samples = max(1, int(chunk_seconds) * sr)

        # Build the resampler once; the same nn.Module instance is
        # reused on every chunk so the kaiser_window kernel is computed
        # only once. Pure tensor math, so torchcodec isn't involved.
        if sr != 16000:
            resampler = torchaudio.transforms.Resample(
                orig_freq=sr, new_freq=16000,
                resampling_method="sinc_interp_kaiser",
            )
        else:
            resampler = None

        with sf.SoundFile(
            str(out_path), "w",
            samplerate=16000, channels=1, subtype="PCM_16",
        ) as dst:
            pos = 0
            while pos < total_samples:
                check_cancel()
                # soundfile reads (samples, channels). always_2d makes
                # the shape consistent even for mono input.
                chunk_np = src.read(
                    chunk_samples, dtype="float32", always_2d=True,
                )
                if chunk_np.shape[0] == 0:
                    break

                # (samples, channels) → (channels, samples) for Demucs.
                mix = torch.from_numpy(np.ascontiguousarray(chunk_np.T))
                # Demucs htdemucs is trained on stereo. If the source
                # is mono, duplicate so the model sees the expected
                # input shape.
                if mix.shape[0] == 1 and getattr(model, "audio_channels", 2) == 2:
                    mix = mix.repeat(2, 1)

                # apply_model expects (batch, channels, samples) and
                # returns (batch, sources, channels, samples).
                # shifts=0, split=True, overlap=0.25 are Demucs defaults.
                # ``split=True`` chunks INTERNALLY at segment-seconds
                # for activation memory; the I/O tensors are sized by
                # our outer chunk_samples.
                with torch.no_grad():
                    sources = apply_model(
                        model, mix.unsqueeze(0),
                        device="cpu", shifts=0, split=True, overlap=0.25,
                        progress=False,
                    )[0]
                vocals = sources[vocals_idx]

                # Mono mix (mean of L+R) then 16 k resample. Both are
                # cheap relative to apply_model.
                if vocals.dim() == 2 and vocals.shape[0] > 1:
                    mono = vocals.mean(dim=0, keepdim=True)
                else:
                    mono = vocals
                if resampler is not None:
                    mono = resampler(mono)
                samples = mono.squeeze(0).clamp(-1.0, 1.0).numpy()
                dst.write(samples)

                pos += chunk_np.shape[0]
                progress_within_phase(min(1.0, pos / max(1, total_samples)))

                # Drop refs + force gc so the next chunk's allocations
                # don't pile on top of this one.
                del mix, sources, vocals, mono, samples, chunk_np
                gc.collect()

        return float(total_samples) / float(sr)


@contextmanager
def isolate_vocals(
    media_path: str,
    track_index: int,
    *,
    model_name: str | None = None,
    progress: Callable[[float], None] = _noop_progress,
    check_cancel: Callable[[], None] = _noop_cancel,
) -> Iterator["IsolationResult"]:
    """Context manager that runs Demucs and yields a result handle whose
    ``wav_path`` points to a 16 kHz mono WAV of the vocals stem, ready
    for the STT phase to consume.

    Model lifecycle: loaded inside this block, **released before yield**.
    File lifecycle: vocals WAV created on enter, unlinked on exit.

    Raises ImportError if the demucs package isn't installed.
    Raises subprocess.CalledProcessError if ffmpeg fails to extract.
    Cancel propagation: ``check_cancel`` is called before and after the
    Demucs run; mid-run cancellation isn't supported (Demucs is one
    monolithic call). A canceled job will still finish the current
    isolation and abort at the next checkpoint."""
    model_name = model_name or settings.vocal_isolation_model
    started = time.monotonic()

    progress(0.0)
    check_cancel()
    raw_wav = _ffmpeg_extract_for_demucs(media_path, track_index)
    progress(0.1)

    # Output WAV lives in the same tmp dir so cleanup is uniform.
    tmp_dir = Path(settings.cache_dir) / "tmp"
    with tempfile.NamedTemporaryFile(
        suffix=".vocals.wav", delete=False, dir=str(tmp_dir),
    ) as tmp:
        vocals_wav = Path(tmp.name)

    audio_seconds_processed = 0.0
    try:
        check_cancel()
        model = _load_model(model_name)
        progress(0.2)

        # Stream-process the audio in chunks. The within-phase callback
        # advances 0.0 → 1.0 as chunks complete; we map that span into
        # the (0.2, 0.95) section of the outer phase so the user sees
        # smooth progress through what's typically the longest single
        # part of the run.
        def _within(within: float) -> None:
            # Clamp defensively in case a fake test impl reports >1.
            within = max(0.0, min(1.0, within))
            progress(0.2 + 0.75 * within)

        audio_seconds_processed = _separate_streaming(
            model, raw_wav, vocals_wav,
            chunk_seconds=int(settings.vocal_isolation_chunk_seconds),
            progress_within_phase=_within,
            check_cancel=check_cancel,
        )
        progress(0.95)

        # ── Critical: release Demucs RAM BEFORE yielding ──────────────
        # Whisper / NLLB will load inside the yielded with-block. We
        # don't want them piling on top of an idle Demucs.
        release_model()
        # Free the source 44.1 kHz WAV too — STT only needs the 16 kHz
        # vocals file from here on.
        raw_wav.unlink(missing_ok=True)
        raw_wav = None  # type: ignore[assignment]

        took = time.monotonic() - started
        progress(1.0)
        yield IsolationResult(
            wav_path=vocals_wav,
            model=model_name,
            took_seconds=round(took, 2),
            audio_seconds_processed=round(audio_seconds_processed, 2),
        )
    finally:
        # Belt-and-suspenders cleanup. The release above already nulled
        # raw_wav; this handles the abort-before-release path.
        if raw_wav is not None:
            raw_wav.unlink(missing_ok=True)
        vocals_wav.unlink(missing_ok=True)
        # If we abort between _load_model and the explicit release,
        # make sure Demucs RAM doesn't survive the context. Idempotent.
        release_model()


from dataclasses import dataclass


@dataclass
class IsolationResult:
    """Returned to the caller via the context manager. Carries both the
    artifact path (consumed by STT) and the telemetry (folded into
    PipelineMetrics for the stats page)."""
    wav_path: Path
    model: str
    took_seconds: float
    audio_seconds_processed: float
