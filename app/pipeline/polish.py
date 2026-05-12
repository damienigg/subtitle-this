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


def polish_vtt_text(vtt_text: str) -> str:
    """Re-polish an already-written .vtt without re-running STT or
    translation. Parses the cues from the input text, runs them
    through ``polish_cues``, and re-emits a new .vtt preserving the
    original header note (``NOTE Subtitle This auto-subs (...)``)
    so the metadata trail stays intact across the polish cycle.

    Idempotent in practice: a second pass on an already-polished
    .vtt produces nearly-identical output (already-long cues are
    above the floor, already-merged cues no longer have eligible
    neighbors). Empty inputs or .vtts with no parseable cues
    pass through unchanged."""
    from app.pipeline.vtt import to_webvtt
    cues, header_note = _parse_vtt_to_cues(vtt_text)
    if not cues:
        return vtt_text
    polished = polish_cues(cues)
    return to_webvtt(polished, header_note=header_note)


def _parse_vtt_to_cues(vtt_text: str) -> tuple[list[Cue], str | None]:
    """Parse a .vtt back into the dataclass shape ``polish_cues``
    expects. Returns the cue list plus the original NOTE-header
    payload (the text after "NOTE ", or None if no header was
    present). Multi-line cue text is joined with a single space —
    line wrapping is re-applied by the writer based on current
    ``max_line_chars`` / ``max_lines_per_cue`` settings, which may
    differ from the original."""
    import re
    ts_re = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
    )

    def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    note: str | None = None
    cues: list[Cue] = []
    next_id = 0
    for block in vtt_text.split("\n\n"):
        lines = block.strip().split("\n")
        if not lines:
            continue
        # NOTE blocks come in two shapes: "NOTE\ntext" or "NOTE text".
        # We keep the FIRST NOTE we encounter (the header), strip the
        # "NOTE" / "NOTE " prefix, and surface its body verbatim so the
        # writer re-emits it as ``NOTE <body>``.
        if note is None and lines[0].startswith("NOTE"):
            head = lines[0]
            body_inline = head[4:].lstrip()
            tail = "\n".join(lines[1:]).strip()
            note = (body_inline + ("\n" + tail if tail else "")).strip() or None
            continue
        # Find the timestamp line; identifier lines optionally precede it.
        for i, line in enumerate(lines):
            m = ts_re.match(line.strip())
            if not m:
                continue
            start_s = _to_seconds(*m.group(1, 2, 3, 4))
            end_s = _to_seconds(*m.group(5, 6, 7, 8))
            text = " ".join(lines[i + 1:]).strip()
            if text and end_s > start_s:
                cues.append(Cue(id=next_id, start=start_s, end=end_s, text=text))
                next_id += 1
            break
    return cues, note


def _extend_min_duration(cues: list[Cue]) -> list[Cue]:
    """For each cue, ensure its on-screen duration meets the higher
    of two minima:
      - settings.min_cue_duration_seconds (absolute floor)
      - len(text) * settings.min_seconds_per_char (reading-speed floor)
    Extends ``end`` forward only — start stays aligned with the
    audio onset. Capped by the next cue's start so two cues never
    overlap.

    Idempotency guard (0.7.19): when merge is enabled, the cap is
    NOT just ``next.start - cue_separation_seconds``. It's the
    stricter ``next.start - max_gap_to_merge_seconds - epsilon``.
    Without this, a cue extended right up to the next one would
    sit at a gap of ``cue_separation`` (default 0.05 s), which is
    below ``max_gap_to_merge`` (default 0.3 s) — meaning a second
    polish pass would see two cues that the FIRST pass deliberately
    chose NOT to merge (because their original gap was too big)
    AND would now merge them on the second pass.

    The extra gap kept here is the price of idempotency: a cue
    might extend to e.g. 10.7 s instead of 10.95 s when the next
    cue starts at 11.0 s. That preserves the "these are two
    separate utterances, not a merged one" decision through any
    number of re-polish passes.

    When merge is disabled, the conventional ``cue_separation``
    cap applies — there's no merge-decision to preserve.
    """
    min_dur = float(settings.min_cue_duration_seconds)
    sec_per_char = float(settings.min_seconds_per_char)
    sep = float(settings.cue_separation_seconds)
    max_gap = float(settings.max_gap_to_merge_seconds)
    merge_enabled = bool(settings.merge_adjacent_cues)
    # 1 ms float-arithmetic safety margin. The merge predicate is
    # strict-less-than, so equality at the boundary is already safe;
    # the epsilon protects against 10.7 + 0.3 not being exactly 11.0
    # in binary floating-point.
    EPSILON = 0.001

    n = len(cues)
    for i, cue in enumerate(cues):
        current = cue.end - cue.start
        desired = max(min_dur, len(cue.text) * sec_per_char)
        if current >= desired:
            continue
        new_end = cue.start + desired
        if i + 1 < n:
            next_start = cues[i + 1].start
            cap_no_overlap = next_start - sep
            if merge_enabled:
                cap_idempotent = next_start - max_gap - EPSILON
                cap = min(cap_no_overlap, cap_idempotent)
            else:
                cap = cap_no_overlap
            new_end = min(new_end, cap)
        if new_end > cue.end:
            cue.end = new_end
    return cues
