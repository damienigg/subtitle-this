# Subtitle This

Auto-generates target-language subtitles for media in your **Emby, Jellyfin, or Plex** library — YouTube auto-caption style — using Whisper for speech-to-text and an LLM of your choice for translation. Single-service: a single Python/FastAPI app (Docker) that talks to your media server's REST API directly. No server plugin to install.

**LLM-agnostic and server-agnostic by design.** All configuration lives in the web UI; nothing requires editing files or restarting containers. Defaults are the cheapest, no-setup combination — the only thing you need to fill in to make subtitles is your media server URL + API key.

## Architecture

```
                           ┌──────────────────────────────────────────┐
                           │ subtitle-this (FastAPI, Docker)          │
   user (web UI) ─────────▶│  Web UI (Jinja2 — settings, library,     │
                           │            jobs, cache explorer)         │
                           │  Server-agnostic media client            │
   Media server ◀──────────│  ├─ EmbyJellyfinClient (X-Emby-Token)    │
   (Emby / Jellyfin /      │  └─ PlexClient        (X-Plex-Token)     │
    Plex; path resolve,    │  Pipeline: ffprobe + ffmpeg              │
    metadata refresh)      │     → audio prep (FC pan + loudnorm)     │
                           │     → optional Demucs vocal isolation    │
                           │     → Whisper (CPU or OpenVINO iGPU)     │
                           │     → confidence-gated refine pass       │
                           │     → anti-hallucination filter          │
                           │     → LLM / DeepL / NLLB translation     │
                           │     → readability polish + WebVTT writer │
   media volume (rw) ◀─────│  (writes Movie.<lang>.ai.vtt)            │
                           └──────────────────────────────────────────┘
```

**Subtitle creation is exclusively a manual user action through the web UI.** No webhook, no auto-trigger on `ItemAdded`, no path-based curl endpoint, no whole-library "subtitle everything" button. Every subtitle is the result of a deliberate user click on a specific item or a specific batch.

## The default path (no setup beyond your media server)

Out of the box, Subtitle This runs in this configuration:

- **Translation**: NLLB-200 — free, local, no API key, no account
- **Whisper backend**: openvino on the openvino-flavored image, cpu otherwise
- **Audio-only pipeline**: Whisper transcribes, NLLB translates, no LLM calls
- **Output filename**: `Movie.fr.ai.vtt` next to the source media

This is what you get with zero configuration past the media server URL + API key. NLLB downloads its 1.5 GB model on first translation; subsequent jobs use the cached model and run offline.

