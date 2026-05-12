"""Regression test for the segment-offset bug fixed in 0.7.2.

The OpenVINO STT loop reads audio in N-second segments to bound RAM. Each
segment runs VAD + region-packing + Whisper decode independently; the
resulting cues come back with timestamps relative to the *segment's*
start, and the loop is supposed to add ``seg_offset_seconds`` (= file_pos
in seconds) to lift them into absolute source-audio time.

Between 0.6.0 (when the packing-based remap replaced the additive
offset path) and 0.7.2 that addition was missing. The bug was invisible
on media under ~10 minutes (a single segment, offset == 0) but on a
2 h film every cue from every segment collapsed onto the 0-600 s
window of the timeline — visible to the user as "subtitles appear
correct but all squeezed into the opening minutes of the film".

This test simulates a 120 s audio stream split into two 60 s segments,
each containing one short speech region, and asserts that the cue from
the second segment ends up at absolute time >= 60 s. Pre-fix it would
have been ~1 s. Whisper, optimum-intel, and soundfile are all mocked so
the test runs in milliseconds and doesn't depend on the openvino image's
native deps.
"""
from __future__ import annotations

import sys
import types

import numpy as np


def _install_fake_soundfile(audio_samples: np.ndarray, sample_rate: int) -> None:
    """Inject a fake `soundfile` module that returns `audio_samples` when
    SoundFile(...).read() is called. The real transcribe() imports
    soundfile lazily inside the function, so populating sys.modules first
    is enough — no monkeypatch of stt_openvino needed."""

    class _FakeSoundFile:
        def __init__(self, path):
            self._pos = 0
            self.samplerate = sample_rate
            self.frames = len(audio_samples)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def seek(self, pos):
            self._pos = pos
        def read(self, frames, dtype="float32"):
            end = min(self._pos + frames, len(audio_samples))
            chunk = audio_samples[self._pos:end].astype(dtype, copy=False)
            self._pos = end
            return chunk

    fake = types.ModuleType("soundfile")
    fake.SoundFile = _FakeSoundFile
    sys.modules["soundfile"] = fake


def test_transcribe_applies_segment_offset_to_cues(monkeypatch):
    sr = 16000
    seg_sec = 60   # small enough to stay fast; large enough to be plausible
    audio = np.zeros(seg_sec * 2 * sr, dtype=np.float32)

    _install_fake_soundfile(audio, sr)
    # `soundfile` was injected via sys.modules — undo on teardown so other
    # tests in the suite that legitimately import it (or expect ImportError)
    # see a clean state.
    monkeypatch.setitem(sys.modules, "soundfile", sys.modules["soundfile"])

    from app.config import settings
    from app.pipeline import stt_openvino

    # Whisper-decoded outputs returned in order, one per segment's single
    # window. Each declares a cue inside the speech region (samples 0-80000)
    # so remap_cue_to_original returns a (start, end) that the loop then
    # has to lift by seg_offset_seconds.
    decoded_outputs = [
        "<|0.00|><|2.00|> Cue from segment 0<|4.00|>",
        "<|0.00|><|1.00|> Cue from segment 1<|3.00|>",
    ]

    class _FakeFeatureExtractor:
        def __call__(self, *a, **kw):
            class _F:
                input_features = "fake_features"
            return _F()

    class _FakeTokenizer:
        def decode(self, ids, **kw):
            return decoded_outputs.pop(0) if decoded_outputs else ""

    class _FakeProcessor:
        feature_extractor = _FakeFeatureExtractor()
        tokenizer = _FakeTokenizer()
        def get_decoder_prompt_ids(self, *a, **kw):
            return None

    class _FakeModel:
        def generate(self, features, **kw):
            # One element per batch slot; tokenizer.decode supplies the
            # actual content we care about, so a placeholder suffices.
            return [None]

    monkeypatch.setattr(
        stt_openvino, "_model_and_processor",
        lambda *a, **kw: (_FakeModel(), _FakeProcessor()),
    )
    # VAD: one 5 s speech region at the start of each segment. Keeping it
    # short means plan_packed_windows produces exactly one 30 s window per
    # segment — simpler to reason about than multi-window segments.
    monkeypatch.setattr(
        stt_openvino, "detect_speech",
        lambda seg, sample_rate: [(0, 5 * sample_rate)],
    )

    monkeypatch.setattr(settings, "stt_audio_segment_seconds", seg_sec, raising=False)
    monkeypatch.setattr(settings, "stt_segment_overlap_seconds", 0, raising=False)
    monkeypatch.setattr(settings, "stt_region_packing", True, raising=False)
    monkeypatch.setattr(settings, "vad_enabled", True, raising=False)
    monkeypatch.setattr(settings, "whisper_model", "small", raising=False)
    monkeypatch.setattr(settings, "openvino_device", "CPU", raising=False)

    from pathlib import Path
    result = stt_openvino.transcribe(Path("ignored.wav"), language_hint="en")

    assert len(result.cues) == 2, (
        f"expected two cues (one per segment), got {len(result.cues)}: "
        f"{[(c.start, c.end, c.text) for c in result.cues]}"
    )

    c0, c1 = result.cues
    # Segment 0 cue: offset 0 + window-relative [2.0, 4.0]
    assert 1.5 < c0.start < 2.5, c0
    assert 3.5 < c0.end < 4.5, c0
    # Segment 1 cue: offset 60 + window-relative [1.0, 3.0] = [61.0, 63.0].
    # Pre-fix this would have been [1.0, 3.0]. The >= seg_sec assertion is
    # the load-bearing one — the precise value matters less than that the
    # cue lands *after* segment 0's window.
    assert c1.start >= seg_sec, (
        f"cue from segment 1 has start={c1.start:.3f}s — seg_offset_seconds "
        f"({seg_sec}s) not applied; this is the v0.6.0-0.7.1 regression"
    )
    assert 60.5 < c1.start < 61.5, c1
    assert 62.5 < c1.end < 63.5, c1

    # 0.7.6: the run must also produce pipeline_metrics. We can't be
    # super-strict on values (Silero isn't mocked in this path — VAD
    # itself is bypassed via detect_speech monkeypatch), but the
    # *presence* of each sub-record is a useful regression guard.
    pm = result.pipeline_metrics
    assert pm is not None, "transcribe() should attach pipeline_metrics in 0.7.6+"
    assert pm.vad is not None
    assert pm.packing is not None
    assert pm.whisper is not None
    # Two segments processed, each with one region.
    assert pm.vad.region_count == 2
    # Two windows (one per segment), both single-region.
    assert pm.packing.windows_total == 2
    assert pm.packing.windows_single_region == 2
    assert pm.packing.cue_keep_count == 2
    assert pm.packing.cue_drop_pad_zone_count == 0
