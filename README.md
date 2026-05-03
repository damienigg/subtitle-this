# Babel Tower Emby

Auto-generates target-language subtitles for media in your Emby library — YouTube auto-caption style — using Whisper for speech-to-text and an LLM of your choice for translation. Single-service: a single Python/FastAPI app (Docker) that talks to Emby's REST API directly. No Emby plugin to install.

**LLM-agnostic by design.** You pick the translation engine and (optionally) the vision engine, configure them entirely from the web UI, and Babel Tower abstracts the rest. Cloud (Anthropic / OpenAI / Gemini / OpenRouter / DeepSeek / Zhipu / …) or fully local (Ollama / LM Studio / LocalAI / vLLM / llama.cpp) — same UI, same UX.

## Architecture

```
                           ┌──────────────────────────────────────────┐
                           │ babel-tower-emby (FastAPI, Docker)       │
   user (web UI) ─────────▶│  Web UI (Jinja2 — settings, library,     │
                           │            jobs, sweep)                  │
   Emby Server ◀───────────│  Emby REST client (httpx)                │
   (path resolve,          │  Pipeline: ffprobe + ffmpeg              │
    metadata refresh)      │     → Whisper (CPU or OpenVINO iGPU)     │
                           │     → LLM / DeepL / NLLB                 │
                           │     → WebVTT writer                      │
   media volume (rw) ◀─────│  (writes Movie.<lang>.<mode>.ai.vtt)     │
                           └──────────────────────────────────────────┘
```

**Subtitle creation is exclusively a manual user action through the web UI.** Babel Tower deliberately does NOT expose a webhook receiver, an auto-trigger on Emby's `ItemAdded` events, or a path-based curl endpoint. The two ways to create subtitles, both in the UI:

- **Per item** — open `/library`, find an item, click *Subtitle this*.
- **Whole library** — on the dashboard, click *Sweep library* to queue jobs for every item missing a subtitle in your default target language.

## What you do as a user

The whole point of Babel Tower is that you never edit a config file. You bring up the container, open `http://<host>:8765/`, and configure everything from three pages:

1. **Settings page** — pick your translation engine (LLM or DeepL or NLLB), pick your vision engine (only if you want scene/cinematic modes), paste API keys, point at endpoints. All persisted to disk and applied at runtime — no restart needed.
2. **Library page** — browse Emby items, filter to "missing target-language sub", click *Subtitle this* on a single item, or hit *Sweep* to queue every missing one in the background.
3. **Dashboard** — watch jobs progress in real time, sees Emby/STT/LLM status pills.

That's it. Env vars exist as an optional first-boot fallback for declarative deployments (TrueNAS YAML, etc.) — see the [Power-user knobs](#power-user-knobs) section at the bottom — but the canonical configuration surface is the web UI.

## Quality tiers (modes)

The mode controls how much visual context the translator gets. The tier is encoded in the output filename — `Movie.fr.audio.ai.vtt` vs `Movie.fr.scene.ai.vtt` vs `Movie.fr.cinematic.ai.vtt` — so multiple tiers can coexist for the same media without overwriting each other, and the cache is keyed by tier.

| Mode        | What it does                                                                                                                                                                                                                                       | Cost     | Time                       |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | -------------------------- |
| `audio`     | Whisper transcript → text translator. Speech only.                                                                                                                                                                                                 | $        | Whisper-bound only         |
| `scene`     | Detect shots with ffmpeg's scene filter → extract one keyframe per shot → the configured **Vision LLM** generates a 1–2 sentence "scene bible" → translator gets it as cached system context plus a per-cue scene tag. Improves pronoun/gender/referent disambiguation. | $$       | + scene detection (5–15 min for a 2h film) + ~20 vision calls |
| `cinematic` | Everything `scene` does **plus** one keyframe per cue attached as an image to translation calls. The translator literally sees what's on screen for each line.                                                                                       | $$$      | + per-cue frame extraction (slow, sequential) + many more API calls |

**Defaults are the cheapest tier**: provider = `nllb` (free, fully local), mode = `audio` (no LLM calls beyond translation). The Settings UI is laid out as a cost ladder — climb from free/local at the top to configurable cost at the bottom. Change via Settings → Defaults, or per-request via the API body's `mode` / `translation_provider` fields.

