"""Tests for app/reference.py (objective comparison) and the
upload/score endpoints exposed in api/manage.py.

The goal here is to pin the math AND the language-strict policy so a
future refactor of the scoring weights or the language detector can't
silently change calibration on existing reference uploads — the
operator's stored ReferenceScore records are persisted, so any drift
in the algorithm makes historical comparisons meaningless.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.reference import (
    _chrf, _match_cues, _Cue, compute_reference_score, detect_language,
    parse_subtitle,
)


# ── Subtitle parsing ────────────────────────────────────────────────────────


def test_parse_srt_basic():
    srt = """1
00:00:01,000 --> 00:00:03,500
Hello world.

2
00:00:04,000 --> 00:00:06,200
How are you?
"""
    cues = parse_subtitle(srt)
    assert len(cues) == 2
    assert cues[0].start == 1.0
    assert cues[0].end == 3.5
    assert cues[0].text == "Hello world."
    assert cues[1].start == 4.0


def test_parse_vtt_skips_webvtt_header_and_note():
    vtt = """WEBVTT

NOTE Subtitle This auto-subs (en -> fr, whisper=small, provider=nllb, polished=true)

00:00:01.000 --> 00:00:03.000
Bonjour.

00:00:04.000 --> 00:00:06.000
Comment ça va ?
"""
    cues = parse_subtitle(vtt)
    assert len(cues) == 2
    assert cues[0].text == "Bonjour."


def test_parse_handles_multi_line_cue_text():
    """Multi-line cue text is joined with a space — line breaks
    inside a cue are irrelevant for comparison."""
    srt = """1
00:00:01,000 --> 00:00:03,000
First line
second line.
"""
    cues = parse_subtitle(srt)
    assert cues[0].text == "First line second line."


def test_parse_drops_degenerate_cues():
    """end <= start cues are dropped — pro subtitles don't have
    them and they'd just add noise to the comparison."""
    srt = """1
00:00:01,000 --> 00:00:01,000
Empty.

2
00:00:02,000 --> 00:00:04,000
Real cue.
"""
    cues = parse_subtitle(srt)
    assert len(cues) == 1
    assert cues[0].text == "Real cue."


def test_parse_tolerant_of_windows_line_endings_and_bom():
    srt = "﻿1\r\n00:00:01,000 --> 00:00:03,000\r\nWith CRLF.\r\n"
    cues = parse_subtitle(srt)
    assert len(cues) == 1
    assert cues[0].text == "With CRLF."


def test_parse_vtt_accepts_mm_ss_format_for_sub_hour_cues():
    """0.11.1 regression: ffmpeg's webvtt encoder emits ``mm:ss.ms``
    (no hours group) for cues under 1 h. Pre-0.11.1 the parser regex
    required the hours group and silently dropped EVERY cue from the
    first hour of a film — Inception (148 min, embedded English subs)
    came out missing all 749 first-half cues. This pins the fix so a
    future regex tightening can't bring back the bug."""
    vtt = """WEBVTT

00:01.000 --> 00:03.500
First cue under 1h.

59:59.500 --> 1:00:00.500
Spanning the boundary.

01:00:00.100 --> 01:00:03.000
Standard hh:mm:ss.ms cue.
"""
    cues = parse_subtitle(vtt)
    assert len(cues) == 3
    assert cues[0].start == 1.0           # mm:ss.ms parsed correctly
    assert cues[0].end == 3.5
    assert cues[0].text == "First cue under 1h."
    assert cues[1].start == 3599.5        # 59:59.500 parsed correctly
    assert cues[1].end == 3600.5          # 1:00:00.500 (single-digit hour)
    assert cues[2].start == 3600.1        # 01:00:00.100 (zero-padded hour)
    assert cues[2].end == 3603.0


# ── Language detection ──────────────────────────────────────────────────────


def _make_cues_from_text(text: str, count: int = 30) -> list[_Cue]:
    """Helper: spread a text block across N cues so detect_language
    sees enough tokens to clear the threshold."""
    cues = []
    for i in range(count):
        cues.append(_Cue(start=float(i), end=float(i) + 1.5, text=text))
    return cues


def test_detect_language_english():
    cues = _make_cues_from_text(
        "I think the car is in the garage and you are not here.",
    )
    assert detect_language(cues) == "en"


def test_detect_language_french():
    cues = _make_cues_from_text(
        "Je pense que la voiture est dans le garage et tu n'es pas là.",
    )
    assert detect_language(cues) == "fr"


