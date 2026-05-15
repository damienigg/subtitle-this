"""On-disk cache for Whisper transcription results.

Whisper is the long pole of the pipeline — 8-80% of the progress
budget, often 30+ minutes of CPU/iGPU work for a 2 h film at
large-v3-turbo. Translation by comparison is fast (10-15 min for
NLLB-1.3B). If translation crashes (OOM, transient API error, the
container getting restarted) the user has to re-run from zero today
— a fresh half-hour of Whisper before they even get back to the
phase that failed. That's the gap this module closes.

The cache stores the `TranscriptionResult` to disk immediately after
`stt.transcribe()` returns, BEFORE the translation phase touches
anything. On retry, processor.py looks here first; a hit skips
audio extraction AND Whisper entirely and jumps straight to
translation.

Cache key dimensions — only the inputs that materially change
Whisper's output:

- **content_fingerprint** (the same one the main VTT cache uses) —
  bytes-stable across mtime bumps and path moves.
- **whisper_model** — small vs. medium vs. large-v3-turbo etc.
- **whisper_backend** — openvino and faster-whisper can produce
  slightly different cue boundaries.
- **vad_enabled** — toggles silence pre-filtering; materially
  changes the cue list (silent-region hallucinations on vs. off).
- **track_index** — which audio track was selected.
- **vocal_isolation** — whether the audio fed to Whisper was the
  raw track or the Demucs vocals stem; the cue list differs
  materially between the two. Encoded as "viYES"/"viNO" so the
  flag survives toggling cleanly across runs.

NOT in the key (deliberately):

- target_lang, provider, mode, LLM settings, scene/cinematic knobs
  — these are downstream of transcription. The whole point is to
  let those change between runs without invalidating the transcript.
- language_hint — derived deterministically from the audio + track
  metadata, both of which are captured by the fingerprint + track_index.

Storage is one JSON file per key under
``cache_dir/transcripts/{key}.json``. Atomic via tmp + os.replace.
Corrupted files are renamed to ``.corrupt`` on load rather than
crashing the pipeline.

Cleanup policy: none, for now. A 2 h film with ~1500 cues serializes
to ~200 KB. Users with disk pressure can ``rm -rf cache_dir/transcripts/``
at any time — the next run just re-transcribes.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from app.config import settings
from app.pipeline.stt import Cue, TranscriptionResult


_log = logging.getLogger(__name__)


def _store_dir() -> Path:
    return Path(settings.cache_dir) / "transcripts"


def _pm_from_dict(data: dict) -> "PipelineMetrics | None":
    """Rehydrate a PipelineMetrics dataclass from its JSON form.
    Tolerates missing keys / unknown types — anything that can't be
    decoded becomes None so a partial / older payload doesn't crash
    the lookup. Lazy import keeps the metrics module out of this
    file's import-time graph for the common path."""
    if not isinstance(data, dict):
        return None
    from app.pipeline_metrics import (
        PipelineMetrics, VocalIsolationMetrics, VadMetrics, PackingMetrics,
        WhisperMetrics, TranslationMetrics, AudioPrepMetrics,
        AntiHallucinationMetrics, PolishMetrics, RefineMetrics,
    )
    def _construct(cls, src):
        if not isinstance(src, dict):
            return None
        # Filter to known fields so a future schema field on disk doesn't
        # break old code via TypeError.
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in src.items() if k in known})
    whisper = _construct(WhisperMetrics, data.get("whisper"))
    if whisper is not None and isinstance(whisper.refine, dict):
        # Nested refine record was rehydrated as a bare dict by the
        # generic _construct (no per-field type inspection). Re-coerce
        # to a RefineMetrics so the template's attribute access works.
        whisper.refine = _construct(RefineMetrics, whisper.refine)
    return PipelineMetrics(
        audio_prep=_construct(AudioPrepMetrics, data.get("audio_prep")),
        vocal_isolation=_construct(VocalIsolationMetrics, data.get("vocal_isolation")),
        vad=_construct(VadMetrics, data.get("vad")),
        packing=_construct(PackingMetrics, data.get("packing")),
        whisper=whisper,
        anti_hallucination=_construct(AntiHallucinationMetrics, data.get("anti_hallucination")),
        polish=_construct(PolishMetrics, data.get("polish")),
        translation=_construct(TranslationMetrics, data.get("translation")),
    )