**`scene` and `cinematic` require translation provider = `llm`** with a vision-capable model — they read the keyframes through your Vision LLM and (in cinematic) attach frames to your Translation LLM. The processor refuses with a 400 if you combine them with DeepL or NLLB (text-only providers) or with a translation model you've flagged non-vision.

The scene bible is cached on disk per `(file fingerprint, vision LLM model id, detection threshold)`, so once it's built, switching target language or going from `scene` to `cinematic` reuses it without re-running the vision pass.

## Translation providers

Three providers, picked from Settings → Defaults → *Who translates the cues?*:

| Provider | Quality                                                                                         | Cost                                                | Where it runs              | Languages                |
| -------- | ----------------------------------------------------------------------------------------------- | --------------------------------------------------- | -------------------------- | ------------------------ |
| `llm`    | Best — uses whichever LLM you configured. Idioms, nuance, cross-cue context, vision-aware in scene/cinematic modes. | Pay-per-token (cloud) or free (local LLM).          | Whatever you point it at   | Everything the LLM knows |
| `deepl`  | Excellent for EU langs, best in class on those pairs. Text-only, no cross-cue context.          | Free tier: 500k chars/month (~6 movies). Paid above. | Cloud (DeepL API)          | ~30 (EU + EA majors)     |
| `nllb`   | Fair-to-good. Covers the long tail of languages.                                                | Free, offline.                                      | Local — Intel iGPU via OpenVINO when available, plain CPU torch otherwise | 200 (FLORES-200 set) |

### LLM models — split by function

The `llm` provider and the scene-bible builder talk to LLMs. Babel Tower exposes them as **two function-named slots** in the settings UI — *Translation model* and *Vision model* — each independently configurable, so you can mix-and-match (e.g. cheap fast text model for translation + strong vision model for scene descriptions).

#### Translation model

Translates subtitle cues. In cinematic mode this same model also receives per-cue keyframes — pick a vision-capable one and tick the *Supports vision* checkbox if you plan to use cinematic.

What makes a good translator: large parameter count, broad multilingual training, strong instruction-following.

| Tier                | Cloud                                                            | Open-source — Chinese-strong              | Open-source — general                                       |
| ------------------- | ---------------------------------------------------------------- | ----------------------------------------- | ----------------------------------------------------------- |
| Frontier            | claude-opus-4-7, gpt-4o, gemini-1.5-pro, mistral-large           | qwen2.5:72b, deepseek-v3, glm-4-flash     | llama3.1:70b, llama3.3:70b, command-r-plus                  |
| Mid-tier            | claude-sonnet-4-6, gpt-4o-mini, gemini-2.0-flash                 | qwen2.5:32b, qwen2.5:14b                  | mistral-large, llama3.1:8b, gemma2:27b                      |
| Cheap & fast        | claude-haiku-4-5, gpt-4o-mini                                    | qwen2.5:7b                                | llama3.1:8b, gemma2:9b                                      |

#### Vision model

Builds the scene bible (1-2 sentence description per shot). Used by `scene` and `cinematic` modes.

What makes a good vision describer: strong OCR (read on-screen text), scene-understanding (count and identify characters, recognize settings), and concise output.

| Tier         | Cloud                                                    | Open-source — Chinese-strong                                           | Open-source — general                                |
| ------------ | -------------------------------------------------------- | ---------------------------------------------------------------------- | ---------------------------------------------------- |
| Frontier     | claude-opus-4-7, gpt-4o, gemini-1.5-pro                  | qwen2.5-vl:72b *(among the strongest open vision models)*, glm-4v-plus | llava-1.6:34b, internvl2:26b, pixtral-12b           |
| Mid-tier     | claude-sonnet-4-6, gpt-4o-mini, gemini-2.0-flash         | qwen2.5-vl:7b                                                          | llava:13b, minicpm-v:8b                              |
| Cheap & fast | claude-haiku-4-5, gemini-1.5-flash                       | qwen2-vl:7b                                                            | llava:7b                                             |

### Recipes — what to put in each Settings field

Open the Settings page, scroll to **Translation model** and **Vision model**, and fill the four fields per slot: *Wire protocol*, *Endpoint URL*, *Model*, *API key*. **Local servers (Ollama, LM Studio, LocalAI) don't authenticate by default — leave the API key field blank.**