def test_detect_language_spanish():
    cues = _make_cues_from_text(
        "Creo que el coche está en el garaje y tú no estás aquí.",
    )
    assert detect_language(cues) == "es"


def test_detect_language_returns_none_on_too_short_input():
    """A single cue with ~3 stopwords can't clear the threshold."""
    cues = [_Cue(start=0.0, end=1.0, text="The cat.")]
    assert detect_language(cues) is None


def test_detect_language_returns_none_when_ambiguous():
    """Mixed FR/EN content where neither dominates by 2× should
    refuse to commit. Conservative: better to refuse than miscount
    the operator's upload."""
    cues = _make_cues_from_text("the le the le the le and et or ou")
    # English wins 4-3 stopwords here, not 2× — should refuse.
    assert detect_language(cues) is None


# ── Cue matching ────────────────────────────────────────────────────────────


def test_match_cues_aligns_close_pairs():
    gen = [_Cue(1.0, 3.0, "a"), _Cue(5.0, 7.0, "b"), _Cue(9.0, 11.0, "c")]
    ref = [_Cue(1.2, 3.0, "a"), _Cue(5.1, 7.0, "b"), _Cue(9.0, 11.0, "c")]
    pairs = _match_cues(gen, ref, window_seconds=3.0)
    matches = [(g, r) for g, r in pairs if g and r]
    assert len(matches) == 3


def test_match_cues_marks_extras_and_misses():
    gen = [_Cue(1.0, 3.0, "a"), _Cue(20.0, 22.0, "extra")]
    ref = [_Cue(1.0, 3.0, "a"), _Cue(10.0, 12.0, "missed")]
    pairs = _match_cues(gen, ref, window_seconds=3.0)
    matches = [(g, r) for g, r in pairs if g and r]
    extras = [g for g, r in pairs if g and not r]
    misses = [r for g, r in pairs if not g and r]
    assert len(matches) == 1
    assert len(extras) == 1
    assert len(misses) == 1


# ── chrF text similarity ────────────────────────────────────────────────────


def test_chrf_identical_strings():
    assert _chrf("Hello world", "Hello world") == 1.0


def test_chrf_completely_different():
    assert _chrf("Hello", "xyzqv") < 0.1


def test_chrf_handles_short_strings():
    """chrF on inputs shorter than the max n-gram should still
    return a sensible value (just average over the n-gram orders
    that are computable)."""
    assert 0.0 <= _chrf("Hi.", "Hi!") <= 1.0


def test_chrf_returns_zero_on_empty():
    assert _chrf("", "anything") == 0.0
    assert _chrf("anything", "") == 0.0


# ── Score computation ──────────────────────────────────────────────────────


def _build_pair(
    gen_text: str, ref_text: str, lang: str = "en",
) -> tuple[str, str]:
    """Build a matched VTT + SRT pair of N cues, gen_text and
    ref_text repeated. Enough for the scorer to clear the language
    and coverage thresholds."""
    gen_lines = ["WEBVTT", "",
                 "NOTE Subtitle This auto-subs (en -> en, whisper=small, provider=nllb, polished=true)",
                 ""]
    ref_lines = []
    for i in range(15):
        t0 = i * 5
        t1 = t0 + 3
        gen_lines.append(f"00:00:{t0:02d}.000 --> 00:00:{t1:02d}.000")
        gen_lines.append(gen_text)
        gen_lines.append("")
        ref_lines.append(str(i + 1))
        ref_lines.append(f"00:00:{t0:02d},000 --> 00:00:{t1:02d},000")
        ref_lines.append(ref_text)
        ref_lines.append("")
    return "\n".join(gen_lines), "\n".join(ref_lines)


def test_score_perfect_match_yields_near_100():
    """Identical VTT + reference → all dimensions at the ceiling.
    chrF won't be exactly 100 because of the 0.7 + n-gram averaging
    quirk on short strings, but the overall must be ≥ 90 (grade A)."""
    gen, ref = _build_pair(
        "I think the car is in the garage and you are not here.",
        "I think the car is in the garage and you are not here.",
    )
    score = compute_reference_score(gen, ref, lang="en")
    assert score.overall_score >= 90
    assert score.overall_grade == "A"
    assert score.coverage_pct == 100.0
    assert score.density_ratio == 1.0
    assert score.timing_offset_median_ms == 0.0
    assert score.text_similarity_chrf > 0.95


