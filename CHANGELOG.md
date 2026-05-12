# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
follows [Semantic Versioning](https://semver.org/) — though as a 0.x release
expect breaking changes between minor versions until 1.0.

## [Unreleased]

## [0.7.21] — 2026-05-12

### Changed

- **Cache Stats histogram tables — readable column widths and
  consistent layout.** The three histograms (cue duration
  distribution, cues per 10-min coverage bucket, VAD region
  duration) now share the same 4-column shape:
  ``Bucket | bar | Count | Share %``.
  - Count column widened from 3rem to 4.5rem so 4-digit values
    (e.g. Inception's 1746 cues per bucket) no longer wrap or
    truncate.
  - New ``.bar-pct`` class — fixed 4rem, right-aligned,
    tabular-nums — so percent columns line up across the three
    tables instead of floating ad-hoc.
  - **Coverage** and **VAD region** histograms now show a
    ``Share %`` column too. Previously only the duration
    histogram did, which made the other two harder to read
    ("100 cues" means nothing without "out of how many").
  - Each table now has a small uppercase column header
    (``Duration / Cues / Share``) so the meaning of each
    number is explicit, not inferred from position.
  - Row hover lights up the bar so you can scan a long
    histogram without losing your eye on the row.

  Pure presentation change — no metric values were added or
  removed, only how they're laid out.

### Added

- **Polish marker in the .vtt NOTE header.** Every .vtt that went
  through the readability polish pass now carries a
  ``polished=true`` field in the
  ``NOTE Subtitle This auto-subs (...)`` line. A reader can tell
  at a glance whether the file is post-polish or raw Whisper
  output, without having to compare cue distributions.

  Example:
  ```
  NOTE Subtitle This auto-subs (en -> fr, mode=audio,
       whisper=large-v3-turbo, provider=nllb, polished=true)
  ```

- New ``polished: bool | None`` field on ``VttStats`` (stats
  record) and ``VttEntry`` (Cache Explorer row). Three states:
  True (marker present + true), False (marker present + false —
  polish was explicitly disabled at write time), None (no marker
  — pre-0.7.20 entries).
- **UI surfacing**:
  - New "Polish" column in the Cache Explorer table with a pill
    per row: ✨ polished (green) / raw (amber) / ? unknown (muted).
    Hovering each variant explains what it means and how to
    re-polish if needed.
  - Cache Stats page's Pipeline section now shows a "Readability
    polish" row reflecting the same three-state marker.

### Changed

- ``polish_vtt_text`` now stamps ``polished=true`` on the
  re-emitted NOTE header. Idempotent — re-polishing an already-
  marked file leaves the marker in place rather than duplicating
  it.
- The NOTE-header regex in ``cache_explorer.py``, ``stats.py``,
  and ``api/manage.py`` now accepts an optional
  ``(?:, polished=(true|false))?`` group before the closing
  parenthesis. The ``provider`` field's match is tightened from
  ``[^)]+`` to ``[^,)]+`` so it doesn't greedily swallow the
  new marker.

### Tests

- 4 new tests covering the marker round-trip:
  - polish_vtt_text stamps the marker once and only once
    (idempotent on re-polish);
  - all other NOTE fields survive the stamp;
  - stats.polished captures True when the marker is present;
  - stats.polished stays None when no marker is present
    (legacy compatibility).

## [0.7.19] — 2026-05-12

### Fixed

- **Polish was not idempotent under re-polish.** The extend pass
  capped each cue's end at ``next.start - cue_separation_seconds``
  (0.05 s), which left a 50 ms gap to the next cue. The merge
  predicate uses ``gap < max_gap_to_merge_seconds`` (0.3 s by
  default), so the FIRST polish would correctly not-merge two
  cues that started 0.7 s apart; but the new 50 ms post-extend
  gap was below the merge threshold, so the SECOND polish would
  merge them anyway. The result drifted toward more-merged
  output over multiple re-polish clicks.

  Fix: when merge is enabled, the extend cap is now
  ``next.start - max_gap_to_merge_seconds - epsilon`` instead of
  ``next.start - cue_separation_seconds``. This preserves the
  first pass's no-merge decision through any number of
  re-polish passes. Cost: cues that previously extended right up
  to the next one (50 ms gap) now leave a 300 ms gap when
  defaults are in effect. The readability gain is preserved
  (short cues still get the full reading-speed extension where
  possible); only the cap shifts.

  When merge is disabled (``merge_adjacent_cues=false``), the
  conventional ``cue_separation_seconds`` cap applies — there's
  no merge decision to preserve.

### Tests

- 4 new tests in `tests/test_polish.py`:
  - extend leaves at least max_gap_to_merge between
    non-mergeable neighbors;
  - polish is idempotent on the canonical drift scenario
    (two cues just out of merge range);
  - three passes converge to pass-1's output (rules out
    "converges in N>1 passes" bugs);
  - idempotency holds when merge is disabled too.
  All 4 verified to fail on pre-fix code with the exact
  diagnostic ("Polish is not idempotent: pass 1 → 2 cues,
  pass 2 → 1 cues").

## [0.7.18] — 2026-05-12

### Added

- **Re-polish from Cache Explorer** — the readability polish pass
  introduced in 0.7.17 can now be re-applied to any cached .vtt
  without re-running STT or translation. New "✨" button per row
  in the Cache Explorer triggers the rewrite; both the cached
  payload AND the .vtt next to the media (when locatable from the
  payload's media_path + the NOTE header) are updated in place.
  Idempotent — running it twice is a near-no-op since already-
  polished cues are above the duration floor and adjacent
  candidates have already been merged.
- New helper `polish_vtt_text(vtt_text) -> str` in
  `app/pipeline/polish.py`: parses a .vtt back into cue dataclasses
  (handling NOTE-header capture so provenance survives the round
  trip), runs ``polish_cues``, re-emits with the original header
  preserved.
- New API endpoint ``POST /api/cache/vtt/{cache_key}/repolish``:
  rewrites the cached payload atomically (.tmp + os.replace),
  best-effort-writes the .vtt next to the media, and returns
  before/after cue counts plus a ``disk_vtt_updated`` flag the
  UI surfaces.

### Tests

- 5 new tests in `tests/test_polish.py` covering the round-trip:
  extend-in-place via .vtt text, NOTE-header preservation, merge
  through the text path, near-idempotency, and the empty-input
  passthrough.
- 1 smoke test asserts the HTTP endpoint rewrites the cached
  .vtt and returns the cue-count delta.

## [0.7.17] — 2026-05-12

Readability polish — addresses the user complaint that generated
subtitles "flash by too fast to read". The Inception comparison
showed 42.8 % of cues under 1 second in the generated .vtt vs
0 % in the pro reference SRT; this release closes that gap.

### Added

- New module `app/pipeline/polish.py` with a two-pass cue
  post-processor that runs between translation and the .vtt writer:
  - **Extend pass**: every cue gets a minimum display duration
    equal to ``max(min_cue_duration_seconds, char_count ×
    min_seconds_per_char)``. Cues below the floor are extended
    forward (``end`` moves; ``start`` never does — preserves
    audio-onset sync). Capped to leave ``cue_separation_seconds``
    between consecutive cues so two subtitles never overlap.
  - **Merge pass**: adjacent cues with a tight gap and combined
    text that fits the line-wrap budget collapse into one. The
    flickery "Yes." / "Yes." / "Yes." back-and-forth sequences
    Whisper produces on quick dialog get smoothed into one
    readable subtitle.
- 7 new settings in the Subtitles section:
  ``polish_enabled`` (master, default ON), ``min_cue_duration_seconds``
  (1.2), ``min_seconds_per_char`` (0.045 ≈ 22 chars/sec),
  ``merge_adjacent_cues`` (default ON), ``max_gap_to_merge_seconds``
  (0.3), ``max_merged_cue_duration_seconds`` (7.0),
  ``cue_separation_seconds`` (0.05). Each carries an inline help
  text explaining the trade-off and a ``show_if`` gate so
  disabling the master switch collapses the sub-knobs.

### Tests

- 15 new tests in `tests/test_polish.py` covering each invariant:
  short cues extend to the floor, long cues pass through, the
  cap-by-next-cue never produces overlaps, char-based reading
  speed wins over the absolute floor when text is long, merge
  respects the gap / chars / duration ceilings, three-cue
  chained merge collapses correctly, ids re-sequence after
  merge.
- `test_transcript_cache_hit_skips_audio_extract_and_whisper`
  updated: seeded cues now have a 5 s gap so the polish pass
  doesn't merge them and the cache-hit count assertion still
  holds.

## [0.7.16] — 2026-05-12

App-update awareness in the dashboard. The app connects to its own
GitHub releases API and tells you whether you're running the
latest version. Optional one-click update execution behind an
opt-in env var.

### Added

- New module `app/updates.py` with two surfaces:
  - `check_for_update()` queries
    `https://api.github.com/repos/damienigg/subtitle-this/releases/latest`,
    compares the tag to `app.__version__`, and returns a structured
    `UpdateStatus`. Cached for 1 hour to stay under GitHub's
    60 req/hr unauthenticated rate limit. Network/API errors
    surface as `error` on the result rather than raising.
  - `run_update_command()` runs whatever the operator stashed in
    the new `BABEL_UPDATE_COMMAND` env var (e.g.
    `cd /mnt/.../subtitle-this && git pull && docker compose build
    && docker compose up -d`). Returns the command's combined
    stdout/stderr + return code. 15-minute hard ceiling. Empty
    env var = button hidden = no execution.
- New API endpoints:
  - `GET /api/update/check?force=0|1` — returns the status JSON
  - `POST /api/update/run` — executes the configured command, or
    HTTP 412 when none is set
- New **update banner** at the top of the Dashboard. Color-graded
  by status: green = up to date, amber = update available,
  muted = couldn't check. Always shows a "Check now" button that
  forces a fresh GitHub query. When `BABEL_UPDATE_COMMAND` is
  set AND an update is available, an additional "Update now"
  button executes the command and streams the output into a
  `<details>` block on the same card.

### Configuration

- New env var `BABEL_UPDATE_COMMAND` (defaults to empty). Set to
  any shell command you want triggered by the "Update now"
  button. The command is operator-controlled by definition (env
  var, not Settings UI), so there's no user-input injection path.
  Self-update from inside a container still requires the
  container to have the privileges its command needs — typically
  a mounted docker socket and the docker CLI present. For most
  setups, the safer-and-simpler alternative is to set up
  `containrrr/watchtower` as a sibling service; the in-app
  button is convenient for build-from-source setups where
  watchtower wouldn't help.

### Tests

- 15 tests in `tests/test_updates.py` covering version parsing
  (with v-prefix and pre-release suffix), comparison, the
  cached/forced GitHub fetch paths, error-graceful surfacing
  (404, network), release-notes truncation, and the
  enabled/disabled gating of the executor.
- 2 smoke tests on the new API endpoints.

## [0.7.15] — 2026-05-12

### Fixed

- **Job stats page score mismatch for legacy jobs.** 0.7.13
  fixed the mismatch for NEW jobs by storing `pipeline_metrics`
  on the Job, but jobs that ran in 0.7.8-0.7.12 had no such
  field on disk — clicking their pill still showed a different
  (inflated) score. New fallback in `/jobs/{id}/stats`: when
  `j.pipeline_metrics is None`, walk `cache_dir/*.json` and
  match by media basename derived from the job's
  `output_path`. First match's payload wins; its
  `pipeline_metrics` is threaded into `compute_from_vtt` exactly
  like the in-job field would have been.

### Changed

- **Quality column split into two pills.** The runner's
  "82 · B" pill is now two adjacent pills wrapped in a shared
  anchor — score on the left, letter grade on the right, both
  color-graded the same. Mirrors the STT / Translation columns'
  family + variant layout. Single click target, same destination
  (`/jobs/{id}/stats`).

### Tests

- New `test_job_stats_page_recovers_pipeline_metrics_from_cache_for_legacy_job`
  builds a Job in the pre-0.7.13 shape (quality_score set,
  pipeline_metrics None), drops a matching cache payload with
  a pad-drop signal, and asserts the stats page surfaces the
  "Region-packing unrecoverable drops" factor — proving the
  cache lookup recovered the metrics.

## [0.7.14] — 2026-05-12

### Fixed

- **Jobs table Progress column was visually centered** rather than
  left-aligned like the other columns. `.progress-label.muted`
  inherited `display: flex; justify-content: center` from the base
  `.progress-label` rule (designed for the active-job overlay,
  where centering across the 11rem progress bar is correct) and
  the override didn't reset the layout — only the position. The
  muted variant now resets to `display: inline-block` so the
  "queued / succeeded / canceled" text starts at the left edge
  of the cell.

### Changed

- **Translation pill split into two pills**, matching the
  dashboard's Translation card. For NLLB, "nllb ·
  nllb-200-distilled-1.3B" (the model id duplicated the "nllb"
  family name and carried the noisy `facebook/nllb-200-` prefix)
  now reads as two pills: `NLLB-200` + `distilled-1.3B`. LLM
  provider gets the same two-pill split (`llm` + model name).
  DeepL renders the provider alone since it has no per-model
  dimension.

## [0.7.13] — 2026-05-12

### Fixed

- **Job stats page score mismatch.** The Quality pill in the Jobs
  table showed e.g. 82/B (computed in the runner from the full
  pipeline_metrics) but clicking it took the user to a page
  rendering 92/A — `/jobs/{id}/stats` was recomputing the score
  from the .vtt alone, with no knowledge of the VAD / packing /
  translation telemetry the runner had seen. Both surfaces now
  use the same `pipeline_metrics` input (stored on the Job in
  the runner, threaded through to `compute_from_vtt` in the
  per-job stats route).

### Added

- New `Job.pipeline_metrics` field, snapshotted at the same time
  as `quality_score` / `quality_grade`. Tolerantly deserialized
  by `jobs_store` so pre-0.7.13 on-disk job records stay
  loadable.
- Regression test in `tests/test_smoke_api.py`:
  `test_job_stats_page_uses_stored_pipeline_metrics` constructs
  a Job with a heavy pad-drop signal in pipeline_metrics and
  asserts the resulting stats page surfaces the
  "Region-packing unrecoverable drops" factor. Verified to fail
  on the pre-fix code with the exact "factor list missing" message.

## [0.7.12] — 2026-05-12

Distribution-readiness pass: onboarding wizard, settings migration
hardening, README freshening.

### Added

- **Onboarding wizard** at `/onboarding`. First-run users (no
  `media_server_url` configured) are auto-redirected here from the
  Dashboard instead of facing the full 40+ field Settings form.
  Three guided steps: pick server type + URL + API key (with a
  "Test connection" button that uses `/api/server/health`), pick
  default language / mode / translator, click "Save & open Library".
  Power users can opt out with the "Skip wizard — I'll configure
  manually" link in the wizard's header (adds `?skip_wizard=1` to
  the dashboard URL to bypass the redirect).