#### A. Default — Anthropic for everything (cloud)

| Slot               | Wire protocol | Endpoint           | Model               | API key            |
| ------------------ | ------------- | ------------------ | ------------------- | ------------------ |
| Translation model  | `anthropic`   | *(ignored)*        | `claude-opus-4-7`   | your Anthropic key |
| Vision model       | `anthropic`   | *(ignored)*        | `claude-opus-4-7`   | your Anthropic key |

#### B. OpenAI for everything (cloud)

| Slot               | Wire protocol   | Endpoint                     | Model         | API key       |
| ------------------ | --------------- | ---------------------------- | ------------- | ------------- |
| Translation model  | `openai_compat` | `https://api.openai.com/v1`  | `gpt-4o-mini` | your key      |
| Vision model       | `openai_compat` | `https://api.openai.com/v1`  | `gpt-4o`      | your key      |

Tick **Supports vision** on the translation slot if you plan to use cinematic mode (gpt-4o-mini handles images).

#### C. Fully local — Ollama (text + vision)

No API keys anywhere. Pull the models on the Ollama host first: `ollama pull qwen2.5:72b && ollama pull qwen2.5-vl:72b`.

| Slot               | Wire protocol   | Endpoint                     | Model              | API key |
| ------------------ | --------------- | ---------------------------- | ------------------ | ------- |
| Translation model  | `openai_compat` | `http://ollama:11434/v1`     | `qwen2.5:72b`      | *(blank)* |
| Vision model       | `openai_compat` | `http://ollama:11434/v1`     | `qwen2.5-vl:72b`   | *(blank)* |

Untick **Supports vision** on the translation slot — `qwen2.5` (text) doesn't see (disables cinematic, scene still works because that uses the vision slot).

If your Ollama isn't on the docker network as `ollama`, use `http://<ollama-host>:11434/v1`.

#### D. Mixed — cheap cloud text + local vision

The translation slot's per-cue cost adds up; the vision slot fires once per shot (~200 calls per film) and benefits from a strong specialised model.

| Slot               | Wire protocol   | Endpoint                          | Model              | API key   |
| ------------------ | --------------- | --------------------------------- | ------------------ | --------- |
| Translation model  | `openai_compat` | `https://api.openai.com/v1`       | `gpt-4o-mini`      | your key  |
| Vision model       | `openai_compat` | `http://ollama:11434/v1`          | `qwen2.5-vl:72b`   | *(blank)* |

#### E. LM Studio on the host machine (Docker → host network)

| Slot               | Wire protocol   | Endpoint                                | Model                       | API key   |
| ------------------ | --------------- | --------------------------------------- | --------------------------- | --------- |
| Translation model  | `openai_compat` | `http://host.docker.internal:1234/v1`   | `Qwen2.5-72B-Instruct-MLX`  | *(blank)* |

`host.docker.internal` resolves to the host machine on Docker Desktop and via TrueNAS Scale's docker default bridge.

#### F. OpenRouter — one key, many models

| Slot               | Wire protocol   | Endpoint                          | Model                            | API key       |
| ------------------ | --------------- | --------------------------------- | -------------------------------- | ------------- |
| Translation model  | `openai_compat` | `https://openrouter.ai/api/v1`    | `deepseek/deepseek-chat`         | your OR key   |
| Vision model       | `openai_compat` | `https://openrouter.ai/api/v1`    | `qwen/qwen2.5-vl-72b-instruct`   | your OR key   |

#### G. DeepSeek native + Zhipu (Chinese stack)

| Slot               | Wire protocol   | Endpoint                                       | Model            | API key            |
| ------------------ | --------------- | ---------------------------------------------- | ---------------- | ------------------ |
| Translation model  | `openai_compat` | `https://api.deepseek.com/v1`                  | `deepseek-chat`  | your DeepSeek key  |
| Vision model       | `openai_compat` | `https://open.bigmodel.cn/api/paas/v4`         | `glm-4v-plus`    | your Zhipu key     |

### Wire protocol cheat sheet

Each slot independently picks one of two protocols:

