# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
follows [Semantic Versioning](https://semver.org/) — though as a 0.x release
expect breaking changes between minor versions until 1.0.

## [Unreleased]

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