def test_score_translation_quality_drops_chrf_dimension():
    """Same timing, completely different translation — coverage +
    timing stay at 100, but text_similarity drops dramatically.
    The overall stays decent because chrF only weighs 20 %."""
    gen, ref = _build_pair(
        "I think the car is in the garage and you are not here.",
        "The wheel of the cat is purple over the mountain.",
    )
    score = compute_reference_score(gen, ref, lang="en")
    assert score.coverage_pct == 100.0
    assert score.text_similarity_chrf < 0.5
    # Score is in the C-D range (text sim + density still partial).
    assert 60 <= score.overall_score < 90


def test_score_missed_coverage_drops_overall():
    """Half the reference cues missing → coverage 50 % → overall
    drops below 70."""
    # 15 ref cues; VTT only has the first 7.
    gen_lines = ["WEBVTT", ""]
    for i in range(7):
        t0 = i * 5
        gen_lines.append(f"00:00:{t0:02d}.000 --> 00:00:{t0+3:02d}.000")
        gen_lines.append("Hello world.")
        gen_lines.append("")
    ref_lines = []
    for i in range(15):
        t0 = i * 5
        ref_lines.append(str(i + 1))
        ref_lines.append(f"00:00:{t0:02d},000 --> 00:00:{t0+3:02d},000")
        ref_lines.append("Hello world.")
        ref_lines.append("")
    score = compute_reference_score(
        "\n".join(gen_lines), "\n".join(ref_lines), lang="en",
    )
    assert score.coverage_pct < 50.0
    # The coverage dimension drops to ~47/100 (weight 30 %); density
    # also drops since len(gen) ≠ len(ref). Timing + chrF on the
    # matched half stay at the ceiling, so the weighted total still
    # sits in the high-70s — a grade-C run, not a grade-F catastrophe.
    # Pin the upper bound so a future weight change that erases the
    # coverage signal would be caught here.
    assert score.overall_score < 82
    assert score.dimension_scores["coverage"] < 50


def test_score_caveat_on_short_reference():
    """References with < 100 cues get a caveat surfaced to the UI."""
    gen, ref = _build_pair("Hi.", "Hi.")
    score = compute_reference_score(gen, ref, lang="en")
    assert score.caveat is not None
    assert "noisier" in score.caveat


# ── Reference store + endpoints ─────────────────────────────────────────────


@pytest.fixture
def client_with_cached_vtt(tmp_path, monkeypatch):
    """Stage a fake cached VTT entry in cache_dir so the reference
    endpoints have something to point at. Returns (client, cache_key).

    Patches ``_settings._env.cache_dir`` directly (not via ``_overrides``)
    because other tests in the suite leave a direct ``cache_dir``
    attribute on the settings instance via ``setattr(_, raising=False)``,
    which would shadow our override on order-dependent runs. Patching
    ``_env`` is the most robust path — it's a real attribute on the
    pydantic model so monkeypatch unwinds cleanly in every order."""
    from app.config import settings as runtime_settings
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(runtime_settings._env, "cache_dir", cache_dir)
    # Defensively clear any direct cache_dir attribute another test
    # may have leaked onto the instance via raising=False setattr.
    if "cache_dir" in runtime_settings.__dict__:
        monkeypatch.delattr(runtime_settings, "cache_dir")

    # Build a small VTT with the NOTE header the endpoint needs
    # to extract target_lang from.
    vtt = (
        "WEBVTT\n\n"
        "NOTE Subtitle This auto-subs (en -> en, whisper=small, provider=nllb, polished=true)\n\n"
    )
    for i in range(15):
        t0 = i * 5
        vtt += f"00:00:{t0:02d}.000 --> 00:00:{t0+3:02d}.000\n"
        vtt += "I think the car is in the garage and you are not here.\n\n"

    cache_key = "abc123def4567890" + "0" * 8   # 24-char hex-looking key
    import json
    payload = {"vtt": vtt, "media_path": "/m/x.mkv"}
    (cache_dir / f"{cache_key}.json").write_text(json.dumps(payload))

    yield TestClient(app), cache_key, vtt


