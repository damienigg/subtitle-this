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
class AudioPrepMetrics:
    """Captured at audio-extraction time. Records WHICH input-prep path
    the pipeline chose and whether any safety fallback fired.

    - ``source_channels`` / ``source_channel_layout``: what ffprobe
      reported for the selected audio track.
    - ``used_center_channel``: True when the 5.1+ optimised path ran
      (``pan=mono|c0=FC`` — Whisper saw dialogue-only audio). False
      means the standard stereo-to-mono downmix path.
    - ``loudnorm_applied``: the EBU R128 ``loudnorm=I=-23`` filter is
      currently applied on every path; the flag exists so a future
      ablation run (loudnorm off) can be told apart from a legacy
      pre-0.7.33 entry where the field is None.
    - ``optimised_chain_failed``: True when the optimised filter
      chain rejected the layout and we fell back to plain downmix.
      Reading this with ``used_center_channel`` lets the stats page
      surface "we tried center extraction but had to retry" vs
      "we never tried it" vs "it worked".
    - ``vocal_isolation_auto_skipped`` (0.9.1+): True when the user
      had ``vocal_isolation_mode != "off"`` in settings BUT the
      source was 5.1+ so Demucs got skipped in favour of the cheaper,
      cleaner FC-pan extraction. Surfaces on the stats page so the
      operator understands why the Vocal isolation block they
      enabled in Settings didn't actually run on this entry.
    """
    source_channels: int = 0
    source_channel_layout: str | None = None
    used_center_channel: bool = False
    loudnorm_applied: bool = False
    optimised_chain_failed: bool = False
    vocal_isolation_auto_skipped: bool = False


