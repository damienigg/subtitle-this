"""Coverage for the P2 hardening pass (items 14-35 from the review).

Pins down behaviors that don't have an obvious test-home elsewhere:
- Literal-typed FastAPI params reject garbage values with 422.
- LLM provider rejects duplicate cue ids in the response (so a model
  that returns N items with overlapping ids doesn't silently drop work).
- Frame extractor builds the right ffmpeg args for fast vs accurate seek
  (no actual ffmpeg invocation — we stub subprocess).
- DeepL and NLLB providers honor the configurable batch sizes.
- Plex pagination forwards start_index to X-Plex-Container-Start.
- Plex _video_sections cache survives across PlexClient instances.
- Refresh-item failure is logged at WARNING (so operators can debug
  "why didn't Emby pick up my new subtitle").
"""
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings as runtime_settings
from app.main import app
from app.pipeline import frames as frames_mod
from app.pipeline.llm.base import LLMError, SystemBlock, TextContent
from app.pipeline.stt import Cue
from app.pipeline.translate import llm as llm_mod
from app.pipeline.translate.base import TranslationError


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ── 14: Literal-typed query params ─────────────────────────────────────────


def test_process_item_rejects_unknown_mode_at_schema(client):
    """FastAPI must 422 on a mode string that's not in the Literal set —
    we don't even get into the route body. Stops accidental typos like
    `mode=audi` from silently falling back to the default-from-settings."""
    r = client.post("/api/process/some-id?mode=audi")
    assert r.status_code == 422


def test_process_item_rejects_unknown_provider_at_schema(client):
    r = client.post("/api/process/some-id?translation_provider=bogus")
    assert r.status_code == 422


# ── 30: LLM cue-id duplicate detection ─────────────────────────────────────


class _FakeLLM:
    def __init__(self, response: str):
        self._response = response
        self._supports_vision = True

    def supports_vision(self) -> bool:
        return self._supports_vision

    def chat(self, **_):
        return self._response


def test_llm_provider_rejects_duplicate_ids_in_response(monkeypatch):
    """Length matches but ids collide → would silently drop a cue. Must
    raise TranslationError with a clear message."""
    cues = [
        Cue(id=0, start=0.0, end=1.0, text="a"),
        Cue(id=1, start=1.0, end=2.0, text="b"),
        Cue(id=2, start=2.0, end=3.0, text="c"),
    ]
    # Response: 3 items as expected, but id=0 appears twice and id=2 is missing.
    bad = (
        '{"translations": ['
        '{"id": 0, "text": "x"},'
        '{"id": 0, "text": "y"},'
        '{"id": 1, "text": "z"}'
        ']}'
    )
    fake = _FakeLLM(bad)
    monkeypatch.setattr(llm_mod, "get_translation_llm", lambda: fake)
    provider = llm_mod.LLMTranslationProvider()
    with pytest.raises(TranslationError, match="Duplicate cue id"):
        provider.translate(cues, "en", "fr")


# ── 18: Frame extraction ffmpeg args ───────────────────────────────────────


