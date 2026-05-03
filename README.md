# Subtitle This

Auto-generates target-language subtitles for media in your **Emby, Jellyfin, or Plex** library — YouTube auto-caption style — using Whisper for speech-to-text and an LLM of your choice for translation. Single-service: a single Python/FastAPI app (Docker) that talks to your media server's REST API directly. No server plugin to install.

**LLM-agnostic and server-agnostic by design.** All configuration lives in the web UI; nothing requires editing files or restarting containers. Defaults are the cheapest, no-setup combination — the only thing you need to fill in to make subtitles is your media server URL + API key.

## Architecture

```
                           ┌──────────────────────────────────────────┐
                           │ subtitle-this (FastAPI, Docker)          │
   user (web UI) ─────────▶│  Web UI (Jinja2 — settings, library,     │
                           │            jobs)                         │
                           │  Server-agnostic media client            │
   Media server ◀──────────│  ├─ EmbyJellyfinClient (X-Emby-Token)    │
   (Emby / Jellyfin /      │  └─ PlexClient        (X-Plex-Token)     │
    Plex; path resolve,    │  Pipeline: ffprobe + ffmpeg              │
    metadata refresh)      │     → Whisper (CPU or OpenVINO iGPU)     │
                           │     → LLM / DeepL / NLLB                 │
                           │     → WebVTT writer                      │
   media volume (rw) ◀─────│  (writes Movie.<lang>.<mode>.ai.vtt)     │
                           └──────────────────────────────────────────┘
```

**Subtitle creation is exclusively a manual user action through the web UI.** No webhook, no auto-trigger on `ItemAdded`, no path-based curl endpoint, no whole-library "subtitle everything" button. Every subtitle is the result of a deliberate user click on a specific item or a specific batch.

## The default path (no setup beyond Emby)

Out of the box, Subtitle This runs in this configuration:

- **Translation**: NLLB-200 — free, local, no API key, no account
- **Mode**: audio (Whisper transcribes, NLLB translates, no LLM calls)
- **Whisper backend**: openvino on the openvino-flavored image, cpu otherwise
- **Output filename**: `Movie.fr.audio.ai.vtt` next to the source media

This is what you get with zero configuration past the media server URL + API key. NLLB downloads its 1.5 GB model on first translation; subsequent jobs use the cached model and run offline.