You only need to configure anything beyond the media server credentials if you want **better translation quality** (LLM or DeepL — see [Advanced: upgrading from defaults](#advanced-upgrading-from-defaults)).

### Automatic quality improvements (no toggle needed)

Six pipeline features run automatically when conditions are right, with no setting to flip:

| Feature | When it fires |
| --- | --- |
| **Center-channel extraction** | 5.1+ sources — ffmpeg `pan=mono\|c0=FC` pulls the dialogue-only front-center channel, skipping the need for Demucs |
| **EBU R128 loudness normalization** | Always — brings audio to −23 LUFS, Whisper's training-data range |
| **Anti-hallucination filter** | Always — drops YouTube-tail signature phrases ("Thanks for watching", "Merci d'avoir regardé") + n-gram stuck-loop repetitions |
| **Confidence-gated re-transcription** | `whisper_backend=cpu` — walks the first-pass output, re-decodes weak 10-min buckets with aggressive params, capped at 20 % audio budget |
| **Word-level timestamps** | `whisper_backend=cpu` — DTW alignment gives per-word timing accuracy |
| **Orphan-word line breaks** | Always — VTT writer avoids ending a line on "of", "the", "de", "la", and similar function words |

The Settings page shows which of these are currently active for your configuration in an "Active automatic improvements" banner at the top.

## What you do as a user

1. Bring up the container (see [Quick start](#quick-start) below).
2. Open `http://<host>:8765/`.
3. **First run only**: the onboarding wizard walks you through 3 steps — pick your server type (Emby / Jellyfin / Plex), paste URL + API key (X-Plex-Token for Plex), choose your default subtitle language + translation provider, and you're done. Power users can skip the wizard via the link in its header and go straight to the full Settings page.
4. Library page → browse your server's items. Click *Subtitle this* on a row, or tick checkboxes on multiple rows and hit *Subtitle selected* (selection persists across pages).
5. Watch the dashboard — jobs auto-refresh every 3 seconds, complete with a Quality score per run.

That's the entire flow.

## What you get

- **Free-by-default subtitle generation**: Whisper STT (local) + NLLB-200 translation (local, ~30 languages out of the box). No API keys required to make subtitles.
- **Optional better quality** via DeepL (500k chars/mo free) or any LLM endpoint (Claude / GPT / local Ollama / LM Studio — anything OpenAI-compatible).
- **Quality observability built in** — every finished run produces a stats record with VAD coverage, region-packing diagnostics, Whisper hallucination counters, translation char-ratios, plus a heuristic 0-100 Quality Score with a per-factor breakdown. No more "did the subtitle generation work?" guessing.
- **Cache Explorer** UI page — list / inspect / delete / download stats per cached subtitle. Re-runs no longer require SSH into the host to find the right hashed filename.
- **Per-job stats page** linked from the Jobs table's Quality pill — see exactly which pathology cost which points, with inline thresholds explaining what the numbers mean.
- **Crash-resilient job tracking**: jobs persist to disk, so an OOM or container restart leaves a clear trace ("failed at 80 % translating") instead of silently evaporating.
- **Settings migration framework** — version bumps automatically clean up renamed/dead fields in your `settings.json`, with a clear log line at startup. You don't have to maintain config across releases.
- **Single Docker service**, plain HTML+HTMX UI, no plugin to install on your media server.

## Supported media servers

| Server | Implementation | Auth | Where to get the credential |
| --- | --- | --- | --- |
| **Emby** | `EmbyJellyfinClient` (shared) | `X-Emby-Token` header | Emby admin → Server Settings → Advanced → API Keys → New API Key |
| **Jellyfin** | `EmbyJellyfinClient` (shared) | `X-Emby-Token` header (legacy compat) | Dashboard → Advanced → API Keys → "+" |
| **Plex** | `PlexClient` | `X-Plex-Token` header | Sign in at app.plex.tv, browse to a local-server URL, copy the `X-Plex-Token=…` from any request in DevTools |

Pick the server type in Settings → Media server → Server type. The library browser, per-item buttons, and batch flow all behave identically regardless of which server backs them.

## Quick start (any docker host)

```sh
git clone https://github.com/damienigg/subtitle-this.git
cd subtitle-this
mkdir -p ./cache && sudo chown -R 568:568 ./cache    # see "TrueNAS dataset perms" below
docker compose up -d
```

Open `http://localhost:8765/` and configure the Media server section. That's it — defaults handle the rest.

The compose file uses the openvino-flavored image by default. Override `IMAGE_FLAVOR=cpu` for the CPU-only image (works on any x86_64 host without Intel iGPU).

The web UI shows three pages:
- **Dashboard** (`/`) — status pills + recent jobs (auto-refreshing).
- **Library** (`/library`) — browse, search, filter to "missing target-lang sub", per-item *Subtitle this* button, multi-select for batch (selection persists across pages).
- **Settings** (`/settings`) — every editable parameter. Sections hide themselves when not relevant given the current Defaults config.

Settings persist to `/cache/settings.json` and override env defaults from compose.

## Creating subtitles

Two flows, both on the Library page:

### One item

1. Open `/library`.
2. Optional: filter to *missing target-language subtitle*.
3. Click *Subtitle this* on the row. A job appears on the dashboard's *Recent jobs* table and processes in the background.

### A custom batch

1. Open `/library`.
2. Tick the checkbox on each row you want subtitled. Selection persists across pagination AND page reloads (stored in `localStorage` under `subtitleThis.batchSelection`).
3. Click *Subtitle selected* in the sticky toolbar above the table.

After a job finishes the result is cached, keyed by file fingerprint + target lang + provider + STT model + translation LLM model id (when provider = llm) + VAD-enabled flag (OpenVINO only). The cache also survives mtime bumps, file renames, and our own metadata write-back step via a content-fingerprint fallback.

## Quality observability

Every finished run produces a JSON stats record persisted in
`<cache_dir>/stats/<cache_key>.json`. The same data is rendered on
the **Cache** tab in the web UI, with a per-row 📊 button that opens
the full breakdown — duration histogram, per-10-min coverage buckets,
audio-prep routing (FC pan vs downmix, loudnorm), vocal-isolation
metrics (when run), VAD speech ratio + region distribution, region-
packing pad-drop / snap-recovery counts, Whisper degenerate-timestamp
drops + refine-pass effect (buckets evaluated / weak / refined),
anti-hallucination splits, polish merge/extend counts, translation
char-ratio / empty / duplicate counts, plus a heuristic 0-100
**Quality Score** with a per-factor table that explains which
pathology cost which points.

The Jobs table on the Dashboard surfaces the Quality Score as a
color-coded pill (A green … F red) next to the Output pill. Click
it to open the same breakdown page directly from the job — no need
to find the matching Cache Explorer row.

Why this matters: "completed" doesn't mean "correct". A run can
finish without error but silently drop 40 % of dialog because of
a Whisper-timestamp / region-packing interaction (this happened
to Inception in 0.7.0 — see CHANGELOG). The Quality Score detects
the known pathologies and tells you BEFORE you discover the gap
during movie night.

The score is a pipeline-health heuristic, not a measure of
translation correctness — we have no ground truth to compare
against. A score of 95 means "no red flags in the pipeline's
behavior"; a score of 50 means "the pipeline mis-behaved in
known ways — go look at the breakdown".

## Generating a media-server API key / token

- **Emby** — Server admin → Server Settings → Advanced → API Keys → New API Key.
- **Jellyfin** — Dashboard → Advanced → API Keys → "+".
- **Plex** — Sign in at app.plex.tv. Open any local-server URL. In browser DevTools (Network tab), copy the `X-Plex-Token=…` query parameter or header from any request to your server.

## Deploying on TrueNAS Scale

TrueNAS Scale 24.10+ runs Docker natively. Two deployment paths:

### Path A — build locally (simplest)

SSH (or Web Shell at `https://<truenas>/ui/shell`) into TrueNAS, clone this repo to a dataset, and run docker-compose:

```sh
ssh admin@truenas      # or use the Web Shell
cd /mnt/<pool>/apps
git clone https://github.com/damienigg/subtitle-this.git
cd subtitle-this
mkdir -p ./cache && sudo chown -R 568:568 ./cache
export RENDER_GID=$(getent group render | cut -d: -f3)
# Edit docker-compose.yml: change `/mnt/media:/Movies:rw` to YOUR media path
sudo docker compose up -d
```

Open `http://<truenas-ip>:8765/` and configure from Settings.

### Path B — pre-built image from GHCR

The repo ships a GitHub Actions workflow that builds + pushes both image flavors to GHCR on every push to `main` and on version tags. Use this for paste-into-TrueNAS-Custom-App deployments.

1. Fork the repo, push to GitHub. Workflow runs automatically and creates two images at `ghcr.io/<your-username>/subtitle-this`.
2. Make the package public so the daemon can pull without auth: GitHub profile → Packages → click `subtitle-this` → Package settings → Change visibility → Public.
3. Edit `docker-compose.yml`: comment out the `build:` block, uncomment the `image:` line. Set `GHCR_OWNER` to your GitHub username/org.

**Image tags published:**

| Tag | What | Retention |
| --- | --- | --- |
| `:openvino` / `:cpu` | Moving "latest from main branch" pointer per flavor | Always (only most recent build kept) |
| `:1.2.3-openvino` | Pinned release (when you push a `v1.2.3` git tag) | **Forever** — protected by the retention regex |
| `:1.2-openvino` | Floating major.minor | **Forever** |
| `:openvino-sha-abc1234` | Per-commit SHA tag | Pruned by the next main-branch build |

GHCR retention runs after every main-branch build (`actions/delete-package-versions@v5`, `min-versions-to-keep: 12` to preserve the multi-arch sub-manifests). Release-tagged versions are explicitly excluded from pruning.

### TrueNAS dataset write permissions

Subtitle This runs as the **`app` user (UID/GID 568)** inside the container — chosen to match TrueNAS Scale 24.10+'s `apps` user. On TrueNAS, dataset reads/writes Just Work without ACL fiddling.

Two host-side directories need to be writable by UID 568:
1. **The cache bind-mount** (`./cache`) — model downloads + transcript cache + `settings.json`
2. **The media library** — for writing `.vtt` files next to source media

If your host isn't TrueNAS or your media dataset isn't owned by `apps`, set `PUID`/`PGID` in `.env` to match your dataset owner:

```sh
PUID=1000 PGID=1000 docker compose up -d
```

Note: `group_add: ${RENDER_GID:-107}` in compose adds the host's `render` group to the container so the non-root `app` user can access `/dev/dri/renderD128` for OpenVINO iGPU inference. TrueNAS Scale 24.10+ uses GID 107; vanilla Ubuntu uses 109.

---

## Advanced: upgrading from defaults

Everything below is **optional**. Skip if NLLB + audio + your media server already meets your needs.

### Better translation quality: switch provider

Three providers, picked from Settings → Translation → *Translation provider*:

| Provider | Quality | Cost | Where it runs |
| --- | --- | --- | --- |
| `nllb` (default) | Fair-to-good. ~30 well-supported languages. | Free, offline | Local (OpenVINO when available, CPU torch fallback otherwise) |
| `deepl` | Excellent for EU/Asian pairs. Text-only, no cross-cue context. | Free tier 500k chars/mo, paid above | Cloud (DeepL API) |
| `llm` | Best — uses whichever LLM you configure. Idioms, nuance, cross-cue context. | Pay-per-token (cloud) or free (local LLM) | Whatever you point it at |

Switching to LLM unhides the *Translation model* section in Settings.

### LLM configuration recipes

Once you switch the Translation provider to LLM, the *Translation model* section appears with four fields: Wire protocol, Endpoint URL (if openai_compat), Model, API key. Common combinations:

| Setup | Wire protocol | Endpoint | Model | API key |
| --- | --- | --- | --- | --- |
| Anthropic cloud | `anthropic` | *(ignored)* | `claude-opus-4-7` | your Anthropic key |
| OpenAI cloud | `openai_compat` | `https://api.openai.com/v1` | `gpt-4o-mini` | your OpenAI key |
| Local Ollama | `openai_compat` | `http://ollama:11434/v1` | `qwen2.5:72b` | *(blank)* |
| LM Studio on host | `openai_compat` | `http://host.docker.internal:1234/v1` | model id from LM Studio | *(blank)* |
| OpenRouter | `openai_compat` | `https://openrouter.ai/api/v1` | `deepseek/deepseek-chat` | your OR key |

**Local servers (Ollama, LM Studio, LocalAI) don't authenticate by default — leave the API key blank.** The OpenAI SDK requires a non-empty key, so Subtitle This substitutes a placeholder transparently.

### Vocal isolation (Demucs)

For score-heavy films where dialogue is buried under music (Inception,
Dunkirk, Tenet), turn on Settings → Speech-to-Text → *Vocal isolation*:

| Mode | Peak RAM | Recommended for |
| --- | --- | --- |
| `off` (default) | n/a | Dialog-driven dramas, 5.1+ sources (center-channel extraction does the job for free) |
| `chunked` | ~1 GB | Any film, any host — safe under a 6 GB cgroup |
| `full` | scales with length × num_stems | Hosts with ≥ 12 GB free RAM; marginal quality bump over chunked |

The Demucs model identifier (`htdemucs` light, `htdemucs_ft` heavy) is
hidden from the UI — set `BABEL_VOCAL_ISOLATION_MODEL` to override the
`htdemucs` default. Demucs is an opt-in extra; install via
`pip install subtitle-this[vocal-isolation]` or use the shipped Docker
images, both of which include it.

### STT backend choice

Two backends, in Settings → Speech-to-Text → Backend:

- **`cpu`** — `faster-whisper` with INT8 quantization. Works on any host. ~20–60 min for a 2h film on small/medium model.
- **`openvino`** — Whisper exported to OpenVINO IR, runs on Intel iGPU. 5–10× faster than CPU on N305-class hardware. Requires the openvino-flavored image with `/dev/dri` exposed.

The openvino device picker is hidden — defaulted to `AUTO` which picks GPU when available and falls back to CPU. Power users can override via `BABEL_OPENVINO_DEVICE`.

### Custom CA bundle for self-signed media-server TLS

If your media server has a self-signed certificate but you want to keep verification on (e.g. you have your own CA):

1. Mount the CA into the container: `- /path/to/ca.crt:/cache/my-ca.crt:ro`
2. Set `SSL_CERT_FILE=/cache/my-ca.crt` in the env. httpx picks it up automatically.

The "Verify SSL certificate" toggle stays on. This is the recommended path over the toggle-off-on-trusted-LAN approach.

### Resource safety — keeping a long film from OOMing your host

A 2 h+ film with Whisper-medium + NLLB-1.3B + vocal-isolation FULL can
peak around 5–8 GB resident with no cgroup fence — enough to push a
TrueNAS host into kernel-OOM territory if ZFS ARC and other apps are
competing. Three layers of mitigation ship in the box:

1. **Container-level cgroup limits** (`docker-compose.yml`): `mem_limit: 6g`,
   `memswap_limit: 6g` (no swap escape — container OOMs alone), `cpus: "4.0"`,
   `pids_limit: 1024`. The kernel enforces these, so the host stays up
   regardless of any bug above. Bump them if you switched to
   `whisper-medium`/`large-v3` or NLLB-1.3B+; check `docker stats subtitle-this`
   during a real run to size your headroom.

2. **In-process caps** that reduce the chance of ever hitting (1):
   - **Audio segmentation** (`stt_audio_segment_seconds`, default 600 s)
     reads the wav in 10-minute chunks instead of one 500 MB buffer. Peak
     audio RAM stays ~75 MB regardless of film length.
   - **Vocal isolation chunking** (`vocal_isolation_mode=chunked`,
     `vocal_isolation_chunk_seconds`, default 300 s) caps Demucs peak
     RAM at ~1 GB regardless of film length. FULL mode trades RAM for
     marginally cleaner seams and is opt-in.
   - **Confidence-gated re-pass budget** — the refine phase that
     re-decodes weak buckets is capped at 20 % of audio total, so a
     pathological first pass can't blow the wall-clock budget.
   - **Whisper model release** between STT and translation — the
     processor evicts Whisper weights before NLLB / LLM loads so the
     two ML models don't sit resident simultaneously.
   - **Job timeout** (`job_timeout_seconds`, default 5400 = 90 min) kills
     a wedged job at any pipeline checkpoint, releasing the queue lock.
     Set to 0 to disable. A timeout shows up as `failed` with `timeout: …`
     in the UI.

3. **BLAS / OMP thread caps** in both Dockerfiles (`OMP_NUM_THREADS=4` etc.)
   so torch + numpy + transformers don't each spawn `os.cpu_count()` workers
   on top of each other.

Default settings are sized for one job at a time on the openvino + small
Whisper + NLLB-600M default stack. If you raise model sizes, raise the
cgroup limits to match.

### Optional auth (`BABEL_AUTH_CREDENTIALS`)

Subtitle This ships with no auth by default — that's the right call on a
trusted LAN and preserves the zero-config first-boot path. On any network
where you wouldn't trust every device, set `BABEL_AUTH_CREDENTIALS=user:password`
(or save it from Settings → Security). Every endpoint except `/health`
then requires Basic auth, and state-changing methods (POST/PATCH/PUT/DELETE)
additionally check that the request's `Origin`/`Referer` matches `Host` — so
a malicious page on another LAN host can't ride your saved browser
credentials to queue jobs that burn your LLM quota. Direct API clients
(curl, scripts) authenticate via Basic alone, since they aren't subject
to CSRF.

### Untagged audio language detection + write-back

When ffprobe reports a track without a language tag (e.g. Emby just shows "Audio"), Subtitle This runs a `faster-whisper-tiny` pre-pass on the first 30s of audio (~75 MB model cached on first use, ~2-3s per job). The detected language drives transcription so NLLB and DeepL get the right `source_lang`.

**The detected language is also written back into the source file's audio-stream metadata** via `mkvpropedit` so the media server reads the right language on next probe. Restricted to MKV/MKA/WebM — `mkvpropedit` edits ONLY the EBML header, never touches audio data, no risk of audio damage. Disabled via Settings → Defaults → *Tag detected source language back into the source file*.

Non-Matroska containers (MP4 / MOV / AVI / …) are deliberately left untouched: an `ffmpeg -c copy` remux would technically preserve audio byte-for-byte but rewrites the whole file with documented edge cases (timestamp re-derivation, lost custom metadata) — not worth the risk on a media library.

---

## Power-user knobs (env vars)

All configurable from the Settings UI; these env vars only matter for declarative deployments (TrueNAS Custom App YAML, GitOps, etc.).

| Variable | Default | Notes |
| --- | --- | --- |
| `BABEL_MEDIA_SERVER_TYPE` | `emby` | `emby` / `jellyfin` / `plex` |
| `BABEL_MEDIA_SERVER_URL` | (unset) | Server base URL |
| `BABEL_MEDIA_SERVER_API_KEY` | (unset) | Emby/Jellyfin API key, or X-Plex-Token for Plex |
| `BABEL_MEDIA_SERVER_VERIFY_SSL` | `true` | Disable for Plex via LAN IP or self-signed certs |
| `BABEL_WHISPER_BACKEND` | `cpu` | `cpu` / `openvino` (the openvino image sets this to `openvino`) |
| `BABEL_WHISPER_MODEL` | `small` | `tiny` / `base` / `small` / `medium` / `large-v3` / `large-v3-turbo` |
| `BABEL_WHISPER_COMPUTE_TYPE` | `int8` | `int8` / `int16` / `float16` / `float32` — cpu backend only |
| `BABEL_OPENVINO_DEVICE` | `AUTO` | `AUTO` / `GPU` / `CPU` (UI doesn't expose this; AUTO is right) |
| `BABEL_VAD_ENABLED` | `true` | Silero VAD pre-filter (openvino backend only) |
| `BABEL_VOCAL_ISOLATION_MODE` | `off` | `off` / `chunked` / `full` — Demucs vocal isolation phase |
| `BABEL_VOCAL_ISOLATION_CHUNK_SECONDS` | `300` | Chunk length when mode = `chunked` |
| `BABEL_VOCAL_ISOLATION_MODEL` | `htdemucs` | Demucs model id — UI-hidden power-user knob |
| `BABEL_DEFAULT_TRANSLATION_PROVIDER` | `nllb` | `nllb` / `deepl` / `llm` |
| `BABEL_DEFAULT_TARGET_LANG` | `fr` | ISO 639-1 |
| `BABEL_DEFAULT_SOURCE_LANG_PRIORITY` | `["en","*"]` | UI-hidden niche knob |
| `BABEL_NLLB_MODEL` | `facebook/nllb-200-distilled-600M` | NLLB variant |
| `BABEL_NLLB_BATCH_SIZE` | `4` | Cues per NLLB generate() call |
| `BABEL_NLLB_LOAD_IN_8BIT` | `true` | int8 NLLB weights (OpenVINO path) — halves resident RAM |
| `BABEL_TRANSLATION_LLM_TYPE` | `anthropic` | `anthropic` / `openai_compat` |
| `BABEL_TRANSLATION_LLM_MODEL` | `claude-opus-4-7` | Translator model id |
| `BABEL_TRANSLATION_LLM_ENDPOINT` | `https://api.openai.com/v1` | Endpoint when type=openai_compat |
| `BABEL_TRANSLATION_LLM_API_KEY` | (unset) | |
| `BABEL_DEEPL_API_KEY` | (unset) | Free-tier keys end in `:fx` (auto-detected) |
| `BABEL_DEEPL_BATCH_SIZE` | `50` | Cues per DeepL request (capped at 50 by the API) |
| `BABEL_TRANSLATION_BATCH_SIZE` | `30` | Cues per LLM call |
| `BABEL_DEFAULT_SKIP_IF_TARGET_AUDIO_EXISTS` | `true` | Skip when target-lang audio already in file |
| `BABEL_WRITE_DETECTED_LANGUAGE_TO_FILE` | `true` | MKV-only language tag write-back |
| `BABEL_MAX_LINE_CHARS` | `42` | Subtitle line wrap |
| `BABEL_MAX_LINES_PER_CUE` | `2` | Max display lines per cue |
| `BABEL_POLISH_ENABLED` | `true` | Readability polish — extend short cues, merge fragments |
| `BABEL_MIN_CUE_DURATION_SECONDS` | `1.2` | Min display duration after polish |
| `BABEL_MIN_SECONDS_PER_CHAR` | `0.045` | Reading-speed floor (≈ 22 chars/sec) |
| `BABEL_MERGE_ADJACENT_CUES` | `true` | Collapse adjacent fragments that visually read as one cue |
| `BABEL_MAX_GAP_TO_MERGE_SECONDS` | `0.3` | Max silence between merge candidates |
| `BABEL_MAX_MERGED_CUE_DURATION_SECONDS` | `7.0` | Hard cap on a merged cue's on-screen duration |
| `BABEL_CUE_SEPARATION_SECONDS` | `0.125` | Min gap between cues after polish (≈ 3 frames @ 24 fps) |
| `BABEL_CACHE_DIR` | `/cache` | Cache directory |
| `BABEL_JOB_TIMEOUT_SECONDS` | `5400` | Wall-clock cap per job; `0` disables |
| `BABEL_STT_AUDIO_SEGMENT_SECONDS` | `600` | OpenVINO STT audio-segment size (RAM cap knob) |
| `BABEL_STT_SEGMENT_OVERLAP_SECONDS` | `30` | Forward overlap on segment reads so straddling regions stay intact |
| `BABEL_STT_REGION_PACKING` | `true` | Pack short speech regions into shared 30 s decoder windows (perf) |
| `BABEL_STT_MAX_REGIONS_PER_WINDOW` | `4` | Density cap for region packing |
| `BABEL_AUTH_CREDENTIALS` | (unset) | `user:password` for HTTP Basic auth; unset = no auth |
| `BABEL_UPDATE_COMMAND` | (unset) | Shell command for the dashboard's "Update now" button; unset hides it |

## Tests

```sh
pip install -e .[dev]
pytest
```

500 tests covering pure-logic (language normalization, VTT formatting + orphan-word breaks, track selection, settings store with migrations + atomic writes + corrupt-file recovery, batching, cache key invalidation, two-level cache fingerprint, transcript-cache rehydration, Emby/Jellyfin payload parsing, Plex API translation, LLM translation provider edge cases, anti-hallucination filter + safety bailout, confidence-gated refine pass, polish merge/extend, audio-prep filter routing, pipeline-metrics serialization, UI form coercion + show-if rules, job deadline enforcement, atomic-write + JSON-quarantine helpers) plus FastAPI smoke tests for every route and the auth + same-origin middleware. Heavy externals (ffmpeg, Whisper, Demucs, LLM/server APIs) are stubbed — the suite runs in ~10 s.

## Layout

```
subtitle-this/
├── .github/workflows/
│   ├── publish.yml                 GHCR multi-flavor image publish + retention
│   └── prune-ghcr.yml              Manual GHCR cleanup workflow
├── .env.example
├── docker-compose.yml
├── Dockerfile                      CPU image (faster-whisper)
├── Dockerfile.openvino             Intel iGPU image
├── pyproject.toml
├── README.md
├── CHANGELOG.md
└── app/
    ├── main.py                     FastAPI entry + lifespan
    ├── auth.py                     Optional HTTP Basic + same-origin CSRF middleware
    ├── config.py                   Layered settings + migrations + atomic persist
    ├── cache.py                    Two-level VTT cache (quick fp + content fp)
    ├── cache_explorer.py           Cache enumeration for the UI
    ├── transcript_cache.py         Whisper-output cache (skips STT on translation retry)
    ├── jobs.py                     In-memory job queue + per-job wall-clock timeout
    ├── jobs_store.py               Persisted jobs queue (survives restart)
    ├── pipeline_metrics.py         Per-run telemetry dataclasses + aggregators
    ├── processor.py                Pipeline orchestrator
    ├── quality.py                  Heuristic 0-100 Quality Score from pipeline metrics
    ├── stats.py                    VTT-derived stats + .stats.json sidecar writer
    ├── updates.py                  In-app update banner + GitHub release check
    ├── util.py                     atomic_write + JSON-quarantine helpers
    ├── api/
    │   ├── manage.py               Server-driven endpoints (jobs, library, cache)
    │   └── settings_api.py         GET/PATCH /api/settings
    ├── server/
    │   ├── base.py                 MediaServerClient ABC + neutral dataclasses
    │   ├── emby_jellyfin.py        Shared Emby/Jellyfin client
    │   └── plex.py                 Plex client
    ├── ui/
    │   └── routes.py               HTML routes + _FIELD_META + _SECTION_SHOW_IF
    ├── templates/
    │   ├── base.html               Shared layout + CSS
    │   ├── cache_explorer.html
    │   ├── cache_stats.html        Per-entry quality-stats page
    │   ├── dashboard.html
    │   ├── job_error.html
    │   ├── _jobs_table.html        HTMX-polled jobs partial
    │   ├── library.html
    │   ├── onboarding.html         First-run 3-step wizard
    │   └── settings.html
    └── pipeline/
        ├── tracks.py               ffprobe-based audio-track selection
        ├── audio.py                ffmpeg audio extraction (FC pan + loudnorm)
        ├── vocal_isolation.py      Demucs vocal-isolation phase
        ├── lang.py                 Language code normalization + dropdown options
        ├── lang_detect.py          faster-whisper-tiny pre-pass for untagged tracks
        ├── track_metadata.py       MKV-only language tag write-back
        ├── vad.py                  Silero VAD wrapper
        ├── packing.py              Speech-region packer (multi-region 30 s windows)
        ├── stt.py                  Whisper-backend dispatcher
        ├── stt_faster_whisper.py   CPU backend (faster-whisper)
        ├── stt_openvino.py         Intel iGPU backend (OpenVINO IR)
        ├── stt_refine.py           0.8.0 confidence-gated re-transcription
        ├── anti_hallucination.py   YouTube-tail + n-gram repetition filter
        ├── polish.py               Readability polish (merge + extend)
        ├── vtt.py                  WebVTT writer + orphan-word line breaks
        ├── openvino_introspect.py  GPU-vs-CPU device selection log
        ├── llm/
        │   ├── base.py
        │   ├── anthropic.py
        │   └── openai_compat.py
        └── translate/
            ├── base.py
            ├── llm.py
            ├── deepl.py
            └── nllb.py
```

See [CHANGELOG.md](CHANGELOG.md) for the full version history.