### Changed

- **Settings migration framework** hardened. The four legacy
  rename migrations (`claude → llm`, `llm_backend → translation_llm_*`,
  shared `anthropic_api_key`, `emby_url → media_server_url`) were
  previously re-run on every startup and the result was never
  persisted. Now: (a) data is serialized before/after migration
  and the on-disk file is rewritten only when the data actually
  changed, (b) a `_schema_version` provenance tag is stamped with
  the current app version on every migration write-back, (c) an
  INFO log line records every schema-version advance so operators
  see what happened at container start.
- New cleanup migration `_drop_unknown_keys` removes residue from
  past renames (or any other settings.json key that's not in the
  current pydantic model). settings.json self-heals on upgrade.

### Documentation

- README freshened to reflect the post-0.6 work: new "What you
  get" feature list section (Quality Score, Cache Explorer,
  observability, migration framework), new "Quality
  observability" section explaining how to read the stats and
  why "completed ≠ correct".

### Tests

- 7 new tests in `tests/test_settings_migration.py` covering the
  unknown-keys cleanup, the schema_version stamping on first run,
  write-back after data-modifying migrations, no-writeback when
  already current, the migration log line, and that legacy
  rename migrations still apply.
- 5 new smoke tests covering the dashboard redirect, the
  `?skip_wizard=1` bypass, the rendered wizard page, and the
  wizard POST → /library redirect with verified settings update.

## [0.7.11] — 2026-05-12

The Inception fix: pad-zone snap recovery + region-packing density
cap. Together these turn the historical "44 % of cues silently
dropped" pathology into "~0 % dropped, ~5-15 % timing-shifted by
&lt; 0.5 s (invisible)".

### Changed

- `remap_cue_to_original` returns a 3-tuple `(start, end, was_snapped)`
  instead of `(start, end) | None`. **When a cue's timestamp falls in
  a silence pad** between packed regions, the function now snaps it
  to the closest region's start instead of returning None. The cue's
  TEXT is preserved with a time shift bounded by the pad width (0.5 s),
  which is well below the audio-subtitle sync perceptual threshold
  (~1 s). None is now reserved for the genuinely unmappable case
  (empty region_map, or a zero-duration cue).
- `plan_packed_windows` takes a new `max_regions_per_window` parameter.
  Default 4 (the Settings UI default); 0 = legacy unlimited.
- Quality-score factor "Region-packing pad-drops" renamed to
  "Region-packing unrecoverable drops" — the old name conflated the
  recoverable and unrecoverable cases that this release separates.
  A new soft factor "Heavy snap-recovery usage" (info-only, ≤ 5
  points) surfaces when &gt; 15 % of cues required snap recovery —
  the run produced usable subtitles but the density cap could be
  tightened for cleaner timing next time.

### Added

- New setting `stt_max_regions_per_window` (default 4, range 0-20).
  Hard cap on how many short speech regions get bundled into one
  30 s Whisper pass. Lower = better timestamp accuracy from Whisper-
  turbo, slower transcription. On the Inception baseline (12.4
  regions/window avg) lowering to 4 cuts pad overhead from 20 %
  to ~5 % of each window's audio time.
- New `PackingMetrics.cue_snap_pad_zone_count` — cues rescued via
  snap, shown on the stats page as "Cues recovered (snap)" with
  inline explanation. The old "Cues dropped — pad zone" row is
  renamed "Cues dropped — degenerate" since with snap recovery
  in place, the residual drops are mostly real hallucinations on
  pad slices.
- Settings page rewrites the `stt_region_packing` help text in
  plain language with concrete runtime numbers: ON = "~10 min on
  iGPU for a 2 h film", OFF = "~1.5-2 hours for the same film",
  with a note that 0.7.11's density cap + snap recovery make ON
  the right default for almost everyone.

### Tests

- 4 new tests in `tests/test_packing.py`: snap to nearest region's
  start (two directions — closer to previous-end vs closer to
  next-start), empty-region-map fallback, zero-duration-in-pad
  drop, and the density cap (verified at cap=3 producing 4
  windows for 12 input regions, and cap=0 reproducing unlimited
  legacy behavior).
- All existing tests using the 2-tuple shape updated to unpack
  the new 3-tuple with `was_snapped`.

## [0.7.10] — 2026-05-12

UI polish out of the feedback loop.

### Changed

- **Dashboard cards** now have asymmetric widths. The
  Speech-to-Text card carries up to three pills (backend / model /
  device) and used to wrap onto a second line — gets
  `grid-column: span 2` so it gets twice the width of the
  Media server card (which only has 2 short pills). On narrow
  viewports (< 600 px) the span collapses back to 1 to avoid an
  awkward solo full-width row.
- **Settings — Cost ladder hero card removed**. The standalone
  "Cost ladder — settings sorted from always free to configurable
  cost" block at the top of `/settings` was duplicative noise for
  power users: the same cost/quality framing already appears in
  the per-section descriptions ("ALWAYS FREE", "Provider is the
  main cost/quality lever") and in the dropdown badges
  (`[FREE · LOCAL]`, `[FREE TIER 500k chars/mo · CLOUD beyond]`,
  `[VARIES]`). New users get the guidance contextually where
  they make the choice; returning users no longer scroll past a
  wall of intro text every visit. The associated `.hero-cost-ladder`
  CSS is dropped.

## [0.7.9] — 2026-05-12

Three feedback-driven UI polish items.

### Changed

- **Cache Explorer** dedupes the two-level cache pairs. Each VTT
  entry is written to disk under both the quick-fingerprint key
  AND the content-fingerprint key (so a path/mtime change doesn't
  miss the cache), which previously produced two identical rows
  per film. The listing now groups them by
  `(media_path, source_lang, target_lang, mode, provider, whisper_model)`
  and shows one row per logical record with a `(×2)` annotation
  on the cache_key column. Delete sends one request per
  underlying key so the pair vanishes together.
- **Translation pill** in the Jobs table now includes the model
  name: `nllb · distilled-1.3B` instead of just `nllb`, `llm ·
  claude-opus-4-7` instead of `llm`. HuggingFace IDs are
  short-formed (last path segment) so the pill stays compact. DeepL
  has no per-model dimension, renders as `deepl` alone.

### Added

- New **Quality** column in the Jobs table — a per-run grade pill
  ("78 · B"), color-coded by letter grade. The pill links to a
  per-job stats page (`/jobs/{id}/stats`) which renders the full
  quality breakdown straight from the job's `output_path` (no
  cache_key lookup needed). New backend fields
  `Job.quality_score` / `Job.quality_grade` set in the runner
  after the .vtt is written; legacy jobs and still-running ones
  render a muted dash.
- New `Job.translation_model` field — snapshotted at submission
  from `settings.nllb_model` / `settings.translation_llm_model`
  / empty for DeepL.