| Type            | What it talks to                                                                                                                                                                                          |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `anthropic`     | Native Anthropic SDK. Prompt caching, adaptive thinking, strict JSON-schema enforcement. Pick this only when the model is a Claude variant.                                                                |
| `openai_compat` | Any Chat-Completions-compatible endpoint: OpenAI proper, Ollama, LocalAI, OpenRouter, Together, Groq, Gemini's OpenAI-compat endpoint, DeepSeek native, Zhipu (GLM), vLLM, llama.cpp's http server, LM Studio. |

Set per-slot endpoint (only used for `openai_compat`) and API key. The two slots are independent — paste the same key value in both if you're using one provider for everything.

`deepl` auto-detects free vs paid by the API key suffix (`:fx` = free).

`nllb` works on **both image flavors** out of the box. The openvino image runs it accelerated on the Intel iGPU via `optimum-intel` (5-10× faster); the CPU image falls back to plain PyTorch transformers. First call triggers an HF model download (~1.5 GB) cached to `/cache/nllb-models`. On openvino there's an additional one-off OpenVINO IR conversion (~5 min) that's also cached.

## STT backends

Two backends, selected from Settings → Speech-to-Text → Backend:

| Backend     | When to use                                  | Image                                |
| ----------- | -------------------------------------------- | ------------------------------------ |
| `openvino`  | Intel iGPU host (e.g. N305, N100, modern NUCs) | `Dockerfile.openvino` (default) |
| `cpu`       | Non-Intel host, or fallback                   | `Dockerfile`                  |

The `openvino` backend uses **optimum-intel** with OpenVINO IR-converted Whisper, runs on the iGPU via `device="GPU"`. The IR conversion happens on first request (5–30 min depending on model size) and is cached on the volume — subsequent restarts are instant.

The `cpu` backend uses `faster-whisper` with INT8 quantization. Slower but no special hardware needed.

## Quick start (any docker host)

```sh
# 1. Clone and bring up the container
git clone <this-repo> babel-tower-emby
cd babel-tower-emby
mkdir -p ./cache && sudo chown -R 568:568 ./cache    # see "TrueNAS dataset perms" below
docker compose up --build -d

# 2. Open the web UI
open http://localhost:8765/

# 3. Go to Settings → fill in:
#    - Emby section: server URL + API key (Emby admin → Advanced → API Keys)
#    - Translation model: pick wire protocol, endpoint (if openai_compat), model id, API key
#    - Vision model (optional, only if you want scene/cinematic): same fields
#    - Defaults: target language, quality tier
#    Click "Save settings". No restart needed.

# 4. Go to Library → click "Subtitle this" on any item.
# 5. Watch the dashboard — your first .vtt lands next to the source media.
```

The web UI shows:
- **Dashboard** (`/`) — status, recent jobs (auto-refreshing), Sweep button.
- **Library** (`/library`) — browse Emby items, search by name, filter to "missing target-lang sub", queue a per-item job with the current target language + mode.
- **Settings** (`/settings`) — every editable parameter (STT backend, Whisper model, translation provider, default target language, scene-detection knobs, Emby URL, API keys, etc.) without redeploying.

Settings persist to `/cache/settings.json` and override env defaults from compose.

## Creating subtitles

Two flows, both in the UI. There is no auto-trigger on item-added events, no webhook, no path-based curl endpoint — every subtitle is the result of a deliberate user action.

### Per item

1. Open `/library`.
2. Optional: filter to *missing target-language subtitle* and search for the item you care about.
3. Click *Subtitle this* on the row. A job appears immediately on the dashboard's *Recent jobs* table and processes in the background.

### Whole library (sweep)

1. On the dashboard, click *Sweep library*.
2. Babel Tower queues one job per item missing a subtitle in your default target language. Already-subtitled items are skipped. Jobs run one at a time so the iGPU doesn't thrash.

After a job finishes, the result is cached (keyed by file fingerprint + target lang + provider + mode + STT model + translation/vision LLM model ids). Click *Subtitle this* again on the same item and it returns instantly. Switching the configured LLM in Settings invalidates the cache automatically.

## Generating an Emby API key

In Emby admin: **Server Settings → Advanced → API Keys → New API Key**. Paste it into the **Settings** page in the Babel Tower UI. Babel Tower uses it to fetch item metadata and trigger refreshes after writing a new `.vtt`.

## Deploying on TrueNAS Scale (24.10+ / Electric Eel and later)

