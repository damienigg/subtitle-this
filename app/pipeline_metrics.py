"""Per-run pipeline metrics — accumulated during a job and surfaced on
the Cache Explorer's stats page.

These are the metrics the .vtt-only stats CAN'T derive after the fact:

- **VAD coverage**: total audio duration, total speech Silero detected,
  speech ratio. Tells us whether Silero is letting most of the dialog
  through (high ratio + low cue count → blame downstream) or whether
  it's rejecting big swaths (low ratio → blame VAD config).
- **VAD region distribution**: histogram of region durations.
  Many regions just above the 250 ms ``min_speech_duration_ms`` floor
  suggests Silero is barely catching short words and tuning the
  threshold lower would surface more.
- **Packing pad-drop count**: every cue that Whisper emitted with a
  timestamp inside a packed window's silence-pad zone gets dropped
  by ``remap_cue_to_original`` (returns None). The count tells us
  directly how much content the region-packing optimization is
  costing us.
- **Whisper degenerate-timestamp drops**: cues emitted with
  ``end <= start`` are dropped by ``_parse_segments``. Usually
  hallucinations, but a spike here flags a model in distress.

The three causes the Inception post-mortem identified (VAD strict /
packing pad-drop / Whisper-turbo compressed timestamps) each have a
dedicated metric here so a future run carries enough evidence to
distinguish them with confidence rather than by elimination.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# Region-duration histogram bins, chosen to flank Silero-VAD's defaults:
#   min_speech_duration_ms=250 → regions never fall below this
#   speech_pad_ms=30 + min_silence_duration_ms=100 → tight clustering
# A heavy 0.25-0.5 s bucket signals "barely-survived single-syllable
# regions" which is the classic film-mix VAD pathology.
_BIN_EDGES_SECONDS = [0.25, 0.5, 1.0, 3.0, 10.0]
_BIN_LABELS = ["lt_0_25s", "0_25_to_0_5s", "0_5_to_1s", "1_to_3s", "3_to_10s", "gte_10s"]


def _bin_index(duration_s: float) -> int:
    for i, edge in enumerate(_BIN_EDGES_SECONDS):
        if duration_s < edge:
            return i
    return len(_BIN_EDGES_SECONDS)


@dataclass
class VadMetrics:
    """Aggregated across every per-segment ``detect_speech`` call in
    one run. Numbers refer to the source audio (one track, the one
    Whisper actually decoded), not the .wav extracted to disk."""
    total_audio_seconds: float = 0.0
    total_speech_seconds_detected: float = 0.0
    speech_ratio_pct: float = 0.0
    region_count: int = 0
    avg_region_seconds: float = 0.0
    median_region_seconds: float = 0.0
    region_duration_histogram: dict[str, int] = field(default_factory=dict)
    # Share of regions just above the 250 ms Silero floor — the
    # "barely-passed" zone. Many here = either the film has lots of
    # short reactions/interjections (fine) or VAD is trimming
    # syllables off longer utterances (bad). Combined with the
    # downstream cue count this distinguishes which.
    short_region_pct: float = 0.0


@dataclass
class PackingMetrics:
    """Captured during the STT loop in stt_openvino.py. Single-region
    windows can't suffer pad-drop (no pads inside the window), so the
    pad-drop count specifically incriminates region-packing — turning
    packing off and re-running should drop it to zero."""
    enabled: bool = True
    windows_total: int = 0
    windows_packed: int = 0          # >1 region per window
    windows_single_region: int = 0   # =1 region (no pad-drop risk)
    avg_regions_per_window: float = 0.0
    cue_drop_pad_zone_count: int = 0
    cue_keep_count: int = 0


@dataclass
class WhisperMetrics:
    """Counts what the decoder produced before downstream filters.
    Degenerate-timestamp drops are the well-known turbo-on-packed-
    windows artefact — high counts here corroborate cause #3."""
    cue_drop_degenerate_timestamp_count: int = 0


@dataclass
class PipelineMetrics:
    """The full per-run telemetry record. Each sub-metric is optional
    so consumers downstream (stats sidecar, transcript cache replay)
    can gracefully degrade if a particular phase wasn't instrumented
    in the run that produced the payload."""
    vad: VadMetrics | None = None
    packing: PackingMetrics | None = None
    whisper: WhisperMetrics | None = None


# ── Aggregators (mutable; written to during a run, then finalized) ────────