@dataclass
class VocalIsolationMetrics:
    """Captured when the Demucs vocal-isolation phase ran. None when the
    feature was off for this run — downstream consumers gate the stats
    page section on presence.

    The headline number is ``took_seconds`` vs ``audio_seconds_processed``:
    a 2 h film with took=900 s means Demucs ran at ~8x realtime, which
    is the realistic ceiling on a 4-core CPU-bound container."""
    enabled: bool = False
    model: str | None = None
    took_seconds: float = 0.0
    audio_seconds_processed: float = 0.0
    # realtime_factor = audio_seconds_processed / took_seconds.
    # >1 = ran faster than the audio length (good); <1 = slower
    # than realtime (expected on CPU + htdemucs). Rounded to 2 sig
    # figs because more precision would be misleading.
    realtime_factor: float = 0.0


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
    packing off and re-running should drop it to zero.

    0.7.11 introduced snap recovery: when a cue's timestamp lands in
    a silence pad, we now snap it to the closest region instead of
    silently dropping the cue. ``cue_snap_pad_zone_count`` tracks how
    many cues were rescued this way; ``cue_drop_pad_zone_count`` now
    only counts cues whose snap target was degenerate (end ≤ start
    after snap — usually a hallucination on a 50-ms pad slice).
    """
    enabled: bool = True
    windows_total: int = 0
    windows_packed: int = 0          # >1 region per window
    windows_single_region: int = 0   # =1 region (no pad-drop risk)
    avg_regions_per_window: float = 0.0
    cue_drop_pad_zone_count: int = 0
    cue_snap_pad_zone_count: int = 0    # rescued, ≤ 0.5 s time-shifted
    cue_keep_count: int = 0


@dataclass
class RefineMetrics:
    """Telemetry from the 0.8.0 confidence-gated re-transcription pass.
    Surfaces on the stats page so the operator can see when the refine
    phase fired and how much it changed.

    - ``buckets_weak``: how many 10-min buckets the first pass produced
      that were below the coverage or logprob threshold.
    - ``buckets_refined``: how many of those weak buckets we actually
      re-decoded (capped at 20 % of total audio).
    - ``cues_added`` / ``cues_replaced``: delta cues from the merge.
    - ``audio_seconds_refined``: how much audio we re-passed.
    - ``skipped_reason``: populated when the whole phase was a no-op
      (``"first_pass_clean"``, ``"no_logprob_data"`` for OpenVINO,
      ``"no_buckets_in_budget"``, etc.).
    """
    buckets_evaluated: int = 0
    buckets_weak: int = 0
    buckets_refined: int = 0
    cues_added: int = 0
    cues_replaced: int = 0
    audio_seconds_refined: float = 0.0
    skipped_reason: str | None = None


@dataclass
class WhisperMetrics:
    """Counts what the decoder produced before downstream filters.
    Degenerate-timestamp drops are the well-known turbo-on-packed-
    windows artefact — high counts here corroborate cause #3.

    ``hallucinations_dropped`` is the count of cues removed by the
    0.7.33 anti-hallucination filter (blacklist hits + n-gram
    repetition stuck-loops). Spikes signal that Whisper had a hard
    time on this audio — typically: long silent stretches mistaken
    for low-confidence speech.

    ``refine`` is the 0.8.0 confidence-gated re-transcription
    sub-metrics block. ``None`` when the refine phase didn't run
    (cache hit, no cues at all)."""
    cue_drop_degenerate_timestamp_count: int = 0
    hallucinations_dropped: int = 0
    refine: RefineMetrics | None = None


@dataclass
class AntiHallucinationMetrics:
    """Captured from the 0.7.33 anti-hallucination filter.
    Splits the drop count by category (blacklist vs n-gram repetition)
    so the stats page can distinguish a YouTube-tail-heavy run from a
    stuck-loop-heavy run.

    - ``safety_bailout``: True when the >= 90% drop-threshold guard
      fired and the filter returned the ORIGINAL cue list unchanged.
      A True here means the counts ARE what we WOULD have dropped,
      not what we actually dropped — the operator should review the
      .vtt before trusting it.
    """
    input_count: int = 0
    blacklist_dropped: int = 0
    repetition_dropped: int = 0
    output_count: int = 0
    safety_bailout: bool = False


@dataclass
class PolishMetrics:
    """Captured during the 0.7.20+ readability polish pass. Counts
    each kind of edit so the stats page can show how much the cue
    list was reshaped from raw STT output.

    - ``cues_merged``: pairs that the merge pass collapsed into one.
      A value of 5 means 5 cues vanished into their predecessors
      (input count - output count = cues_merged when merge is the
      only operation that removes cues).
    - ``cues_extended``: cues whose ``end`` was pushed forward by
      the extend pass to meet the minimum-display-duration floor.
    - ``enabled``: False when ``settings.polish_enabled`` was off
      for this run — distinguishes "polish ran with no edits to
      make" (enabled=True, counts=0) from "polish was disabled"
      (enabled=False).
    """
    enabled: bool = False
    input_count: int = 0
    output_count: int = 0
    cues_merged: int = 0
    cues_extended: int = 0


@dataclass
class TranslationMetrics:
    """Captured after ``provider.translate(...)`` returns. We measure
    from OUTSIDE the provider — counts + chars on input vs output cue
    lists, plus the wall-clock took — so the same metrics work for
    every provider (NLLB / DeepL / LLM) without each one needing its
    own internal instrumentation.

    Two pathologies show up cleanly here:

    - **Empty outputs**: many cues translated to the empty string.
      Common with int8-quantized NLLB on certain seq2seq configs
      (the 0.7.1 candidate we ruled out via timestamp-collapse
      diagnosis). Spike here → quantization is degenerate; turn
      ``nllb_load_in_8bit`` off.
    - **Duplicate outputs**: many cues translate to the same string.
      Classic model-collapse signature (temperature too high, or
      the model is stuck in a low-entropy basin). Less common but
      worth a flag.
    """
    provider: str | None = None
    model: str | None = None
    took_seconds: float = 0.0
    input_cue_count: int = 0
    output_cue_count: int = 0
    input_total_chars: int = 0
    output_total_chars: int = 0
    char_ratio: float = 0.0           # output_chars / input_chars
    empty_output_count: int = 0
    duplicate_output_count: int = 0   # cues whose text matches another's


def compute_translation_metrics(
    *,
    provider: str | None,
    model: str | None,
    input_cues,
    output_cues,
    took_seconds: float,
) -> TranslationMetrics:
    """Pure function — takes input and output cue lists from a finished
    translation call and reports the diagnostic counts. Aggregator-free
    because translation runs once per job and we don't need a streaming
    interface (unlike VAD/packing which fire many times during STT)."""
    in_chars = sum(len(c.text) for c in input_cues)
    out_texts = [c.text for c in output_cues]
    out_chars = sum(len(t) for t in out_texts)
    empty = sum(1 for t in out_texts if not t.strip())
    # Duplicate detection: a text counts as duplicated if it appears
    # MORE THAN ONCE in the output. We count the duplicated instances
    # (not just unique duplicates) so a "model emits 'Yes.' 800 times"
    # surfaces as 800, not 1.
    from collections import Counter
    counts = Counter(t for t in out_texts if t.strip())
    dup = sum(n for n in counts.values() if n > 1)
    return TranslationMetrics(
        provider=provider,
        model=model,
        took_seconds=round(took_seconds, 2),
        input_cue_count=len(input_cues),
        output_cue_count=len(output_cues),
        input_total_chars=in_chars,
        output_total_chars=out_chars,
        char_ratio=round(out_chars / in_chars, 3) if in_chars > 0 else 0.0,
        empty_output_count=empty,
        duplicate_output_count=dup,
    )


@dataclass
class PipelineMetrics:
    """The full per-run telemetry record. Each sub-metric is optional
    so consumers downstream (stats sidecar, transcript cache replay)
    can gracefully degrade if a particular phase wasn't instrumented
    in the run that produced the payload."""
    audio_prep: AudioPrepMetrics | None = None
    vocal_isolation: VocalIsolationMetrics | None = None
    vad: VadMetrics | None = None
    packing: PackingMetrics | None = None
    whisper: WhisperMetrics | None = None
    anti_hallucination: AntiHallucinationMetrics | None = None
    polish: PolishMetrics | None = None
    translation: TranslationMetrics | None = None


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
        self.cue_snap_pad_zone_count = 0
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

    def record_cue_snap_pad_zone(self) -> None:
        """Called when remap_cue_to_original recovered a cue via snap
        instead of dropping it. The cue's content survives; only its
        timing is off by ≤ 0.5 s."""
        self.cue_snap_pad_zone_count += 1

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
            cue_snap_pad_zone_count=self.cue_snap_pad_zone_count,
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