TrueNAS Scale's Apps system runs Docker natively and accepts docker-compose YAML via the **Custom App** dialog. Two paths:

### Path A — build locally (simplest)

SSH into TrueNAS, clone this repo to a dataset, and run docker-compose from the shell:

```sh
ssh admin@truenas
cd /mnt/tank/apps        # or wherever you keep apps
git clone <this-repo> babel-tower-emby
cd babel-tower-emby
mkdir -p ./cache && sudo chown -R 568:568 ./cache
export RENDER_GID=$(getent group render | cut -d: -f3)
# edit docker-compose.yml: change `/mnt/media:/mnt/media:ro` to your dataset path
docker compose up --build -d
```

Open `http://<truenas-ip>:8765/` and configure everything from the Settings page — no .env file, no env vars.

### Path B — pre-built image from GHCR (Custom App UI friendly)

The TrueNAS Apps UI doesn't reliably run `build:` — paste-into-Custom-App works best with pre-built images. The repo ships a GitHub Actions workflow (`.github/workflows/publish.yml`) that builds and pushes both image flavors to GHCR on every push to `main` and on version tags.

**One-time setup (in your fork):**

1. Push the repo to GitHub.
2. The `Publish container images` workflow runs automatically and creates two images at `ghcr.io/<your-username>/babel-tower-emby`.
3. Make the package public so TrueNAS can pull without auth:
   - Go to your GitHub profile → **Packages** tab → click `babel-tower-emby`
   - **Package settings** → **Change visibility** → **Public**
   - (Optional, recommended) **Manage Actions access** → Add the repo with **Write** role so future builds can update it.

**On TrueNAS:**

1. Edit `docker-compose.yml`: comment out the `build:` block, uncomment the `image:` line. Set `GHCR_OWNER` to your GitHub username/org.
2. Paste the resulting YAML into **TrueNAS → Apps → Discover Apps → Custom App**.
3. Once the container is up, open the web UI on `:8765` and finish the setup there — same as Path A.

**Image tags published:**

| Tag                        | What                                                    |
| -------------------------- | ------------------------------------------------------- |
| `:openvino` / `:cpu`       | Moving "latest from main branch" pointer per flavor     |
| `:1.2.3-openvino`          | Pinned release (when you push a `v1.2.3` git tag)       |
| `:1.2-openvino`            | Floating major.minor                                    |
| `:openvino-sha-abc1234`    | Pinned to a specific commit, for traceability/rollback  |

For production deployments, pin to a specific version tag rather than `:openvino` to avoid surprise updates.

### TrueNAS dataset write permissions

Babel Tower runs as the **`app` user (UID/GID 568)** inside the container — not root. UID 568 was chosen deliberately to match the `apps` user that TrueNAS Scale 24.10+ uses for containerized apps. **On TrueNAS, this means dataset reads/writes Just Work without any ACL fiddling** — the `apps`-owned datasets are already writable by UID 568.

Two host-side directories need to be writable by UID 568:

1. **The cache bind-mount** (`./cache`) — holds OpenVINO IR, HF model downloads, transcripts, and `settings.json`
2. **The media library** (`/mnt/media` or wherever you mount it) — for writing the `.vtt` files next to source media

If your host *isn't* TrueNAS, or your media dataset isn't owned by `apps`, you have three options:

**(a) Override the runtime UID to match your host.**

Find the dataset owner with `stat /mnt/<pool>/media` — note the `Uid` and `Gid`. Then either set `PUID`/`PGID` in your shell before `docker compose up`, or override directly in compose's `user:` line.

**(b) Grant UID 568 access to your dataset.**

From the host shell, grant UID 568 rwx on the media dataset (ACL or `chown`/`chmod`), and prepare the cache bind-mount:

```sh
mkdir -p ./cache && sudo chown -R 568:568 ./cache
```

This keeps the container running as the unprivileged `app` user and grants exactly the access it needs.

**(c) Fall back to root.** Set `PUID=0 PGID=0` — loses the security benefit but works without any host-side perm changes.

If perms are wrong, you'll see `PermissionError: [Errno 13]` on the failed job in the dashboard — the rest of the pipeline runs fine, the `.vtt` just doesn't land on disk.

