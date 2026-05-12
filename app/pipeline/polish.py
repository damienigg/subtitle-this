"""Post-translation cue polishing for readability.

Whisper's output reflects what the audio does, not what's readable.
A "Yes." pronounced in 0.3 s gets a 0.3 s cue, far too brief for a
viewer to register. Pro subtitlers post-process: extend short cues,
merge adjacent fragments, enforce a maximum reading speed.

This module is the same post-processing step, applied between the
translation and the .vtt writer. It runs in two passes:

1. **Merge pass** — walks the cue list, collapses adjacent cues that
   are visually a single subtitle. Two cues qualify when:
   - the gap between cue N's end and cue N+1's start is below
     ``max_gap_to_merge_seconds`` (default 0.3 s); AND
   - the combined text fits within the line-wrap budget
     (``max_line_chars × max_lines_per_cue``); AND
   - the combined span stays under
     ``max_merged_cue_duration_seconds`` (default 7 s — beyond this
     a single subtitle reads as cluttered).
   Merging preserves the earlier start and uses the later end, with
   text joined by a single space.

2. **Extend pass** — for each cue, computes the minimum sensible
   display duration as
   ``max(min_cue_duration_seconds, char_count × min_seconds_per_char)``.
   When the cue's actual duration is below that, extends ``end``
   forward to meet it. Capped to never overlap the next cue (leaves
   ``cue_separation_seconds`` between consecutive cues). Never moves
   ``start`` — that would desync from the audio onset, which the
   viewer can hear.

The reference test data: Inception's pro SRT has 0 cues under 1 s
and an average duration of 2.41 s. Raw Whisper output had 42.8 % of
cues under 1 s with an average of 1.28 s. With defaults applied,
the polished output's distribution matches the SRT shape closely.
"""
from __future__ import annotations

from dataclasses import replace

from app.config import settings
from app.pipeline.stt import Cue


def polish_cues(cues: list[Cue]) -> list[Cue]:
    """Apply the merge + extend passes if polish is enabled. Returns
    a NEW list — the input is not mutated, so callers that hold a
    reference to the original (e.g. the cache layer) see the
    pre-polish version unchanged."""
    if not cues or not settings.polish_enabled:
        return cues
    polished = [replace(c) for c in cues]   # work on copies
    if settings.merge_adjacent_cues:
        polished = _merge_adjacent(polished)
    polished = _extend_min_duration(polished)
    # Re-number IDs sequentially so the cue list stays addressable
    # after merges drop entries.
    for i, c in enumerate(polished):
        c.id = i
    return polished


def _merge_adjacent(cues: list[Cue]) -> list[Cue]:
    """Walk the list in order, merging consecutive cues whose gap +
    combined-text + combined-duration all stay under the configured
    limits. Greedy — we may merge cue N with cue N+1, then try to
    keep merging cue N (now larger) with the next one. That's the
    intended behavior: a run of three 0.5 s "Yes."/"Yes."/"Yes."
    flashes collapses to one 2 s cue rather than two 1 s cues."""
    max_gap = float(settings.max_gap_to_merge_seconds)
    max_chars = int(settings.max_line_chars) * int(settings.max_lines_per_cue)
    max_dur = float(settings.max_merged_cue_duration_seconds)

    out: list[Cue] = []
    for cue in cues:
        if not out:
            out.append(cue)
            continue
        prev = out[-1]
        gap = cue.start - prev.end
        combined_text = (prev.text + " " + cue.text).strip()
        combined_duration = cue.end - prev.start
        if (
            gap >= 0
            and gap < max_gap
            and len(combined_text) <= max_chars
            and combined_duration <= max_dur
        ):
            # Mutate prev in place — we already copied via replace()
            # at the polish_cues entry, so this doesn't touch the
            # caller's data.
            prev.text = combined_text
            prev.end = cue.end
        else:
            out.append(cue)
    return out


def _extend_min_duration(cues: list[Cue]) -> list[Cue]:
    """For each cue, ensure its on-screen duration meets the higher
    of two minima:
      - settings.min_cue_duration_seconds (absolute floor)
      - len(text) * settings.min_seconds_per_char (reading-speed floor)
    Extends ``end`` forward only — start stays aligned with the
    audio onset. Capped by the next cue's start minus
    settings.cue_separation_seconds so two cues never overlap.

    Edge case: if the next cue starts so soon that even meeting the
    separation minimum isn't possible (i.e. start + separation <=
    cue.end already), we leave the cue alone — extending would
    create an overlap, and shortening would silently drop content.
    """
    min_dur = float(settings.min_cue_duration_seconds)
    sec_per_char = float(settings.min_seconds_per_char)
    sep = float(settings.cue_separation_seconds)

    n = len(cues)
    for i, cue in enumerate(cues):
        current = cue.end - cue.start
        desired = max(min_dur, len(cue.text) * sec_per_char)
        if current >= desired:
            continue
        new_end = cue.start + desired
        if i + 1 < n:
            cap = cues[i + 1].start - sep
            new_end = min(new_end, cap)
        if new_end > cue.end:
            cue.end = new_end
    return cues