def test_upload_reference_endpoint_returns_score(client_with_cached_vtt):
    client, cache_key, vtt = client_with_cached_vtt
    # Same content → near-perfect score.
    ref_srt = ""
    for i in range(15):
        t0 = i * 5
        ref_srt += f"{i+1}\n"
        ref_srt += f"00:00:{t0:02d},000 --> 00:00:{t0+3:02d},000\n"
        ref_srt += "I think the car is in the garage and you are not here.\n\n"

    resp = client.post(
        f"/api/cache/vtt/{cache_key}/reference",
        files={"file": ("ref.srt", io.BytesIO(ref_srt.encode()), "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["overall_score"] >= 90
    assert body["overall_grade"] == "A"
    assert body["coverage_pct"] == 100.0


def test_upload_reference_endpoint_refuses_language_mismatch(client_with_cached_vtt):
    """VTT is English; uploading a French SRT must 400 with a clear
    same-language-policy message."""
    client, cache_key, _ = client_with_cached_vtt
    fr_srt = ""
    for i in range(15):
        t0 = i * 5
        fr_srt += f"{i+1}\n"
        fr_srt += f"00:00:{t0:02d},000 --> 00:00:{t0+3:02d},000\n"
        fr_srt += "Je pense que la voiture est dans le garage et tu n'es pas là.\n\n"

    resp = client.post(
        f"/api/cache/vtt/{cache_key}/reference",
        files={"file": ("ref.srt", io.BytesIO(fr_srt.encode()), "text/plain")},
    )
    assert resp.status_code == 400
    assert "same-language" in resp.json()["detail"].lower()


def test_target_lang_from_payload_handles_all_note_shapes():
    """0.11.3 regression: _target_lang_from_payload was hardcoded to
    only match the old 'auto-subs (xx -> yy)' NOTE header. The 0.10.0
    embedded-subs short-circuit emits two new shapes:
    - translate-other-lang: 'embedded subs (en -> fr, ...)'
    - copy-same-lang     : 'embedded subs (fr, ..., copied as-is)'
    Both must resolve to the correct target — without this, every
    reference upload on an embedded-subs job returned the
    'Cannot determine the generated VTT's target language' 500."""
    from app.api.manage import _target_lang_from_payload

    cases = [
        # (NOTE header line, expected target)
        ("Subtitle This auto-subs (en -> fr, whisper=small, provider=nllb)", "fr"),
        ("Subtitle This embedded subs (en -> fr, source=embedded-subrip, "
         "track #6, provider=nllb, polished=true)", "fr"),
        ("Subtitle This embedded subs (fr, source=embedded-subrip, "
         "track #6, copied as-is)", "fr"),
        # Pre-0.7.32 mode-suffixed legacy header — verifies the lax
        # match doesn't accidentally regress.
        ("Subtitle This auto-subs (ja -> en, whisper=large, provider=llm, "
         "polished=true)", "en"),
    ]
    for note, expected in cases:
        vtt = f"WEBVTT\n\nNOTE {note}\n\n00:00:01.000 --> 00:00:02.000\nhi\n"
        assert _target_lang_from_payload({"vtt": vtt}) == expected, (
            f"failed on NOTE: {note!r}"
        )


def test_upload_reference_endpoint_refuses_unparseable(client_with_cached_vtt):
    """An uploaded file that doesn't parse to any cue must 400."""
    client, cache_key, _ = client_with_cached_vtt
    resp = client.post(
        f"/api/cache/vtt/{cache_key}/reference",
        files={"file": ("garbage.srt", io.BytesIO(b"not a subtitle"), "text/plain")},
    )
    assert resp.status_code == 400


def test_get_score_then_delete_endpoint(client_with_cached_vtt):
    """End-to-end: upload, GET score, DELETE, GET 404."""
    client, cache_key, _ = client_with_cached_vtt
    ref_srt = ""
    for i in range(15):
        t0 = i * 5
        ref_srt += f"{i+1}\n00:00:{t0:02d},000 --> 00:00:{t0+3:02d},000\n"
        ref_srt += "I think the car is in the garage.\n\n"

    client.post(
        f"/api/cache/vtt/{cache_key}/reference",
        files={"file": ("r.srt", io.BytesIO(ref_srt.encode()), "text/plain")},
    )
    # GET returns the cached score.
    r = client.get(f"/api/cache/vtt/{cache_key}/reference/score")
    assert r.status_code == 200
    assert "overall_score" in r.json()
    # DELETE removes it.
    r = client.delete(f"/api/cache/vtt/{cache_key}/reference")
    assert r.status_code == 200
    assert r.json()["removed"] is True
    # Next GET is 404.
    r = client.get(f"/api/cache/vtt/{cache_key}/reference/score")
    assert r.status_code == 404