Note: the `group_add: ${RENDER_GID:-107}` in compose adds the host's `render` group to the container user, which is what gives the non-root `app` user access to `/dev/dri/renderD128` for OpenVINO iGPU inference. Make sure `RENDER_GID` matches your host (`getent group render | cut -d: -f3`).

### TrueNAS-specific notes

- **Render GID**: TrueNAS Scale 24.10+ uses GID 107 by default (the `RENDER_GID` env var defaults to 107 in compose). Verify with `getent group render` on the host.
- **Volume paths**: TrueNAS datasets live under `/mnt/<pool>/...`. Adjust the media volume to match your library, e.g. `- /mnt/tank/media:/mnt/media:ro`.
- **iGPU passthrough**: if you also use the iGPU for jellyfin/plex/emby transcoding, all containers can share `/dev/dri` — there's no exclusive lock.
- **Cache volume**: keep `./cache:/cache` on a fast dataset; the OpenVINO IR files are several GB for `large-v3-turbo`.

## Power-user knobs

Everything below is **optional**. The web UI covers the same surface and is the canonical configuration path. These env vars only matter if you prefer declarative deployment (TrueNAS Custom App YAML, GitOps, etc.) — they set first-boot defaults that the UI then overrides as soon as you click *Save settings*.

| Variable                       | Default              | Notes                                                                    |
| ------------------------------ | -------------------- | ------------------------------------------------------------------------ |
| `BABEL_WHISPER_BACKEND`        | `cpu`                | `cpu` (faster-whisper) or `openvino` (Intel iGPU). The OpenVINO Dockerfile sets `openvino`. |
| `BABEL_WHISPER_MODEL`          | `small`              | `tiny`/`base`/`small`/`medium`/`large-v3`/`large-v3-turbo`              |
| `BABEL_OPENVINO_DEVICE`        | `GPU`                | `GPU` for iGPU, `CPU` for OpenVINO-on-CPU, `AUTO` to let it pick         |
| `BABEL_WHISPER_DEVICE`         | `cpu`                | (CPU backend only) `cpu`/`cuda`                                          |
| `BABEL_WHISPER_COMPUTE_TYPE`   | `int8`               | (CPU backend only) `int8`/`int16`/`float16`/`float32`                    |
| `BABEL_TRANSLATION_LLM_TYPE`   | `anthropic`          | `anthropic` or `openai_compat`                                           |
| `BABEL_TRANSLATION_LLM_MODEL`  | `claude-opus-4-7`    | Translator model id (e.g. `gpt-4o-mini`, `qwen2.5:72b`, `deepseek-v3`) |
| `BABEL_TRANSLATION_LLM_ENDPOINT` | `https://api.openai.com/v1` | Endpoint URL when type=openai_compat                            |
| `BABEL_TRANSLATION_LLM_API_KEY` | (unset)             | API key for the translation slot                                         |
| `BABEL_TRANSLATION_LLM_SUPPORTS_VISION` | `true`      | True if the translation model accepts images — required for cinematic   |
| `BABEL_VISION_LLM_TYPE`        | `anthropic`          | `anthropic` or `openai_compat`                                           |
| `BABEL_VISION_LLM_MODEL`       | `claude-opus-4-7`    | Scene-describer model id (e.g. `qwen2.5-vl:72b`, `gemini-1.5-pro`)     |
| `BABEL_VISION_LLM_ENDPOINT`    | `https://api.openai.com/v1` | Endpoint URL when type=openai_compat                            |
| `BABEL_VISION_LLM_API_KEY`     | (unset)              | API key for the vision slot                                              |
| `BABEL_VISION_LLM_ENABLED`     | `true`               | Master switch for scene/cinematic modes                                  |
| `BABEL_CACHE_DIR`              | `/cache`             | Cache directory for OpenVINO IR + transcripts                            |
| `BABEL_NLLB_MODEL`             | `facebook/nllb-200-distilled-600M` | HuggingFace id for the local NLLB translator                |
| `BABEL_TRANSLATION_BATCH_SIZE` | `30`                 | Cues per LLM API call (text-only mode)                                  |
| `BABEL_MAX_LINE_CHARS`         | `42`                 | Subtitle line wrap width                                                 |
| `BABEL_MAX_LINES_PER_CUE`      | `2`                  | Max display lines per cue (overflow merges into the last line, never drops content) |
| `BABEL_DEFAULT_TARGET_LANG`    | `fr`                 | Default target language for per-item and sweep jobs                     |
| `BABEL_DEFAULT_SOURCE_LANG_PRIORITY` | `["en","ja","*"]` | Source-language preference for track selection (JSON list)             |
| `BABEL_DEFAULT_TRANSLATION_PROVIDER` | `nllb`       | Default provider for per-item and sweep jobs (`nllb`/`deepl`/`llm`). Default `nllb` is free, local, no key — works on both image flavors out of the box. |
| `BABEL_DEFAULT_MODE`           | `audio`              | Default quality tier — `audio` / `scene` / `cinematic`                  |
| `BABEL_SCENE_DETECTION_THRESHOLD` | `0.4`             | ffmpeg scene-detection threshold (0–1, lower = more scenes)              |
| `BABEL_SCENE_MIN_LENGTH_SECONDS` | `1.5`              | Skip scenes shorter than this many seconds                               |
| `BABEL_SCENE_MAX_SCENES`       | `500`                | Hard cap on detected scenes per file                                     |
| `BABEL_SCENE_KEYFRAME_POSITION` | `midpoint`          | Where to grab the scene's keyframe — `start`/`midpoint`/`end`           |
| `BABEL_SCENE_FRAME_MAX_SIZE`   | `1024`               | Long-edge px for scene keyframes sent to the vision LLM                 |
| `BABEL_SCENE_BIBLE_BATCH_SIZE` | `10`                 | Scenes per vision LLM call when building the bible                      |
| `BABEL_CINEMATIC_FRAME_MAX_SIZE` | `768`              | Long-edge px for per-cue frames in cinematic mode                       |
| `BABEL_CINEMATIC_BATCH_SIZE`   | `10`                 | Cues per LLM call in cinematic mode                                     |
| `BABEL_DEFAULT_SKIP_IF_TARGET_AUDIO_EXISTS` | `true` | Skip when target-language audio is already in the file                   |
| `BABEL_EMBY_URL`               | (unset)              | Emby server base URL (e.g. `http://emby:8096`)                           |
| `BABEL_EMBY_API_KEY`           | (unset)              | Emby admin API key (Server Settings → Advanced → API Keys)              |
| `BABEL_DEEPL_API_KEY`          | (unset)              | DeepL API key. Free-tier keys end in `:fx` (auto-detected)              |