class VadAggregator:
    """Drop-in collector for ``detect_speech`` output. The STT loop
    invokes ``observe(seg_audio_seconds, regions)`` once per
    600 s-segment iteration; ``finalize()`` produces the immutable
    VadMetrics record persisted to the sidecar."""

    def __init__(self) -> None:
        self.total_audio_seconds: float = 0.0
        self.total_speech_seconds: float = 0.0
        self._region_durations: list[float] = []

    def observe(self, seg_audio_seconds: float, regions: list[tuple[int, int]],
                sample_rate: int) -> None:
        self.total_audio_seconds += seg_audio_seconds
        for r_start, r_end in regions:
            d = max(0.0, (r_end - r_start) / sample_rate)
            self._region_durations.append(d)
            self.total_speech_seconds += d

    def finalize(self) -> VadMetrics:
        m = VadMetrics(
            total_audio_seconds=round(self.total_audio_seconds, 2),
            total_speech_seconds_detected=round(self.total_speech_seconds, 2),
            region_count=len(self._region_durations),
        )
        if self.total_audio_seconds > 0:
            m.speech_ratio_pct = round(
                100.0 * self.total_speech_seconds / self.total_audio_seconds, 1,
            )
        if self._region_durations:
            m.avg_region_seconds = round(
                self.total_speech_seconds / len(self._region_durations), 3,
            )
            sorted_d = sorted(self._region_durations)
            mid = len(sorted_d) // 2
            m.median_region_seconds = round(
                sorted_d[mid] if len(sorted_d) % 2 else
                0.5 * (sorted_d[mid - 1] + sorted_d[mid]),
                3,
            )
            hist = {label: 0 for label in _BIN_LABELS}
            for d in self._region_durations:
                hist[_BIN_LABELS[_bin_index(d)]] += 1
            m.region_duration_histogram = hist
            # Anything below 0.5 s is a "barely-passed" region —
            # the lt_0_25s bin should be empty with Silero defaults,
            # 0_25_to_0_5s carries the diagnostic weight.
            short = hist["lt_0_25s"] + hist["0_25_to_0_5s"]
            m.short_region_pct = round(
                100.0 * short / len(self._region_durations), 1,
            )
        return m


class PackingAggregator:
    """Counts windows + per-cue drop reasons during the STT inner loop.
    ``record_window(n_regions)`` once per Whisper call;
    ``record_cue_drop_pad_zone()`` / ``record_cue_keep()`` per parsed cue.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.windows_total = 0
        self.windows_packed = 0
        self.windows_single_region = 0
        self._regions_per_window_sum = 0
        self.cue_drop_pad_zone_count = 0
        self.cue_keep_count = 0

    def record_window(self, n_regions: int) -> None:
        self.windows_total += 1
        self._regions_per_window_sum += n_regions
        if n_regions <= 1:
            self.windows_single_region += 1
        else:
            self.windows_packed += 1

    def record_cue_drop_pad_zone(self) -> None:
        self.cue_drop_pad_zone_count += 1

    def record_cue_keep(self) -> None:
        self.cue_keep_count += 1

    def finalize(self) -> PackingMetrics:
        avg = (
            round(self._regions_per_window_sum / self.windows_total, 2)
            if self.windows_total else 0.0
        )
        return PackingMetrics(
            enabled=self.enabled,
            windows_total=self.windows_total,
            windows_packed=self.windows_packed,
            windows_single_region=self.windows_single_region,
            avg_regions_per_window=avg,
            cue_drop_pad_zone_count=self.cue_drop_pad_zone_count,
            cue_keep_count=self.cue_keep_count,
        )


class WhisperAggregator:
    """Tracks whisper-output-level drops (currently just degenerate
    timestamps from ``_parse_segments``). Lives next to the packing
    aggregator so all per-run pipeline counters share a single API."""

    def __init__(self) -> None:
        self.cue_drop_degenerate_timestamp_count = 0

    def record_degenerate_timestamp_drop(self, n: int = 1) -> None:
        self.cue_drop_degenerate_timestamp_count += n

    def finalize(self) -> WhisperMetrics:
        return WhisperMetrics(
            cue_drop_degenerate_timestamp_count=self.cue_drop_degenerate_timestamp_count,
        )


def to_jsonable(metrics: PipelineMetrics) -> dict[str, Any]:
    """Flatten to a JSON-safe dict for sidecar + API output. None
    sub-records render as null so a downstream consumer can tell
    "phase wasn't instrumented" apart from "phase ran but found zero."""
    return asdict(metrics)