def _key(
    content_fp: str,
    whisper_model: str,
    whisper_backend: str,
    vad_enabled: bool,
    track_index: int,
    vocal_isolation_enabled: bool = False,
) -> str:
    """Stable composite key. Order matters only for readability — these
    fields are concatenated, not hashed, so changing order would break
    existing cache files. Don't reorder unless you bump the schema
    version prefix (and accept the one-time miss across upgrade).

    Schema versions:
    - v3 (current): adds vocal_isolation_enabled dimension. Audio fed
      to Whisper is materially different (full mix vs vocals stem)
      so transcripts can't be shared between the two modes.
    - v2 (pre-0.7.23): cues carry source-audio-absolute timestamps.
    - v1 (pre-0.7.2): cues from multi-segment runs had timestamps stamped
      segment-relative because the region-packing remap dropped the
      additive seg_offset_seconds. Files with v1-shaped timestamps look
      structurally valid but collapse every cue into the first 600 s of
      the timeline on long media — invalidating the prefix forces a
      re-transcribe so users don't silently inherit broken caches.
    """
    return (
        f"v3"
        f"_{content_fp}"
        f"_{whisper_backend}"
        f"_{whisper_model.replace('/', '-')}"
        f"_vad{int(bool(vad_enabled))}"
        f"_vi{int(bool(vocal_isolation_enabled))}"
        f"_t{track_index}"
    )


def lookup(
    content_fp: str,
    whisper_model: str,
    whisper_backend: str,
    vad_enabled: bool,
    track_index: int,
    vocal_isolation_enabled: bool = False,
) -> TranscriptionResult | None:
    """Returns the cached transcription for these inputs, or None on miss
    or corrupted file. Never raises — failures are logged + the file is
    quarantined so the next run can re-transcribe cleanly."""
    path = _store_dir() / f"{_key(content_fp, whisper_model, whisper_backend, vad_enabled, track_index, vocal_isolation_enabled)}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        cues = [Cue(**c) for c in data["cues"]]
        # pipeline_metrics is optional (added in 0.7.6) — older caches
        # won't have it, and the dataclass field default is None so
        # the absence reads cleanly. We re-hydrate from the flat dict
        # rather than reconstructing the dataclass tree because the
        # downstream consumer (processor → VTT cache payload → stats)
        # passes it through as opaque JSON anyway.
        pm_data = data.get("pipeline_metrics")
        pipeline_metrics = _pm_from_dict(pm_data) if pm_data else None
        return TranscriptionResult(
            detected_language=data["detected_language"],
            cues=cues,
            pipeline_metrics=pipeline_metrics,
        )
    except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
        _log.warning("transcript_cache: %s unreadable (%s) — quarantining", path, e)
        try:
            path.rename(path.with_suffix(".corrupt"))
        except OSError:
            pass
        return None


def store(
    content_fp: str,
    whisper_model: str,
    whisper_backend: str,
    vad_enabled: bool,
    track_index: int,
    result: TranscriptionResult,
    vocal_isolation_enabled: bool = False,
) -> None:
    """Persist the transcription. Atomic — writes to ``.tmp`` and renames.
    Best-effort: any IO error is logged and swallowed, since persistence
    is a retry-resume optimization, not a correctness requirement."""
    if not result.cues:
        return   # don't cache empty transcriptions
    from app.util import atomic_write
    store_dir = _store_dir()
    try:
        path = store_dir / f"{_key(content_fp, whisper_model, whisper_backend, vad_enabled, track_index, vocal_isolation_enabled)}.json"
        payload = {
            "detected_language": result.detected_language,
            "cues": [asdict(c) for c in result.cues],
        }
        # Persist pipeline_metrics so a cache hit preserves the
        # provenance numbers — without this, replaying from the
        # transcript cache would silently lose the VAD / packing
        # telemetry the original run captured.
        if result.pipeline_metrics is not None:
            payload["pipeline_metrics"] = asdict(result.pipeline_metrics)
        atomic_write(path, json.dumps(payload))
    except Exception:
        _log.warning("transcript_cache: failed to save", exc_info=True)