## Defaults and tradeoffs

- **STT model is `small` by default** for quick iteration. For production quality on the iGPU, switch to `large-v3-turbo` (close to `large-v3` quality at ~2× the speed).
- **OpenVINO first-run is slow.** The IR conversion for `large-v3-turbo` takes 15–30 min on the N305 and produces ~3 GB of IR files. Subsequent runs hit the cache and start in seconds. Watch the container logs the first time.
- **Untagged audio tracks are auto-detected.** When ffprobe reports a track without a language tag (Emby just shows "Audio"), Babel Tower runs a Whisper-tiny detection pre-pass on the first 30s of the extracted audio (~2-3s extra per job, model is ~75 MB cached on first use). The detected ISO 639-1 code drives transcription so NLLB/DeepL get the right source language.
- **Tag write-back is MKV-only and surgical.** When the detected language differs from "untagged", we persist it back into the source file's audio-stream metadata via `mkvpropedit` — which edits ONLY the EBML metadata header and never touches the audio/video data. No re-encode, no remux, no temporal manipulation, no risk of audio gaps. Restricted to `.mkv` / `.mka` / `.webm`. For non-Matroska containers (MP4 / MOV / AVI / …) the write-back is skipped on purpose: an `ffmpeg -c copy` remux would rewrite the whole file with documented edge cases (timestamp re-derivation on unusual MP4s, lost obscure metadata, full-I/O write window) that aren't worth the risk on a media library. Detection still drives transcription correctness for those files — only the persist-to-Emby polish is skipped. Disable entirely via Settings → Defaults → *Tag detected source language back into the source file*.
- **Path-based contract:** the app reads media files directly from disk. Mount the same path inside the container that Emby sees. No file uploads over HTTP.
- **Manual-trigger only:** subtitle creation is always an explicit user action in the UI. There is no webhook, no auto-trigger on Emby `ItemAdded`, no path-based curl endpoint. This is a deliberate scope decision — the goal is for the user to decide when each title gets translated.
- **Cache hygiene.** The `./cache/` bind-mount holds three things: model weights (faster-whisper / OpenVINO IR / NLLB; can be tens of GB), per-file transcript JSON (small, one per `(file, target, mode, provider, llm-model)` combo), and the UI-mutated `settings.json`. Nothing currently expires — clear `cache/` to force a clean re-run, but be aware that re-conversion of OpenVINO IR is slow.

