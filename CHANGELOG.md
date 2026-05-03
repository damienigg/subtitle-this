# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
follows [Semantic Versioning](https://semver.org/) — though as a 0.x release
expect breaking changes between minor versions until 1.0.

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