### Tests

- 2 new tests in `tests/test_cache_explorer.py`: dedup collapses
  quick-fp + content-fp pairs into one row; two distinct films
  with the same NOTE header still get their own rows.

## [0.7.8] — 2026-05-12

Jobs table rework + Quality Score on the stats page.

### Added

- **Quality Score** (new module `app/quality.py`). 0-100 composite
  derived from the stats record + pipeline metrics, mapped to a
  5-star rating and an A-F letter grade. Each penalty is tied to
  a specific known pathology with its own warn-vs-critical
  threshold:
  - Compressed timestamps (very_short_pct &gt; 15/25 %)
  - Region-packing pad-drops (drop_pct &gt; 5/10/20 %)
  - VAD under-detection (speech_ratio &lt; 30/20 %)
  - VAD trimming short words (short_region_pct &gt; 25/40 %)
  - Whisper hallucinations (degen_drops/100 cues &gt; 5/20)
  - Empty translations (empty_pct &gt; 5/10 %)
  - Duplicate translations (dup_pct &gt; 15/30 %)
  - Cue count mismatch (in ≠ out by more than 5 %)
  The factor breakdown is rendered as a table on the stats page so
  the user sees WHICH pathology cost which points — actionable
  rather than just diagnostic.
- Quality card rendered at the top of the stats page with a big
  score, 5 stars, letter grade, color-coded left border, and the
  per-factor table below.
- New **STT** column in the Jobs table — shows the Whisper model
  that was active when the job was submitted. New
  `Job.whisper_model` field, snapshotted at submission so the
  table stays accurate even if the user changes the setting
  between submission and completion.
- **Output cell** is now a clickable pill (▸ vtt) that opens the
  .vtt in a new browser tab via the new
  ``GET /api/jobs/{job_id}/output.vtt`` endpoint. Defends against
  arbitrary-path requests by only serving the path the runner
  recorded as the job's own output. No more SSH-and-cat.

### Changed

- Jobs table column **Provider** renamed to **Translation** —
  STT is now its own column so labeling needs to disambiguate.

### Tests

- 17 new tests in `tests/test_quality.py` covering every penalty
  threshold (each at its trigger value + one below), the
  multiple-pathology compounding case (Inception profile),
  zero-clamp, grade-band boundaries, and the JSON serializer.

## [0.7.7] — 2026-05-12

Three operator-driven additions out of the post-mortem feedback
loop: relocate the stats sidecar, add a download button for it,
and instrument the translation phase that was missing from 0.7.6's
telemetry.

### Changed