## Tests

```sh
pip install -e .[dev]
pytest
```

Tests across pure-logic surface (language normalization, VTT formatting, track selection, scene mapping, settings store with migration, batching, Emby item parsing, cache key invalidation when LLM model changes) plus FastAPI smoke tests for every route. Heavy externals (ffmpeg, Whisper, LLM APIs) are stubbed — the suite runs in ~1s.

```
tests/
├── conftest.py                test env + per-test isolation
├── test_lang.py               ISO 639-1/2 normalization
├── test_vtt.py                Timestamp formatting + line wrapping
├── test_cache.py              Fingerprint, key, JSON-error handling, LLM-model invalidation
├── test_config.py             SettingsStore + the legacy → per-function migration
├── test_scenes.py             Cue → scene mapping, keyframe positioning
├── test_tracks.py             Track-selection policy
├── test_translate_util.py     batches() helper
├── test_emby_client.py        EmbyItem.has_subtitle_track, ISO 639-2 lookup
├── test_jobs.py               Job dataclass, eviction, missing-loop guard
├── test_processor.py          Mode validation gates
└── test_smoke_api.py          /health, /api/settings, /api/jobs, /api/process,
                               /api/sweep, dashboards, library, partials, plus
                               regression tests confirming /webhook/emby and
                               /transcribe-translate are absent (404)
```

## Layout

```
babel-tower-emby/
├── .github/workflows/publish.yml   GHCR multi-flavor image publish
├── .env.example                    Optional first-boot defaults (UI overrides everything)
├── docker-compose.yml
├── Dockerfile                      CPU image (faster-whisper)
├── Dockerfile.openvino             Intel iGPU image (OpenVINO + optimum-intel)
├── pyproject.toml
├── README.md
└── app/
    ├── main.py                     FastAPI entry, registers all routers
    ├── config.py                   Layered settings (env + persisted JSON)
    ├── cache.py                    File-fingerprint transcript cache
    ├── jobs.py                     In-memory job queue (single-worker async)
    ├── processor.py                Pipeline orchestrator (called from API + UI)
    ├── api/
    │   ├── manage.py               Emby-driven endpoints (back the UI buttons)
    │   └── settings_api.py         GET/PATCH /api/settings
    ├── emby/
    │   └── client.py               Minimal Emby REST client
    ├── ui/
    │   └── routes.py               HTML routes (dashboard, library, settings)
    ├── templates/
    │   ├── base.html
    │   ├── dashboard.html
    │   ├── library.html
    │   └── settings.html
    └── pipeline/
        ├── tracks.py               ffprobe + audio track selection
        ├── audio.py                ffmpeg audio extraction
        ├── lang.py                 ISO 639-2 → 639-1 normalization
        ├── stt.py                  STT backend dispatcher
        ├── stt_faster_whisper.py
        ├── stt_openvino.py
        ├── scenes.py               ffmpeg scene-detection + cue mapping (scene/cinematic)
        ├── frames.py               In-memory ffmpeg keyframe extraction
        ├── scene_bible.py          LLM-vision scene-bible builder + cache
        ├── vtt.py                  WebVTT formatter
        ├── llm/
        │   ├── base.py             LLMClient protocol + content blocks
        │   ├── anthropic.py        Anthropic-native (caching, thinking, schema)
        │   └── openai_compat.py    OpenAI-Chat-Completions compatible (OpenAI/Ollama/etc.)
        └── translate/
            ├── base.py             Provider protocol + TranslationContext
            ├── llm.py              LLM-backed translator (uses the configured backend)
            ├── deepl.py            DeepL (httpx, text-only — context ignored)
            └── nllb.py             NLLB-200 — OpenVINO-accelerated when available, CPU torch fallback otherwise
```