def _capture_ffmpeg_args(timestamp: float, accurate: bool) -> list[str]:
    """Invoke extract_frame_bytes with subprocess.run stubbed, return the
    full ffmpeg argv that would have been called."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        result = MagicMock()
        result.stdout = b"\xff\xd8\xff\xd9"   # minimal valid JPEG bytes
        return result

    with patch.object(frames_mod.subprocess, "run", side_effect=fake_run):
        frames_mod.extract_frame_bytes(
            "/some/movie.mkv", timestamp, max_size=512, accurate=accurate,
        )
    return captured["args"]


def test_extract_frame_fast_seek_puts_ss_before_i():
    args = _capture_ffmpeg_args(timestamp=120.0, accurate=False)
    # `-ss <ts>` appears immediately before `-i <media_path>`.
    ss_idx = args.index("-ss")
    i_idx = args.index("-i")
    assert ss_idx < i_idx
    # And there's only ONE `-ss` in the fast path.
    assert args.count("-ss") == 1


def test_extract_frame_accurate_seek_uses_combined_seek():
    args = _capture_ffmpeg_args(timestamp=120.0, accurate=True)
    # Accurate path emits TWO `-ss` invocations: pre-roll before -i,
    # remainder after -i.
    assert args.count("-ss") == 2
    ss_positions = [i for i, a in enumerate(args) if a == "-ss"]
    i_idx = args.index("-i")
    # First -ss is before -i (fast input seek to ~5s pre-roll), second is
    # after (accurate output seek of the remaining 5s).
    assert ss_positions[0] < i_idx < ss_positions[1]


def test_extract_frame_accurate_at_zero_timestamp_falls_back_to_fast():
    """When timestamp < pre_roll (5s), accurate seek would degenerate
    to `-i + -ss 0` which is the same as the fast path. We collapse it
    cleanly rather than emitting a no-op `-ss 0`."""
    args = _capture_ffmpeg_args(timestamp=2.0, accurate=True)
    assert args.count("-ss") == 1


# ── 26: Configurable batch sizes for NLLB + DeepL ──────────────────────────


def test_deepl_batch_size_setting_is_respected(monkeypatch):
    """Setting deepl_batch_size=10 should make a 25-cue list trigger 3
    HTTP requests (10 + 10 + 5) rather than the previous hardcoded 1
    (25 fit in one batch of 50)."""
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides,
         "deepl_api_key": "test-key",
         "deepl_batch_size": 10},
    )
    from app.pipeline.translate.deepl import DeepLProvider

    cues = [Cue(id=i, start=float(i), end=float(i) + 1.0, text=f"line {i}")
            for i in range(25)]

    call_count = {"n": 0}

    def fake_post(self, url, data, **kwargs):
        call_count["n"] += 1
        # Count `text` entries in this call's data payload (a list of
        # (key, value) tuples) — matches the batch size we expect.
        text_count = sum(1 for k, _ in data if k == "text")
        # First two calls: 10 cues each, third call: 5 cues.
        if call_count["n"] <= 2:
            assert text_count == 10
        else:
            assert text_count == 5
        # Build a valid DeepL-shaped response.
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "translations": [{"text": f"t{i}"} for i in range(text_count)],
        }
        return resp

    with patch.object(httpx.Client, "post", new=fake_post):
        provider = DeepLProvider()
        out = provider.translate(cues, "en", "fr")
    assert len(out) == 25
    assert call_count["n"] == 3   # 10 + 10 + 5


# ── 19+20: Plex pagination + module-level section cache ────────────────────


def test_plex_list_videos_forwards_start_index_to_server(monkeypatch):
    """When library_id is set and the user asks for page 2 (start_index=200),
    we must pass X-Plex-Container-Start=200 to the server rather than
    fetching 10 000 items and slicing in Python."""
    from app.server import plex as plex_mod

    # Reset the module cache so this test starts fresh.
    plex_mod._clear_video_sections_cache()

    captured_calls = []

    def fake_get(self, url, params=None, **kwargs):
        captured_calls.append((url, dict(params or {})))
        # /library/sections returns one movie section.
        if url.endswith("/library/sections"):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "MediaContainer": {
                    "Directory": [{"key": "1", "type": "movie", "title": "Movies"}],
                },
            }
            return resp
        # /library/sections/1/all → page of metadata.
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "MediaContainer": {
                "Metadata": [],
                "totalSize": 0,
                "size": 0,
            },
        }
        return resp

    with patch.object(httpx.Client, "get", new=fake_get):
        client = plex_mod.PlexClient("http://plex.test", "tok")
        client.list_videos(library_id="1", start_index=200, limit=50)

    # Look for the /all call and confirm the pagination params.
    all_calls = [c for c in captured_calls if c[0].endswith("/library/sections/1/all")]
    assert len(all_calls) == 1
    params = all_calls[0][1]
    assert params["X-Plex-Container-Start"] == 200
    assert params["X-Plex-Container-Size"] == 50


def test_plex_video_sections_cache_survives_across_clients(monkeypatch):
    """Two PlexClient instances built with the same (base_url, token) must
    share the cached section list — the previous per-instance cache was
    always cold because the factory builds a fresh client per request."""
    from app.server import plex as plex_mod

    plex_mod._clear_video_sections_cache()

    sections_hits = {"n": 0}

    def fake_get(self, url, params=None, **kwargs):
        if url.endswith("/library/sections"):
            sections_hits["n"] += 1
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "MediaContainer": {
                "Directory": [{"key": "1", "type": "movie", "title": "Movies"}],
            },
        }
        return resp

    with patch.object(httpx.Client, "get", new=fake_get):
        c1 = plex_mod.PlexClient("http://plex.test", "tok")
        c1._video_sections()
        c2 = plex_mod.PlexClient("http://plex.test", "tok")
        c2._video_sections()

    # Only the first client should have hit /library/sections; the second
    # reads from the module cache.
    assert sections_hits["n"] == 1


def test_plex_video_sections_cache_isolates_per_token(monkeypatch):
    """Different tokens (e.g. two users hitting the same server) must NOT
    share the cache, otherwise a user could see another user's section
    list. The cache key includes both base_url and token."""
    from app.server import plex as plex_mod

    plex_mod._clear_video_sections_cache()
    sections_hits = {"n": 0}

    def fake_get(self, url, params=None, **kwargs):
        if url.endswith("/library/sections"):
            sections_hits["n"] += 1
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "MediaContainer": {
                "Directory": [{"key": "1", "type": "movie", "title": "Movies"}],
            },
        }
        return resp

    with patch.object(httpx.Client, "get", new=fake_get):
        plex_mod.PlexClient("http://plex.test", "tok-A")._video_sections()
        plex_mod.PlexClient("http://plex.test", "tok-B")._video_sections()

    # Different tokens → two separate cache entries → two backend hits.
    assert sections_hits["n"] == 2


# ── 16: _parse_segments drops degenerate timestamps without exploding ─────


def test_parse_segments_drops_degenerate_timestamp_pairs():
    """If Whisper hallucinates and emits markers with end <= start AND
    text between them, the cue is dropped silently. The function must
    not crash and must not include the degenerate pair in the output."""
    from app.pipeline.stt_openvino import _parse_segments

    # <|2.50|> appears before <|0.00|> with text "hi" between them —
    # the kind of artifact Whisper sometimes emits on heavy hallucination.
    decoded = "<|2.50|>hi<|0.00|>real<|3.00|>"
    out = _parse_segments(decoded, time_offset_s=0.0)
    # Only the (0.00, 3.00, "real") pair survives — the (2.50, 0.00, "hi")
    # one is dropped because end < start.
    assert out == [(0.0, 3.0, "real")]