You only need to configure anything beyond the media server credentials if you want **better translation quality** (LLM or DeepL — see [Advanced: upgrading from defaults](#advanced-upgrading-from-defaults)) or **scene-aware translation** (scene/cinematic modes — also Advanced).

## What you do as a user

1. Bring up the container (see [Quick start](#quick-start) below).
2. Open `http://<host>:8765/`.
3. Settings page → **Media server** section is at the top: pick your server type (Emby / Jellyfin / Plex), paste URL + API key (X-Plex-Token for Plex), save. Settings persist to disk and apply at runtime — no restart.
4. Library page → browse your server's items. Click *Subtitle this* on a row, or tick checkboxes on multiple rows and hit *Subtitle selected* (selection persists across pages).
5. Watch the dashboard — jobs auto-refresh every 3 seconds.

That's the entire flow.

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

After a job finishes the result is cached, keyed by file fingerprint + target lang + provider + mode + STT model + translation/vision LLM model ids. The cache also survives mtime bumps, file renames, and our own metadata write-back step via a content-fingerprint fallback.

## Quality tiers (modes)

The mode controls how much visual context the translator gets. The tier is encoded in the output filename — `Movie.fr.audio.ai.vtt` vs `Movie.fr.scene.ai.vtt` vs `Movie.fr.cinematic.ai.vtt` — so multiple tiers can coexist for the same media without overwriting each other.

| Mode | What it does | Cost | Time |
| --- | --- | --- | --- |
| `audio` | Whisper transcript → text translator. Speech only. | $ | Whisper-bound only |
| `scene` | Adds an LLM-vision scene bible (one description per shot) for pronoun/gender disambiguation. | $$ | + scene detection (5–15 min) + ~20 vision calls |
| `cinematic` | Everything `scene` does **plus** one keyframe per cue attached to translation calls. The translator literally sees what's on screen for each line. | $$$ | + per-cue frame extraction + many more API calls |

**Default is `audio`.** scene and cinematic require translation provider = `llm` with a vision-capable model — see [Advanced: upgrading from defaults](#advanced-upgrading-from-defaults).

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

Three providers, picked from Settings → Defaults → *Translation provider*:

| Provider | Quality | Cost | Where it runs |
| --- | --- | --- | --- |
| `nllb` (default) | Fair-to-good. ~30 well-supported languages. | Free, offline | Local (OpenVINO when available, CPU torch fallback otherwise) |
| `deepl` | Excellent for EU/Asian pairs. Text-only, no cross-cue context. | Free tier 500k chars/mo, paid above | Cloud (DeepL API) |
| `llm` | Best — uses whichever LLM you configure. Idioms, nuance, cross-cue context, vision-aware in scene/cinematic. | Pay-per-token (cloud) or free (local LLM) | Whatever you point it at |

Switching to LLM unhides the *Translation model* section in Settings.

### Scene-aware translation: switch mode

Mode `scene` improves pronoun and gendered-agreement decisions by feeding the translator a per-shot scene bible (built from keyframes via your Vision LLM). Mode `cinematic` additionally attaches a frame to each translation call.

Both require provider = `llm`. Switching mode to scene/cinematic in Defaults unhides the *Vision model* and *Scene & Cinematic* sections in Settings.

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

For scene/cinematic, configure the Vision model section similarly. A common pattern: cloud LLM for translation + local Ollama running `qwen2.5-vl:72b` for vision (vision is the slot that benefits most from a strong specialized model).

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
| `BABEL_OPENVINO_DEVICE` | `AUTO` | `AUTO` / `GPU` / `CPU` (UI doesn't expose this; AUTO is right) |
| `BABEL_DEFAULT_TRANSLATION_PROVIDER` | `nllb` | `nllb` / `deepl` / `llm` |
| `BABEL_DEFAULT_TARGET_LANG` | `fr` | ISO 639-1 |
| `BABEL_DEFAULT_MODE` | `audio` | `audio` / `scene` / `cinematic` |
| `BABEL_DEFAULT_SOURCE_LANG_PRIORITY` | `["en","*"]` | UI-hidden niche knob |
| `BABEL_NLLB_MODEL` | `facebook/nllb-200-distilled-600M` | NLLB variant |
| `BABEL_TRANSLATION_LLM_TYPE` | `anthropic` | `anthropic` / `openai_compat` |
| `BABEL_TRANSLATION_LLM_MODEL` | `claude-opus-4-7` | Translator model id |
| `BABEL_TRANSLATION_LLM_ENDPOINT` | `https://api.openai.com/v1` | Endpoint when type=openai_compat |
| `BABEL_TRANSLATION_LLM_API_KEY` | (unset) | |
| `BABEL_TRANSLATION_LLM_SUPPORTS_VISION` | `true` | Required for cinematic mode |
| `BABEL_VISION_LLM_TYPE` | `anthropic` | `anthropic` / `openai_compat` |
| `BABEL_VISION_LLM_MODEL` | `claude-opus-4-7` | Scene-describer model |
| `BABEL_VISION_LLM_ENDPOINT` | `https://api.openai.com/v1` | |
| `BABEL_VISION_LLM_API_KEY` | (unset) | |
| `BABEL_VISION_LLM_ENABLED` | `true` | Master switch for scene/cinematic |
| `BABEL_DEEPL_API_KEY` | (unset) | Free-tier keys end in `:fx` (auto-detected) |
| `BABEL_SCENE_DETECTION_THRESHOLD` | `0.4` | ffmpeg threshold (0–1, lower = more scenes) |
| `BABEL_SCENE_MIN_LENGTH_SECONDS` | `1.5` | Skip shorter shots |
| `BABEL_SCENE_MAX_SCENES` | `500` | Hard cap |
| `BABEL_SCENE_KEYFRAME_POSITION` | `midpoint` | `start` / `midpoint` / `end` |
| `BABEL_SCENE_FRAME_MAX_SIZE` | `1024` | Long-edge px sent to vision LLM |
| `BABEL_SCENE_BIBLE_BATCH_SIZE` | `10` | Scenes per vision-LLM call |
| `BABEL_CINEMATIC_FRAME_MAX_SIZE` | `768` | Long-edge px for per-cue frames |
| `BABEL_CINEMATIC_BATCH_SIZE` | `10` | Cues per LLM call in cinematic mode |
| `BABEL_TRANSLATION_BATCH_SIZE` | `30` | Cues per LLM call in text-only mode |
| `BABEL_DEFAULT_SKIP_IF_TARGET_AUDIO_EXISTS` | `true` | Skip when target-lang audio already in file |
| `BABEL_WRITE_DETECTED_LANGUAGE_TO_FILE` | `true` | MKV-only language tag write-back |
| `BABEL_MAX_LINE_CHARS` | `42` | Subtitle line wrap |
| `BABEL_MAX_LINES_PER_CUE` | `2` | Max display lines per cue |
| `BABEL_CACHE_DIR` | `/cache` | Cache directory |

## Tests

```sh
pip install -e .[dev]
pytest
```

166 tests covering pure-logic (language normalization, VTT formatting, track selection, scene mapping, settings store with migrations, batching, cache key invalidation, two-level cache fingerprint, Emby/Jellyfin payload parsing, Plex API translation, LLM translation provider edge cases, UI form coercion) plus FastAPI smoke tests for every route. Heavy externals (ffmpeg, Whisper, LLM/server APIs) are stubbed — the suite runs in ~1s.

## Layout

```
subtitle-this/
├── .github/workflows/publish.yml   GHCR multi-flavor image publish + retention
├── .env.example
├── docker-compose.yml
├── Dockerfile                      CPU image (faster-whisper)
├── Dockerfile.openvino             Intel iGPU image
├── pyproject.toml
├── README.md
├── CHANGELOG.md
└── app/
    ├── main.py                     FastAPI entry
    ├── config.py                   Layered settings + migrations
    ├── cache.py                    Two-level fingerprint transcript cache
    ├── jobs.py                     In-memory job queue
    ├── processor.py                Pipeline orchestrator
    ├── api/
    │   ├── manage.py               Server-driven endpoints
    │   └── settings_api.py         GET/PATCH /api/settings
    ├── server/
    │   ├── base.py                 MediaServerClient ABC + neutral dataclasses
    │   ├── emby_jellyfin.py        Shared Emby/Jellyfin client
    │   └── plex.py                 Plex client
    ├── ui/
    │   └── routes.py               HTML routes + _FIELD_META + _SECTION_SHOW_IF
    ├── templates/
    │   ├── base.html
    │   ├── dashboard.html
    │   ├── library.html
    │   └── settings.html
    └── pipeline/
        ├── tracks.py
        ├── audio.py
        ├── lang.py                 Language code normalization + dropdown options
        ├── lang_detect.py          faster-whisper-tiny pre-pass for untagged tracks
        ├── track_metadata.py       MKV-only language tag write-back
        ├── stt.py
        ├── stt_faster_whisper.py
        ├── stt_openvino.py
        ├── scenes.py
        ├── frames.py
        ├── scene_bible.py
        ├── vtt.py
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