- **Stats sidecar location**: moved from `{vtt_path}.stats.json`
  (next to the .vtt in the user's movie folder) to
  `cache_dir/stats/{cache_key}.json` (inside the cache). Movie
  folders stay clean. The legacy `write_sidecar()` function is
  kept as a deprecated alias for the tests that exercise it; the
  hot path now uses `write_cache_sidecar()`.
- `delete_vtt_entry` in the cache_explorer now also removes the
  paired stats sidecar, so a row's two files vanish together.

### Added

- **Translation telemetry** — the 4th instrumented phase. New
  `TranslationMetrics` dataclass in `pipeline_metrics.py`:
  provider, model id, wall-clock duration, input/output cue
  counts (mismatch flagged), input/output total characters,
  char_ratio (en→fr expected 1.10-1.25; way off → content
  dropped), `empty_output_count` (NLLB int8 quantization
  degenerate signature; > 5 % triggers a warn), and
  `duplicate_output_count` (model-collapse signature; > 15 %
  triggers a warn). Computed in `processor.process()` from
  outside the provider so the same code works for NLLB / DeepL
  / LLM with no per-provider hooks.
- New "Translation" section on the Cache Explorer's stats page
  with inline thresholds matching the warn classes above.
- "💾" download button per row in the Cache Explorer. Fetches the
  stats JSON via the existing API endpoint and triggers a Blob
  download with a media-name-derived filename
  (`Inception (2010).BluRay.stats.json`, not the hash) so the
  file is recognizable when the user copies it off-NAS.

### Tests

- 4 new tests covering `compute_translation_metrics` (char ratio,
  empty counting, duplicate-group counting, empty-input
  safety).
- 2 new tests covering the sidecar relocation (lives inside
  cache_dir/stats/, paired-delete works).

## [0.7.6] — 2026-05-12

Per-run pipeline telemetry: the stats sidecar (and the Cache
Explorer's stats page) now carry enough evidence to identify with
confidence which of the four candidate causes is dropping cues —
VAD too strict, region-packing pad-drop, Whisper compressed
timestamps, or Whisper hallucinations.

### Added

- New module `app/pipeline_metrics.py` with three aggregators that
  the OpenVINO STT loop populates as it runs:
  - **VadAggregator**: total audio analyzed, total speech detected
    by Silero, speech ratio (low % → VAD too strict for the mix),
    region count, region duration histogram (lt_0_25s, 0_25_to_0_5s,
    0_5_to_1s, 1_to_3s, 3_to_10s, gte_10s — the 0.25-0.5 s bucket
    flags barely-passed regions), average/median region duration,
    short_region_pct (share &lt; 0.5 s).
  - **PackingAggregator**: total Whisper windows, single-region vs
    packed counts, avg regions/window, **cue_drop_pad_zone_count**
    (cues silently dropped because Whisper-predicted timestamps fell
    in a packed window's silence-pad zone — the direct evidence for
    pathology #2), cue_keep_count.
  - **WhisperAggregator**: count of cues with degenerate timestamps
    (end ≤ start) dropped by `_parse_segments`. Spike here
    corroborates pathology #3 (turbo-on-packed compressed
    timestamps).
- `_parse_segments` accepts an `on_drop` callback so the inner loop
  feeds the whisper aggregator without changing the parser's return
  shape (existing tests untouched).
- `TranscriptionResult`, `ProcessResult`, the VTT cache payload,
  the transcript cache payload, and the .stats.json sidecar all
  carry `pipeline_metrics` through end-to-end. A cache hit
  preserves the original-run telemetry.
- Cache Stats page gains three new sections (VAD, Region packing,
  Whisper) with inline thresholds telling the user which numbers
  are healthy vs. concerning (speech ratio &lt; 25 % → warn,
  pad-drop share &gt; 10 % → warn, short_region_pct &gt; 25 % →
  warn). Entries from pre-0.7.6 runs gracefully degrade to a "no
  telemetry available — re-process to capture" note.

### Tests

- 11 new tests in `tests/test_pipeline_metrics.py` covering each
  aggregator's math (sum, average, median, histogram bin
  classification, edge-zero handling, enabled-flag carry-through,
  serialization-with-None semantics).
- Existing segment-offset regression test extended to assert that
  `pipeline_metrics` are populated and that single-region windows
  produce zero pad-drops.

## [0.7.5] — 2026-05-12

Objective quality / coverage metrics per completed conversion —
the same dimensions surfaced in the Inception 0.7.1 post-mortem
(cue count, duration histogram, per-10-min coverage buckets,
character density, speech-display ratio), now produced
automatically for every run.

### Added

- New module `app/stats.py` computing the full stats record
  from a finished .vtt. All metrics derive from the .vtt content
  alone — no media probe — so they're cheap to recompute on
  demand for any cached entry.
- Sidecar `<vtt_path>.stats.json` is written next to the .vtt at
  job completion (atomic via tmp + os.replace, best-effort —
  a metrics write failure cannot block a job's actual completion).
  Means copying a .vtt off the NAS brings its quality numbers
  with it.
- New page `GET /cache/vtt/{cache_key}/stats` rendering the same
  record with horizontal bar charts (duration distribution +
  per-10-min coverage) and inline annotations explaining what
  shapes flag pathologies (>15 % very-short cues = compressed-
  timestamp regression; a single bucket at zero between populated
  ones = VAD rejected a scene).
- API endpoint `GET /api/cache/vtt/{cache_key}/stats` returning
  the JSON record — same payload as the `.stats.json` sidecar.
- "📊" button on every Cache Explorer row, linking to the stats
  page for that entry.

### Tests

- 11 unit tests in `tests/test_stats.py` covering cue parsing
  (timestamps + multi-line text + NOTE header handling), duration
  bucket classification at band edges, the very_short_pct
  pathology metric, coverage-bucket spanning, NOTE-header
  metadata parsing + override precedence, atomic sidecar write,
  and the no-raise-on-OSError contract.
- 3 smoke tests in `tests/test_smoke_api.py` for the API and the
  page render. Also added a `_redirect_cache_dir` helper that
  strips any stale `cache_dir` instance attribute a prior test
  may have left behind (legacy monkeypatch-via-setattr pattern
  in `test_perf_hardening` was shadowing `_overrides`).

## [0.7.4] — 2026-05-12

New **Cache Explorer** page so re-runs no longer require SSH-ing
into the host to find the right hashed cache filename.

### Added

- `GET /cache` page (new nav tab "Cache") with two sections:
  **VTT cache** (top-level `cache_dir/*.json`) and
  **Transcript cache** (`cache_dir/transcripts/*.json`). Each row
  shows the film name, language pair, mode, provider, Whisper
  model, cue count, size, and a relative "modified" timestamp.
  Per-row delete buttons and per-section "Clear all" buttons. The
  page excludes model weights (`openvino-models/`, `nllb-models/`,
  `hf/`) and runtime state (`settings.json`, `jobs.json`) — those
  aren't subtitle artefacts and shouldn't be one-click-deletable.
- New module `app/cache_explorer.py` with list / delete helpers,
  parsing the .vtt `NOTE` header line to surface lang / mode /
  provider / whisper for legacy entries that pre-date the
  payload-side `media_path` field.
- `media_path` is now stored in the VTT cache payload at write
  time so future entries render the film name directly. Pre-0.7.4
  entries fall back to NOTE-header parsing and a first-cue preview
  for visual identification.
- 6 new API endpoints under `/api/cache/...` (list / delete /
  clear-all for each of the two buckets). Defensive against
  path-traversal at the boundary; refuses to touch
  `settings.json` / `jobs.json` even with a syntactically valid
  key.

### Tests

- 14 unit tests in `tests/test_cache_explorer.py` covering listing
  (media-path-rich + legacy + corrupt + sorting), deleting
  (existing, missing, path-traversal, runtime-file refusal), and
  clear-all (both buckets).
- 3 smoke tests in `tests/test_smoke_api.py` for page render +
  API listing + path-traversal HTTP 400.

## [0.7.3] — 2026-05-12

Two operator-facing additions that came out of the Inception
post-mortem: a way to disable region-packing without editing
config files, and a way to keep the persistent jobs table from
growing unbounded.

### Added

- New Settings field `stt_region_packing` (Speech-to-Text section,
  OpenVINO-only). The setting itself has existed in `config.py`
  since 0.6.0 but was never exposed in the UI. Turning it off is
  the first thing to try when dialog goes missing in long-film
  output — packing multiple short speech regions into one Whisper
  window with 0.5 s silence pads can cause legitimate cues to be
  dropped if Whisper's predicted timestamp drifts into a pad zone.
  Cost of OFF: 1.5-3× more iGPU compute on dialog-heavy films.
- "Clear finished" button in the Jobs table header. Removes all
  jobs in terminal states (succeeded / failed / canceled) from
  both the in-memory list and the `jobs.json` persistence so the
  dashboard table doesn't grow unbounded across weeks of runs.
  Running, queued, and canceling jobs are left alone — clearing
  those mid-flight would orphan the runner coroutine. Backed by
  `POST /api/jobs/clear-finished` (returns `{"cleared": N}`).

### Tests

- 3 new tests in `tests/test_jobs_persistence.py` covering the
  clear-finished behavior: terminal-state pruning + disk
  persistence, no-op when nothing to drop, and the canceling-job
  preservation invariant.

## [0.7.2] — 2026-05-12

Fixes a long-standing STT timestamp bug that was masked by the
80 % OOM crashes until 0.7.1 finally let runs reach the `.vtt`
writer. On any media longer than the audio segment size (default
600 s ≈ 10 min), every cue from segments 2..N was stamped with
segment-relative timestamps instead of source-audio-absolute,
causing **all** subtitles to collapse into the opening 10 min of
the timeline — text was correct, timestamps were wrong.

### Fixed

- `app/pipeline/stt_openvino.py`: the region-packing remap returns
  segment-relative cue timestamps; the loop now lifts them by
  `seg_offset_seconds` (= `file_pos / sample_rate`) before
  appending to the cue list. The additive offset was present in
  the pre-0.6.0 chunked-mode path but got dropped during the
  region-packing refactor; this restores it. The CPU/faster-whisper
  backend was unaffected (faster-whisper yields globally-correct
  segment timestamps from its own iterator).

### Changed

- `transcript_cache` key schema bumped from v1 to v2. Any cached
  transcription stored by 0.7.0–0.7.1 has the broken
  segment-relative timestamps baked in; bumping the key prefix
  forces a one-time miss so users don't silently inherit a
  poisoned cache. Old `.json` files are left on disk and can be
  cleaned with `rm -rf cache_dir/transcripts/*` if desired.

### Tests

- New `tests/test_stt_segment_offset.py` exercises the full
  multi-segment `transcribe()` loop with mocked Whisper/VAD/
  soundfile. Asserts that a cue produced inside segment 1
  (`file_pos = segment_seconds`) lands at an absolute time
  ≥ `segment_seconds`, not at 0-segment_seconds. Verified the
  test fails on the pre-fix code with the expected diagnostic.

## [0.7.1] — 2026-05-11

NLLB-1.3B now fits comfortably under a 12 GB cgroup. Two changes
target the residual translation-phase memory ceiling.

### Added

- New setting `nllb_load_in_8bit` (default **ON**). On the OpenVINO
  path, the model is quantized to int8 via NNCF at load time. Cuts
  resident weight memory in half — `distilled-1.3B` drops from
  ~3 GB to ~1.5 GB. First-time export pays a 1-2 min quantization
  step; the int8 IR is cached on disk so subsequent loads are fast.
  Quality cost is ~0.3 BLEU, below the noise floor for subtitle
  translation. Exposed in the Settings UI as "Compress NLLB weights
  to int8 (OpenVINO path)" under the Translation section. The
  CPU/torch fallback ignores this flag — bitsandbytes int8 needs
  CUDA and isn't in the base image.

### Changed

- NLLB translation loop now does explicit `del inputs, generated,
  decoded` after every batch + `gc.collect()` + `try_malloc_trim()`
  every 10 batches. Without this, resident memory drifts upward
  through a long translation (allocator fragmentation + lingering
  internal pools from optimum-intel's OV inference) and eventually
  trips a 12 GB cap even though no single batch is large. The
  periodic trim returns the freed glibc arenas to the kernel so
  the cgroup actually sees the memory back.

### Behavioral effect

On a 2 h film at large-v3-turbo (STT) + NLLB-distilled-1.3B
(translation), translation-phase peak goes from ~11.5 GB
(crashing at 12 GB) to roughly ~8 GB — comfortable headroom under
a 12 GB cgroup even with Whisper's page cache still in residence.

If you previously set `BABEL_NLLB_LOAD_IN_8BIT=false` (or you
explicitly want fp32 weights for some reason) the toggle is in
Settings → Translation.

## [0.7.0] — 2026-05-11

**Resume from 80%.** When translation crashes (OOM, transient
provider error, container restart), the next retry no longer
re-runs Whisper — it resumes directly at the translation phase
against the already-computed cue list. For a 2 h film at
large-v3-turbo that's ~30 minutes saved per retry.

### Added

- `app/transcript_cache.py` — on-disk JSON cache of
  `TranscriptionResult`, keyed only on STT-relevant inputs:
  `(content_fingerprint, whisper_model, whisper_backend,
  vad_enabled, track_index)`. Stored under
  `cache_dir/transcripts/{key}.json` with atomic `os.replace`
  writes and corrupted-file quarantine on read.

  The cache key deliberately does NOT include `target_lang`,
  `provider`, `mode`, or any LLM/vision setting — those don't
  affect the transcript. So changing the translation provider
  between runs ALSO hits the cached transcript.

- `app/processor.py` — checks `transcript_cache.lookup` before
  the audio-extraction block. On a hit, skips both ffmpeg audio
  extraction AND the Whisper pass entirely, jumping the progress
  bar straight to "translating (transcript cache hit)". On a
  miss, transcribes as normal and stores the result IMMEDIATELY
  after `stt.transcribe()` returns — before the translation phase
  begins, so a crash there is recoverable.

  The `stt.release()` + `lang_detect.release_detector()` calls
  are also skipped on a cache hit — nothing was loaded this run,
  so there's nothing to free.

### Behavioral effect

For a typical 2 h film at large-v3-turbo + NLLB-1.3B:

- First run: same total time as before. After Whisper succeeds
  the transcript is written to disk; total time unchanged.
- Translation crashes mid-flight: dashboard now shows a `failed`
  row with the last persisted progress (added in 0.6.4).
- User retries: progress jumps to 80% immediately, only the
  translation phase runs. ~30 min → ~5 min on the retry.

### Tests

- 14 new unit tests in `test_transcript_cache.py` covering
  round-trip, key invalidation per STT axis, empty-cue suppression,
  corrupted-file quarantine, atomic writes, key composition.
- 2 new integration tests in `test_processor.py`:
  `test_transcript_cache_hit_skips_audio_extract_and_whisper`
  (the load-bearing one — on a hit, neither audio.extract_audio
  nor stt.transcribe must be called) and
  `test_transcript_cache_stored_after_successful_transcribe`
  (proves the file is on disk BEFORE the provider runs, so a
  translation-phase crash leaves it recoverable).

291 tests, all green (was 275).

### Operational notes

- Cleanup policy: none for now. Each transcript serializes to
  ~200 KB. Disk pressure? `rm -rf cache_dir/transcripts/` is
  safe — next run just re-transcribes.
- Want to force re-transcription? Either delete the specific
  file, or change `whisper_model` / `vad_enabled` (cache key
  includes them, so toggling invalidates).

## [0.6.9] — 2026-05-11

Dashboard layout: every status card is now a single horizontal row of
pills, the jobs table moves below the how-to copy, and the explanatory
text is tightened.

### Changed

- **Media server card** — trailing `<p><code>{{ url }}</code></p>` is
  gone. The type + connected pills are enough; the URL is configuration
  detail that lives in Settings.
- **STT card** — for the CPU backend, `compute_type · device` is now
  a muted pill inside the row instead of a trailing `<p>`. The
  OpenVINO dynamic `AUTO → GPU` pill was already in the row; it'll
  visually sit on the same line as long as the card width permits
  (the `.pill-row` flex wraps only when pills overflow).
- **Vision card** — `vision_llm_model` moves into the pill row as a
  muted pill, matching how the Translation card now renders its model.
- **"How to subtitle a film"** rewritten as one short sentence and
  moved above the jobs table, so the dashboard reads top-to-bottom:
  status → how-to → jobs activity.
- **Jobs section header** renamed *Recent jobs* → *Jobs*.

## [0.6.8] — 2026-05-11

NLLB-1.3B memory peak slashed so it fits comfortably under a 12 GB
cgroup alongside the residual page cache of Whisper-large. Two
complementary fixes:

### Added

- `app/pipeline/stt.py:try_malloc_trim()` — Linux/glibc-only helper
  that calls `malloc_trim(0)` to force glibc to return freed arenas
  to the kernel. Without it, `gc.collect()` releases Python objects
  but glibc keeps the memory in its internal pools, so the cgroup
  still sees it as in-use. That's why the previous OOM-killed at
  anon-rss=1.96 GB despite the model being logically freed — the
  un-trimmed arenas from the Whisper era + the in-flight NLLB
  allocation breached the cap. Silent no-op on Alpine/musl.

### Changed

- `release_model()` in both STT backends + `release_detector()` in
  lang_detect now call `try_malloc_trim()` after `gc.collect()`.
- `nllb_batch_size` default: **16 → 4**. The KV cache during
  `model.generate()` scales as `batch × num_beams × seq_len × hidden
  × num_layers`; for NLLB-1.3B at batch=16 that was ~1.5 GB of
  transient activation memory on top of the ~3 GB weight footprint.
  batch=4 brings the activation peak to ~400 MB. Users with the
  600M variant or more headroom can bump it back via the Settings UI
  for throughput.
- `_MAX_LEN` in `translate/nllb.py`: **256 → 128**. Subtitle cues are
  short — almost always under 30 source words → under 50 tokens, and
  the translated output is similarly bounded. 128 covers every
  realistic cue with margin and halves the KV cache footprint.
- `num_beams` in NLLB inference: **2 → 1** (greedy decoding).
  Quality difference on subtitle-length cues is negligible — beam
  search benefits long-form generation where late tokens recover
  from early choices, but a 5-15-word utterance rarely needs it.
  Halves the KV cache again.

Combined savings for a typical translation phase with NLLB-1.3B:
~2-3 GB lower peak. The combo of `large-v3-turbo + NLLB-1.3B` should
now run with headroom in a 12 GB cgroup.

## [0.6.7] — 2026-05-11

Dashboard polish — model names rendered consistently, Parameters card
streamlined.

### Changed

- **Translation card**: model names (NLLB variant, LLM model) now
  appear as muted pills inside the same row as the provider pill —
  matching the STT card's `whisper_model` style. Previously they
  rendered as inline `<code>` in a separate `<p>`, which put them in
  a monospace font that didn't match the rest of the dashboard. The
  `<p>` line is gone for all three provider branches.
- **DeepL branch** of the Translation card: the "API key: [set]" line
  is removed. Only the missing-key warning is surfaced as an inline
  warn pill — when the key is set there's nothing useful to display.
- **Parameters card** (was "Default job"): renamed for clarity (the
  card is a parameters summary, not a job status). The `→` arrow
  before the target-language pill is gone — pure decoration — and
  the trailing "Click *Subtitle this*…" help paragraph is gone since
  that flow is already explained in the "How to subtitle a film"
  section at the bottom of the dashboard.

## [0.6.6] — 2026-05-11

Dashboard card cleanup — strip redundant chrome.

### Changed

- **STT card**: the static "OpenVINO IR" bottom line is gone when
  `whisper_backend = openvino`. It restated the already-visible
  backend pill and added no information the dynamic
  `AUTO → GPU / AUTO → CPU` device pill doesn't surface. The CPU
  backend still shows `int8 · cpu` (the compute-type + device combo
  there genuinely affects throughput and quality).
- **Translation card (NLLB branch)**: the "free · local" muted pill
  is gone. Picking NLLB is the choice; saying "free · local" right
  after just restates the implication. DeepL's "cloud · 500k/mo free"
  and the LLM branch's "cloud or local" pills are kept since they
  carry quota / network-mode info the user actually wants visible.

## [0.6.5] — 2026-05-11

Settings page reorganized so each section now contains the knobs it
actually owns. Previously the Translation provider chooser lived in
"Defaults" while the NLLB / DeepL / LLM knobs that depend on it lived
two sections away — picking DeepL meant scrolling down to find your
API key in an orphaned single-field "API keys" section.

### Changed

- `default_translation_provider` moves from **Defaults** to the top of
  **Translation**, where it can sit visually adjacent to the knobs it
  gates (NLLB model variant, batch sizes, DeepL key).
- `deepl_api_key` moves from the orphaned **API keys** section into
  **Translation**, next to `deepl_batch_size`, with field-level
  `show_if` so it only appears when provider=DeepL.
- `translation_batch_size` moves from **Translation** to the top of
  **Translation model** — it's LLM-only and belongs with the rest of
  the LLM config.
- The **API keys** section is removed (now empty after the DeepL key
  migration).
- Section display order is reflowed by workflow priority: Media server
  (start here) → Defaults → STT → Translation → Translation model →
  Vision → Scene & Cinematic → Subtitles → Resource safety → Security.
  Advanced tuning (Resource safety, Security) sits at the bottom so
  it doesn't crowd the first thing a fresh user sees.

### Removed

- Section-level `show_if` on **Translation**. The provider chooser
  now lives inside the section, so hiding the whole section would
  also hide the only way to change provider. Field-level `show_if` on
  each NLLB/DeepL/LLM-only field handles the conditional visibility.

### Tests

275 tests, all green (no changes to test code — the reorg is entirely
within `_FIELD_META` and `_SECTION_META` / `_SECTION_SHOW_IF`, which
the template renders generically).

## [0.6.4] — 2026-05-11

Jobs queue is now persisted to disk. After an OOM-kill or any other
restart, the dashboard regains every previously-known job — including
the one that died mid-flight, with its last-known progress baked into
the error column. Previously the queue lived only in RAM, so a kill
wiped every trace and the user was left wondering whether the job had
ever existed.

### Added

- `app/jobs_store.py` — JSON-backed persistence at
  `cache_dir/jobs.json` with atomic `os.replace` writes (same pattern
  as the settings store).
- `app/jobs.py:load_persisted()` — startup hook called from
  `app/main.py:lifespan`. Reads the file, populates the in-memory dict,
  and marks orphans (`queued` / `running` / `canceling` from the
  previous instance) as `failed` with a descriptive error that
  includes timestamp and last-known progress:

  ```
  process restarted at 2026-05-11 19:42:13 before job finished
  (likely OOM-kill or container restart) — last progress: 78% transcribing
  ```

- `app/jobs.py:_persist()` / `_persist_throttled()` — internal helpers
  called from every status transition (queued→running, →succeeded,
  →failed, →canceled) and from `Job.update_progress` (throttled to one
  write per 3 s per job).

### Changed

- `Job.update_progress` now writes a throttled disk snapshot so a kill
  mid-transcription preserves "stage=transcribing, pct=78" rather than
  whatever was last on disk.
- `_run()` persists immediately at every status transition; the throttle
  is reserved for the frequent progress updates.

### Trade-offs

- ~1 KB of disk write per status transition + at most one ~1 KB write
  every 3 s per running job. Negligible on the 500-job cap.
- Persistence is best-effort: any IO error is logged + swallowed, and
  the in-memory queue remains the source of truth for the running
  process. A corrupted on-disk file is renamed to `.corrupt` at
  startup and the queue starts fresh — uvicorn never crashes over a
  bad jobs file.

### Tests

- 14 new tests in `test_jobs_persistence.py` covering round-trip,
  orphan rewrite, atomic write, corrupted-file recovery, throttling.

266 → 275 tests, all green.

## [0.6.3] — 2026-05-11

Resource fix: free the STT model before the translation phase loads its
own weights. A real incident on TrueNAS — cgroup `mem_limit: 6g`,
whisper-small + NLLB-600M — produced a silent kernel OOM-kill at the
80% mark of the pipeline (no Python traceback, no error on the job,
just a job that stops producing a .vtt). Root cause: Whisper-small
(~1 GB) stayed resident through the NLLB-600M (~1.5 GB) initialization
spike; combined with Python heap, torch pools and the page cache of
the mmap'd model files, the cgroup limit was breached right at the
translation-phase model load. The `@lru_cache(maxsize=1)` decorators
on the model factories had no eviction hook — once warmed, models sat
for the lifetime of the process.

### Added

- `app/pipeline/stt_openvino.release_model()` — drops the cached
  OpenVINO IR Whisper model + processor. `cache_clear()` plus
  `gc.collect()` so the OpenVINO `CompiledModel` destructor runs and
  releases the iGPU-reserved RAM.
- `app/pipeline/stt_faster_whisper.release_model()` — analogous for
  the CPU/faster-whisper backend.
- `app/pipeline/stt.release()` — dispatcher mirror of `transcribe()`;
  picks the right backend's release function based on
  `whisper_backend`.
- `app/pipeline/lang_detect.release_detector()` — frees the tiny
  language-detection model (~250 MB resident) after the pre-pass.

### Changed

- `app/processor.py` now calls `stt.release()` (and
  `lang_detect.release_detector()` when the pre-pass ran) between the
  NoSpeech check and `progress(80, "translating")`. So by the time
  `get_provider()` instantiates NLLB / the vision LLM client, the
  Whisper weights are gone and the cgroup has its headroom back.

### Trade-off

The next job pays a 10-30s Whisper reload cost — dwarfed by the
actual decode work (which is the long pole at 8-80% of the pipeline
budget). If anyone hits this and would prefer a configurable "keep
Whisper warm between jobs" mode for a beefier deployment, this is the
obvious knob to add (default off).

### Tests

- `test_processor_releases_stt_before_translation` — end-to-end spy
  that asserts the call order release → get_provider → translate.
- Three direct unit tests for `release_model()` / `release_detector()`
  cache eviction.
- One dispatcher test for `stt.release()` picking the right backend.

261 → 266 tests, all green.

## [0.6.2] — 2026-05-10

UI cosmetics: the per-job elapsed-time counter now sits INSIDE the
progress bar overlay, next to "65%" and the stage name, instead of on
its own line below the bar. Reads as one unit ("1m 32s · 65% ·
transcribing") and frees the row height for more dense job tables.

### Changed

- `_jobs_table.html` (dashboard): elapsed-time merged into the
  progress-label for running/canceling jobs. For terminal states
  (succeeded/canceled) the elapsed is inlined alongside the status
  word in the same label ("100% · 1m 32s", "canceled · after 1m 32s").
- `library.html` per-row batch progress: same treatment. Row width
  bumped 7rem → 8rem to accommodate the combined text; status column
  widened 7rem → 9rem.
- `base.html` adds a `.progress-label .elapsed-time` CSS rule so the
  nested element inherits the label's color and size, with
  tabular-nums kept so seconds don't jitter the surrounding text.

The global elapsed-time ticker in base.html is unchanged — it queries
`.elapsed-time[data-elapsed-base]` regardless of DOM position and
updates only the inner element's textContent, so the surrounding
"· 65% · transcribing" stays put while seconds tick up.

## [0.6.1] — 2026-05-10

Build-environment hygiene. Both image flavors now run Python 3.12.

### Changed

- **OpenVINO image base** bumped from `openvino/ubuntu22_runtime:2025.4.1`
  (Ubuntu 22.04 + Python 3.10) to `openvino/ubuntu24_runtime:2025.4.1`
  (Ubuntu 24.04 + Python 3.12). Same OpenVINO runtime; newer interpreter.
- **CPU image base** bumped from `python:3.11-slim` to `python:3.12-slim`
  so both flavors are in lockstep on the same interpreter version.
- **`pyproject.toml` `requires-python` bumped to `>=3.12`** — was
  previously `>=3.11` which was a lie because the openvino image was
  running 3.10. The two are now consistent and accurate.
- **Dockerfile fix from earlier today**: `torchaudio` installed from
  the CPU wheel index alongside `torch` so silero-vad's transitive
  dependency doesn't pull an ABI-incompatible CUDA-build wheel. (Was
  shipped as a hotfix between 0.6.0 and 0.6.1.)

### Why 3.12

- Security support through **October 2028** (3.10 EOLs October 2026).
- ~10-15% faster pure-Python execution from the "Faster CPython"
  project (3.11) + per-interpreter improvements (3.12). Modest in our
  workload since most wall-clock is in torch/OpenVINO/ffmpeg C++,
  but it's free and compounds.
- PEP 657 fine-grained tracebacks point at the exact subexpression
  on errors — saves time when debugging in `docker logs`.
- Better dict/set perf, faster startup, smaller per-interpreter
  baseline RAM.

### Validated dependencies

All Python deps have official 3.12 wheels:
- `torch` / `torchaudio` (CPU index): since 2.2 (we're on 2.5+).
- `transformers`, `optimum-intel[openvino]`: yes (for years).
- `faster-whisper`, `silero-vad`, `sentencepiece`, `soundfile`: yes.
- `fastapi`, `uvicorn[standard]`, `pydantic`, `httpx`, `jinja2`,
  `anthropic`, `openai`: all 3.12-clean.

No code changes were required to support 3.12 — we don't use any
3.10-or-3.11-only syntax.

## [0.6.0] — 2026-05-10

The performance-within-safety release. Headline: **STT region packing**
cuts iGPU compute 1.5-3× on dialog-heavy films by concatenating short
speech regions into shared 30 s decoder windows. Plus a sweep of
remaining safety contributors that the second code-review surfaced:
audio temp file relocated off /tmp, asyncio executor capped to 4
workers, scene-bible keyframes now lazy (same anti-pattern we already
fixed for cinematic, at smaller scale), settings persistence uses
copy-on-write so concurrent readers can't see half-applied state, LLM
clients disabled SDK-level retries to keep the per-call timeout a true
ceiling, and ffmpeg/ffprobe subprocesses + media-server HTTP clients
got proper timeouts.

### Added — performance + correctness

- **STT region packing.** New module `app/pipeline/packing.py`. When the
  VAD finds short speech regions (3-10 s, the typical dialog
  utterance), the planner concatenates several into a single 30 s
  Whisper decoder window with brief silence pads between, then
  demultiplexes the emitted cues back to original-audio timestamps via
  each window's `region_map`. On dialog-heavy films this cuts iGPU work
  ~2-3× because each chunk used to be mostly zero-padding (a 7 s region
  in a 30 s window is 77% wasted decode). Default ON via
  `stt_region_packing`; flip false as an escape hatch if a specific
  film shows misattributed cues at region boundaries.

- **Cross-segment region merging.** STT segment reads now pull an extra
  `stt_segment_overlap_seconds` (default 30 s) past each segment
  boundary. A speech region that straddles the boundary is processed
  fully within one segment; the next segment skips ahead to where the
  previous one stopped. Eliminates the split-word artifacts at segment
  boundaries that the audio-segmentation feature introduced in 0.5.0.
  Costs ~1.9 MB extra peak RAM during the read.

- **Scene-bible keyframes are now lazy** (mirroring the cinematic
  cue_frames refactor from 0.5.0). The processor passes a closure to
  `scene_bible.describe_scenes(keyframe_provider=...)`; each LLM batch
  extracts its `scene_bible_batch_size` (default 10) frames inline and
  releases them. Drops peak RAM during the 5-15 min bible build from
  ~125 MB (500 scenes × 250 KB JPEGs) to ~2.5 MB.

- **`faster_whisper.detect_language()` for the pre-pass.** The previous
  shim ran a full `transcribe()` call and discarded the segments just
  to read `info.language`. Switched to the SDK's dedicated single-pass
  language-detection API — ~3-5× faster on the pre-pass with the same
  accuracy.

- **`cinematic_frame_accurate_seek` end-to-end coverage**: the option
  from 0.5.0 is now exercised in tests (fast / accurate / accurate-at-
  zero-timestamp fallback).

### Changed — safety + concurrency

- **Audio temp files relocated to `<cache_dir>/tmp/`** (was `/tmp`). For
  a 2 h film at 16 kHz mono 16-bit WAV that's ~250 MB. On TrueNAS,
  `/tmp` is commonly tmpfs and counts against host memory — every
  batched job piling up there could collide with the 6 GB cgroup cap
  from another angle. The cache_dir is bind-mounted to a real volume
  so the bytes never spill into RAM.

- **asyncio default executor capped at 4 workers** in
  `app/main.py:lifespan`. Python defaults the thread pool to
  `min(32, os.cpu_count() + 4)` — on a 16-core host that's 20. With the
  cgroup `cpus: "4.0"` cap, an HTMX poll burst could spawn ~20 threads
  each running OMP-parallel torch/numpy and blow past the CPU
  allocation. The job lock already serializes the heavy pipeline; this
  cap protects the sync FastAPI handlers that aren't gated by it.

- **Settings persistence uses copy-on-write.** `update()` / `reset()` /
  `reset_all()` now build a NEW dict and rebind `self._overrides`
  atomically rather than mutating it in place. A concurrent reader
  doing `settings.whisper_model` sees either the pre-update or
  post-update snapshot — never a half-applied state. Atomic rebind +
  the existing `os.replace` write give a clean memory + on-disk
  consistency guarantee.

- **LLM clients constructed with `max_retries=0`.** The Anthropic and
  OpenAI SDKs both default to silent transparent retries (2 per call,
  each paying the full 300 s timeout). On a wedged backend that
  multiplies into ~15 min per call, which would blow the 90 min
  job-level deadline budget on a single batch. We'd rather see fast
  failures here and let the higher level decide whether to retry.

- **Plex section cache gained a 1-hour TTL.** Previously the
  module-level cache was effectively forever — an operator who renamed
  a Plex library section needed a container restart to see the change.
  TTL lets the change land within an hour without restart.

- **httpx timeouts bumped 30 s → 60 s** on the Emby/Jellyfin and Plex
  clients. Real deployments with 100 k+ items on slow storage hit
  ReadTimeout on Library page renders at 30 s.

- **Subprocess timeouts added** to every ffmpeg / ffprobe /
  mkvpropedit call (audio extract: 60 min, frame extract: 30 s,
  ffprobe / mkvpropedit: 30-60 s). The job-level deadline is still the
  real fence; subprocess timeouts are defense-in-depth so a single
  wedged invocation doesn't park the worker for the full job timeout.

### Added — P2 hardening (review items 14-35)

A correctness/hygiene sweep across the code-review items that didn't make
the resource-safety release. Mostly defensive tightening of inputs,
clearer error messages, and a handful of small UX/perf wins.

- **`/api/process/{id}` rejects garbage `mode` / `translation_provider`
  at the FastAPI schema layer.** Both are now `Literal[...]` typed —
  bad values 422 with the enum list, instead of falling through to a
  less-readable BadRequest deeper in the pipeline.

- **LLM translation provider now catches duplicate cue ids in the
  response.** Previously a model that returned `[{id:0}, {id:0}, {id:1}]`
  for a 3-cue batch silently dropped one cue under the dict-dedup. New
  `Duplicate cue id(s) [...]` error surfaces it as a clear `TranslationError`
  rather than producing a translation with missing lines.

- **Frame-accurate seek for cinematic mode** (`cinematic_frame_accurate_seek`,
  default false). False keeps the current fast keyframe-snap seek
  (`-ss <ts> -i <file>`) for scene-bible keyframes and most cinematic
  use cases. True switches the per-cue extractor to the combined seek
  pattern (`-ss <ts-5> -i <file> -ss 5 -frames:v 1`) — frame-accurate at
  the cost of decoding ~5s of video per cue. Useful only when extracted
  frames will drive fine-grained visual decisions (lip-sync, on-screen
  OCR).

- **NLLB and DeepL batch sizes are now configurable.** New settings
  `nllb_batch_size` (default 16, range 1-128) and `deepl_batch_size`
  (default 50, capped at DeepL's documented per-call max). Surface in
  the Translation section of the Settings UI — only visible when the
  matching provider is selected. The previous hardcoded constants
  forced one tuning for everyone.

- **Plex `list_videos` forwards `start_index` / `limit` to the server.**
  Previously every Library page render fetched 10 000 items per section
  and sliced in Python — fine for small libraries, catastrophic for a
  50 k-episode show section. Now we pass `X-Plex-Container-Start` /
  `X-Plex-Container-Size` directly, so the server does the pagination.
  The aggregate (no `library_id`) path now fetches only `start_index +
  limit` items per section rather than 10 k.

- **Plex section cache is now module-scoped and survives across
  client instances.** `media_server_client()` builds a fresh `PlexClient`
  per request, so the previous per-instance cache was always cold.
  The new cache is keyed on `(base_url, token)` — so two users hitting
  the same server with different tokens still don't share entries.

- **Refresh failures are now logged at WARNING.** Previously
  `server.refresh_item(...)` swallowed `MediaServerError` silently;
  operators debugging "why didn't Emby pick up my new subtitle"
  couldn't see why. The subtitle is still written; the log line just
  tells you the server didn't get pinged.

- **OpenVINO `_parse_segments` logs dropped degenerate timestamps**
  at DEBUG level rather than discarding them invisibly. Regressions
  that turn half the cues into degenerates become visible in `docker logs`.

### Changed

- **`_coerce` in the settings form uses `typing.get_origin` instead of
  substring-matching `str(target)`.** Behavior on the current field set
  is unchanged (covered by `test_ui_coerce.py`); the principled
  inspection drops the silent mis-dispatch footgun for any future
  annotation that mentions "bool" / "int" / "list" in a non-matching
  position (e.g. a `Literal["bool"]` field would have coerced to bool
  under the old logic).

- **UI help text for the language write-back checkbox** now correctly
  describes the per-backend behavior. The previous text claimed "we
  always run a Whisper-tiny pre-pass" which is openvino-only — the
  CPU/faster-whisper backend detects internally during the main
  transcribe call.

- **`vad_enabled` setting docstring** clarified that it's openvino-only
  and that the CPU backend runs its own internal VAD which is unrelated
  and not toggleable through this flag.

- **`openai_compat` LLM client** documents the wire-format expectation
  (typed-list user content). Modern Ollama / LM Studio / vLLM accept
  it; ancient versions need an upgrade rather than a client-side
  fallback.

## [0.5.0] — 2026-05-10

The resource-safety + observability release. Triggered by a TrueNAS host
that kernel-OOM'd while subtitling a 2 h 12 min film — this version closes
the entire class of "long film eats all available RAM" failures, adds a
job-wide deadline so a wedged run can't camp on the queue lock, and ships
an optional HTTP Basic + same-origin guard for any deployment where the
LAN isn't fully trusted. The running version is now rendered in the footer
of every page and exposed via `GET /api/version` so it's obvious which
build you're talking to.

### Added — resource safety + auth (the OOM-prevention pass)

A 2h12 film pushed a TrueNAS host into kernel-OOM territory; the post-mortem
turned up a stack of contributors (full audio buffered in RAM, per-cue
JPEGs pre-extracted across the whole film, three permanently-resident ML
models, no cgroup limits on the container, no job timeout). This release
addresses each of them.

- **Audio segmentation for the OpenVINO STT path.** The wav is now read
  in N-second segments (default 600 s ≈ 10 min) instead of slurping the
  whole 2h+ buffer at once. Each segment is independently VAD-filtered
  and transcribed, then released before the next is read. Peak audio
  RAM drops from ~500 MB for a 2 h film to ~75 MB regardless of length.
  New setting: `stt_audio_segment_seconds` (env
  `BABEL_STT_AUDIO_SEGMENT_SECONDS`). The CPU/`faster-whisper` backend
  streams from disk on its own and ignores this knob.

  Trade-off: words straddling a segment boundary may split into two
  cues. With 600 s segments and typical films, that's at most ~10
  boundaries; tunable up if you'd rather trade RAM for fewer splits.

- **Cinematic frames are now lazy + capped.** Previously the pipeline
  pre-extracted one JPEG per cue across the whole film into a single
  dict (1500+ frames ≈ 200-300 MB resident) before any translation
  began. New behavior: a closure handed to the translator extracts
  frames per translation batch — peak RAM is `cinematic_batch_size`
  frames instead of all of them. Plus a new cap
  `cinematic_max_cues_with_frames` (default 800) bounds how many cues
  get a frame at all; out-of-cap cues still translate, just text-only.
  Set to 0 to disable per-cue frames entirely (cinematic degrades
  to scene-mode behavior).

- **Per-job wall-clock timeout.** New `job_timeout_seconds` setting
  (default 5400 = 90 min, 0 = unlimited). Enforced at every pipeline
  checkpoint via `Job.check_cancel`, so a wedged Whisper run no longer
  holds the queue lock indefinitely. A new `JobTimeout` exception
  subclasses `JobCanceled` so existing handlers compose; the UI shows
  the job as `failed` with `timeout: …` rather than `canceled`.

- **Scene detection is now cancellable.** `ffmpeg`'s scene-detection
  pass over a 2h+ film could run for 10+ minutes with no way to stop
  it — `subprocess.run(..., capture_output=True)` blocked the runner.
  The new implementation streams ffmpeg stderr line-by-line, calls
  `check_cancel` between lines (so the deadline and user cancel both
  reach it within a couple of seconds), and terminates the subprocess
  cleanly on bail. Also adds `-an` to skip audio decoding — pure waste
  of CPU on a video-only filter — and stops buffering the entire stderr
  in RAM.

- **Streamed first-30 s read for language detection.** `lang_detect`
  previously called `sf.read(full_wav)` just to slice off the first 30
  seconds — a 500 MB allocation right before the heaviest stage, in
  parallel with the soon-to-be-streamed STT read of the same file.
  Now uses `SoundFile.read(frames=30 * sr)`.

- **Container-level resource limits.** `docker-compose.yml` now sets
  `mem_limit: 6g`, `memswap_limit: 6g` (no swap escape), `cpus: "4.0"`,
  `pids_limit: 1024`, and tightened ulimits. These are the actual
  kernel-enforced fence — the in-process caps above reduce the chance
  of ever hitting it. Sized for the default workload (openvino + small
  Whisper + NLLB-600M, audio mode). Bump for whisper-medium/large or
  NLLB-1.3B+.

- **BLAS / OMP thread caps baked into both Dockerfiles.** torch,
  transformers, numpy, and numexpr each defaulted to spawning
  `os.cpu_count()` worker threads. On a 16-core host that's ~50
  concurrent worker threads during transcription. Set
  `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`,
  `NUMEXPR_NUM_THREADS=4` and `TOKENIZERS_PARALLELISM=false` so they
  line up with the `cpus: "4.0"` cgroup cap.

- **Optional HTTP Basic auth + same-origin CSRF guard.** New
  `auth_credentials` setting (env `BABEL_AUTH_CREDENTIALS`). Empty
  (default) = auth off (preserves zero-config first boot). Set to
  `user:password` to require Basic auth on every endpoint except
  `/health`, plus a same-origin check on POST/PATCH/PUT/DELETE so a
  malicious LAN page can't ride your saved browser credentials to start
  jobs that burn your LLM quota. Direct API clients (curl, scripts)
  pass on Basic creds alone — the CSRF check only fires when Origin or
  Referer is present and mismatched.

- **Running version is now visible.** The footer of every page renders
  `Subtitle This v0.5.0` so it's immediately clear which build you're
  looking at. `GET /api/version` returns `{"version": "0.5.0"}` for
  scripts and monitoring. Single source of truth is
  `app/__init__.py:__version__` — both `pyproject.toml`, the FastAPI
  app, the OpenAPI doc, and the footer read from there.

### Changed

- **lru_cache(maxsize=2) → maxsize=1** for the Whisper-OV, faster-whisper,
  and NLLB model caches. The previous size let a user toggling between
  two models double the resident RAM (whisper-large is ~3 GB; NLLB-1.3B
  ~3 GB) for no real workflow benefit. The cache still keys on full
  config so same-config jobs hit cleanly.

- **Pydantic `Field(ge=, le=)` bounds on numeric settings.** Previously
  the UI accepted `scene_max_scenes=9_999_999`, `cinematic_batch_size=999`
  etc. without complaint and only failed downstream in subprocess land.
  Now invalid values are rejected at PATCH time with a clear error.

- **Atomic `settings.json` writes** via `os.replace` from a `.tmp`
  sidecar, plus a `threading.Lock` around the read-modify-write so two
  simultaneous UI saves can't race. A corrupt file on load is moved to
  `settings.json.corrupt.<ts>` and logged at WARNING, rather than
  silently zeroing the user's API keys.

### Fixed
- **OpenVINO STT no longer hallucinates on silence.** The OpenVINO backend
  calls `OVModel.generate()` directly (to dodge the HF pipeline's CPU↔iGPU
  round-trip), which means it bypasses Whisper's built-in no-speech /
  log-prob / compression-ratio guards. On silent audio — establishing
  shots, music cues, action without dialogue — the autoregressive decoder
  was inventing boilerplate from its language prior ("Thank you.",
  "Thanks for watching.", repeated lines). Pre-filtering with Silero-VAD
  and chunking strictly within speech regions kills this entirely. Side
  effect: typical films are 30–50 % silence, so transcription is also
  meaningfully faster (a 47 min run on a 2h28 film should drop to roughly
  half that on the same hardware). Add `BABEL_VAD_ENABLED=false` (or the
  Settings toggle) as an escape hatch for very-quiet-but-real-speech
  files where Silero is too strict. The CPU/`faster-whisper` backend
  already had its own VAD (`vad_filter=True`) and is unchanged.

  Cache invalidation: `vad_enabled` is now part of the cache key for
  OpenVINO runs (and only for OpenVINO runs — the CPU backend's VAD is
  unrelated). Existing OpenVINO cache entries written before this change
  will miss on first re-run and recompute cleanly, so users automatically
  get the fix applied to films they've already processed. CPU-backend
  entries are unaffected.

## [0.4.0] — 2026-05-03

The post-rename refinement release. The 0.3.0 → 0.4.0 jump consolidates a
month of bug fixes, correctness work, and a major UI overhaul. The product
is the same — Emby/Jellyfin/Plex auto-subtitling — but the surface is more
honest about what's needed when, the cache is genuinely impossible to
needlessly invalidate, and a code-review pass found and fixed three latent
correctness issues in the LLM dispatch and HTTP layers.

### Added
- **Two-level cache fingerprint** (`010c502`). The transcript cache now uses
  a *quick* fingerprint (path + size + mtime) on the hot path AND a *content*
  fingerprint (size + mid-file byte samples, immune to mtime / path / metadata-
  only edits) as a stable fallback. On a quick miss we fall back to the content
  fingerprint, find the cached payload, and re-link it under the new quick key
  so the next lookup is fast again. Eliminates the entire class of "we
  re-translated because mtime moved" bugs that previously cost users their LLM
  budget on rsync, mkvpropedit write-back, and library reorganizations.
- **HTTPS support with verify-SSL toggle** (`827546e`). Default-on cert
  verification works out of the box for Let's Encrypt-fronted servers; flip
  the new Settings checkbox off for Plex-via-LAN-IP (cert is for `*.plex.direct`)
  or self-signed homelab setups. The `SSL_CERT_FILE` env var route stays
  available for users wanting a custom CA bundle without disabling verification.
- **Cross-page batch selection** (`4d5d03b`). The Library page's multi-select
  now persists across pagination + page reloads via `localStorage`. Tick rows
  on page 1, paginate to page 3, tick more, hit *Subtitle selected* — every
  ticked item gets queued. Visible checkboxes are pure UI affordances; the
  form submits a hidden mirror of the entire saved selection. Counter shows
  on-page vs across-pages breakdown.
- **GHCR retention auto-prune** (`8ad17ed`, `50d19f4`). Each successful main
  branch build now prunes old GHCR versions automatically. Released versions
  (semver-tagged: `1.2.3-openvino`, etc.) are protected forever; SHA-pinned
  per-commit versions get cleaned up. Fixed the multi-arch sub-manifest
  retention math so the moving `:openvino`/`:cpu` tags never resolve to
  pruned platform-specific manifests.
- **LLM dispatch test coverage** (`d15c778`). 12 new tests for the previously-
  untested LLMTranslationProvider path: length-mismatch detection, missing-id
  detection, invalid-JSON detection, batch-size selection (text vs cinematic),
  cinematic vision-capability gating, scene bible payload structure, per-cue
  frame attachment.
- **EmbyJellyfin HTTP behaviour tests** (`8649735`). Ported the mock-transport
  pattern from the Plex test suite for parity. 7 new tests covering health,
  get_item, list_videos pagination, refresh.
- **UI form-coercion tests** (`8649735`). 12 new tests pinning down `_coerce()`
  dispatch logic for bool/int/float/list/str types from form strings.

### Changed
- **Settings UI** (`b8ee4c6`, `da52863`, `72fb962`):
  - Media server section moved to the top of the form ("START HERE — without
    a working media server connection nothing else is reachable").
  - Target language is now a dropdown (~38 languages) in both Settings and
    the Library filter form, replacing the free-form text input.
  - OpenVINO device dropdown removed entirely — defaulted to AUTO which Just
    Works (picks GPU when available, falls back to CPU). One less knob to
    misconfigure.
  - Whisper compute-type and device dropdowns hidden when backend = openvino
    (CPU-backend-only knobs).
  - NLLB model is now a curated dropdown (4 variants with size/quality
    badges) instead of a free-form HF model id. Hidden when provider != nllb.
  - LLM batch size hidden when provider != llm.
  - Source language priority hidden entirely (hard-coded `["en", "*"]`,
    overridable via env var).
  - Section-level conditional visibility: Translation, Translation model,
    Vision model, Scene & Cinematic, and API keys sections now hide
    themselves when irrelevant given the current Defaults config. A live JS
    watcher re-evaluates on every change.
  - Generic `show_if: {field, equals}` framework supporting both single-value
    and any-of (list) checks, declared in `_FIELD_META` and `_SECTION_SHOW_IF`.
- **Dashboard cards rewritten** (`da52863`). Cards now show only what the
  active config actually uses:
  - Translation card content adapts to provider (NLLB → variant; DeepL →
    free-tier indicator; LLM → wire protocol + model).
  - Vision card only renders when mode is scene/cinematic AND provider=llm.
  - Job-defaults card now shows the literal output filename pattern so users
    visualize what each *Subtitle this* click produces.
- **Compact, refined typography** (`2eed237`). Pico defaults override:
  14.5px / 1.5 line-height (was 16px / 1.65), tightened spacing variables,
  max-width 1280px (was 1100px), denser table rows for the data-heavy Library
  and Recent jobs views.
- **Settings layout polish** (`b8ee4c6`):
  - Library table uses `table-layout: fixed` with explicit per-column widths;
    long paths and error pills now ellipsis-truncate (with full text in
    `title=` for hover) instead of blowing the layout out.
  - Settings checkbox layout restructured so the supporting "currently:
    on/off" indicator and the help paragraph no longer overlap visually.
- **Validation consolidation** (`da03e33`). Mode/provider invariants
  (`mode in (scene, cinematic)` requires `provider=llm` etc.) lifted into a
  single `validate_mode_provider_combo()` helper in `processor.py`.
  `submit_item_job` and `process()` both call it; previously had duplicated
  logic with subtly different exception types and message wording.
- **Settings.json migration framework** (`2f9d1a8`). The 65-line straight-line
  migration block in `SettingsStore._load()` was extracted to a list of named,
  self-contained migration functions (`_rename_translation_provider_claude_to_llm`,
  `_split_unified_llm_into_per_function_slots`, `_drop_shared_anthropic_api_key`,
  `_rename_emby_to_media_server`). Adding the next migration is now one
  function + one append.

### Removed
- **`/api/sweep` endpoint** (`f6e3008`). The whole-library sweep had no UI
  affordance after we removed the dashboard button (deliberate — there's no
  legitimate use case where "subtitle every film in my 5000-item library at
  once" beats a deliberate batch). The HTTP endpoint, the
  `MediaServerClient.iter_videos()` abstract method (sweep was its only
  consumer), and the corresponding implementations in EmbyJellyfin/Plex are
  all gone. A regression test pins `/api/sweep` to 404.
- **Stale `BABEL_EMBY_*` env vars in compose** (`d15c778`). These were
  silently ignored after the rename to `BABEL_MEDIA_SERVER_*`, so a fresh
  `docker compose up` left the container with no server config. Renamed in
  `docker-compose.yml`; the legacy `EMBY_URL`/`EMBY_API_KEY` shell-env names
  are kept as a fallback for users with existing `.env` files.
- **`("llm", "claude")` legacy alias** (`da03e33`). Three places still
  accepted "claude" as a synonym for "llm". The settings.json migration
  rewrites "claude" to "llm" before any consumer sees it, so the synonym
  branches were dead code. Cleaned up in `app/pipeline/translate/__init__.py`,
  `app/processor.py`, and `app/api/manage.py`.
- **`MediaServerClient.list_videos(types=...)` kwarg** (`da03e33`). Unused —
  no caller passed it and the abstract base didn't declare it.
- **`file_fingerprint` alias in cache.py** (`da03e33`). Renamed to
  `quick_fingerprint`; no external callers since this isn't a published library.
- **OpenVINO device dropdown** (`da52863`). Hidden from the UI; default
  switched to AUTO.

### Fixed
- **Plex API: `health()` was probing an unauthenticated endpoint** (`6944d41`).
  `/identity` returns 200 to anonymous callers per the Plex docs, so the
  health check returned True even with a wrong/missing X-Plex-Token. Switched
  to `/library/sections` (auth-required, 401s on bad token) — a green pill
  now actually means "URL + token both work".
- **Plex API: `type=1,4` (comma-separated) was unsupported syntax** (`6944d41`).
  The Plex docs and python-plexapi reference both confirm the type filter
  accepts a single integer per call. Redesigned `_video_sections()` to pair
  each section with its natural content type (movie sections → type=1,
  show sections → type=4 for episodes), and `_section_page` now sends one
  request per (section, type) pair. The previous `type=1,4` would silently
  produce wrong or empty results.
- **Emby `get_item` used the unsupported `/Items/{id}` path** (`893d66a`).
  Some Emby versions return a static-file-style 404 ("The file '/Items/X'
  could not be found") on this endpoint, even when the same item works fine
  via the collection query. Switched to `GET /Items?Ids={id}&Fields=...`,
  which is universally supported on both Emby and Jellyfin. `_item_from_payload`
  also gained a `MediaSources[0]` fallback for the Path/MediaStreams fields
  in case the top-level Fields= projection isn't populated.
- **`process()` ran synchronously inside an `async def runner`** (`d15c778`).
  Whisper transcription is 20+ minutes on a feature film. Calling it directly
  from an async function pinned the event loop for that duration, blocking
  HTMX polling, /partials/jobs auto-refresh, and concurrent UI clicks.
  Wrapped in `asyncio.to_thread` so the runner yields the loop while
  transcription crunches.
- **Scene-bible cache key was incomplete** (`d15c778`). The bible cache
  keyed only on (fingerprint, vision_llm_model, scene_detection_threshold).
  But `scene_min_length_seconds`, `scene_max_scenes`, `scene_keyframe_position`,
  `scene_frame_max_size`, and `scene_bible_batch_size` ALL change what bible
  we'd produce. Bumping any of those silently served stale bibles. Extracted
  `_BIBLE_KEY_INPUTS()` listing every bible-affecting setting in stable order.
- **Symmetric LLM error handling** (`da03e33`). `openai_compat.py` was using
  `except Exception` while `anthropic.py` narrowed to `anthropic.APIError`.
  Both now use their respective SDK's parent error class. Both also pass an
  explicit timeout (5 min) so a wedged backend doesn't park a job indefinitely.
- **OCI multi-arch index correctness in GHCR retention** (`50d19f4`). The
  `actions/delete-package-versions` step with `min-versions-to-keep: 2` was
  pruning the platform-specific manifests of older builds, leaving the
  moving `:openvino`/`:cpu` indices pointing at non-existent sub-manifests.
  Result: `docker pull` failed with "manifest unknown" on a public image.
  Bumped `KEEP_LATEST_N_VERSIONS` to 12 (2 builds × 3 manifests × 2 flavors).

### Security
- **Plex SSL handling** (`827546e`). The new verify-SSL toggle exposes the
  trade-off explicitly in the UI: leave on for valid certs, turn off for
  self-signed/IP-based setups (with an explicit "trusted LAN only" warning).
  Help text also documents the secure middle ground (custom CA bundle via
  `SSL_CERT_FILE`).

---

## [0.3.0] — 2026-05-03

The "drop the Emby specificity, generalize to any media server" release.
Renames the project from `babel-tower-emby` to `subtitle-this`, abstracts
the Emby-only client into a server-agnostic protocol with three backends,
and removes the auto-trigger / curl-driven surface in favor of a strictly
manual UI-driven workflow.

### Added
- **Multi-server support: Emby, Jellyfin, Plex** (`9d0d738`, `ca2468a`).
  Replaced `app/emby/` with `app/server/`, defining a `MediaServerClient`
  ABC plus neutral `MediaItem`, `MediaPage`, `MediaStream` dataclasses.
  `EmbyJellyfinClient` covers both Emby and Jellyfin (their REST APIs are
  functionally identical — Jellyfin keeps Emby's auth header for legacy
  compat). `PlexClient` is a separate implementation for Plex's
  `X-Plex-Token` auth and `/library/sections` + `/library/metadata/{ratingKey}`
  endpoint structure.
- **Multi-select batch action on Library** (`ab7db01`). Tick checkboxes on
  multiple rows, click *Subtitle selected*, queue them all in one shot. Backend
  endpoint `POST /api/batch` accepts repeated `item_id` form fields.
- **Untagged audio language detection + write-back** (`5d4ff8a`). When ffprobe
  reports an audio track with no language tag (Emby just shows "Audio"),
  Subtitle This now runs a `faster-whisper-tiny` pre-pass on the first 30s
  to detect the language so NLLB and DeepL get the right `source_lang`.
  After the .vtt is written, the detected language is also persisted back
  into the file's audio-stream metadata via `mkvpropedit` (Matroska only —
  see "Removed" below for the rationale on dropping the ffmpeg path).
- **Cost-ladder hierarchy in Settings** (`c917490`). Hero card on the
  Settings page that lays out the 5 tiers from "NLLB + audio (free)" to
  "LLM + cinematic (most expensive)". Settings reorganized so the simplest/
  free combination is the default that requires no setup beyond Emby URL.
- **NLLB CPU fallback** (`cd3398a`). NLLB previously required the
  openvino-flavored image (optimum-intel for OpenVINO acceleration). Added
  a plain-PyTorch transformers fallback so the default `nllb` provider works
  on the CPU image too. Slower (~5-10 min for a 1000-cue film) but
  zero-setup either way.
- **CHANGELOG file** (this file).

### Changed
- **Project rename: `babel-tower-emby` → `subtitle-this`** (`5828d16`).
  Reflects the generalized scope: no longer Emby-specific. Repo URL,
  pyproject package name, FastAPI title, GHCR image path, h1, favicon
  emoji (🗼 → 🎬), tagline all updated. Migration path for existing GHCR
  pulls documented in the README.
- **Default translation provider: `llm` → `nllb`** (`cd3398a`).
  Out-of-the-box default now requires no API key, no account, no setup —
  just works on both image flavors with the bundled NLLB-200 model
  (downloaded once on first call).
- **Cache key now includes LLM model ids** (`0a3a53e`). Switching the
  configured translation LLM (e.g. claude-opus-4-7 → gpt-4o → qwen2.5:72b)
  used to silently serve stale translations from the previous model.
  Cache keys now include translation_llm_model and vision_llm_model where
  relevant.
- **Descriptive dropdown labels** (`cf37d39`). Provider, mode, and STT
  backend dropdowns gained `[BADGE]` labels showing cost/complexity
  consequences (e.g. `nllb · [FREE · LOCAL]`, `cinematic · [+1 LLM call/cue]`).
- **Documentation reframed as LLM-agnostic** (`b3b4b12`). README, settings
  help text, and inline docstrings rewritten to drop the Claude-specific
  framing that suggested Anthropic was THE engine. The `anthropic` wire-
  protocol value remains; the documentation now treats it as one option
  among many.

### Removed
- **Emby webhook receiver** (`592dd3f`). Subtitle creation is now exclusively
  a manual user action through the web UI — no auto-trigger on `ItemAdded`.
  Removed the `/webhook/emby` endpoint, the `webhook_secret` setting, and
  all related plumbing. A regression test pins it to 404.
- **`POST /transcribe-translate` endpoint** (`592dd3f`). The path-based curl
  endpoint had no UI counterpart. Subtitle creation now goes through the
  per-item or batch UI flows only. Regression test pins it to 404.
- **ffmpeg-based metadata write-back for non-MKV files** (`bddd17a`). The
  `mkvpropedit` MKV path is genuinely surgical (edits only the EBML header,
  never touches audio data). The ffmpeg `-c copy` remux path for MP4/MOV/AVI
  was "mostly safe" but had documented edge cases (timestamp re-derivation,
  lost custom metadata, full-I/O write window). Restricted to MKV only.
  Detection still runs for non-MKV files, only the persist-to-server step
  is skipped.

---

## [0.2.0] — 2026-05-02

Initial commit. Project bootstrap — at the time named `babel-tower-emby`,
focused on Emby only, with audio/scene/cinematic modes, Whisper STT (CPU +
OpenVINO), Anthropic-native + OpenAI-compatible LLM dispatch, DeepL +
NLLB-200 alternative providers, Docker images for both flavors, and a basic
HTMX-driven web UI.
