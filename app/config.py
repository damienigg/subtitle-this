"""Layered settings: code defaults < env vars < /cache/settings.json (mutated by UI).

Existing code reads `from app.config import settings` and uses attribute access
(`settings.whisper_model`). The `Settings` object below is a SettingsStore that
proxies attribute access — values written via the UI override env-bound defaults.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


_log = logging.getLogger("subtitle_this")


class _EnvSettings(BaseSettings):
    """Bootstrap defaults from env vars. Never written to at runtime."""
    model_config = SettingsConfigDict(env_prefix="BABEL_", env_file=".env", extra="ignore")

    cache_dir: Path = Path("/cache")

    # STT
    whisper_backend: str = "cpu"
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    # AUTO lets OpenVINO pick GPU (Intel iGPU) when available, falls back to
    # CPU otherwise. Hidden from the Settings UI — explicit GPU/CPU choices
    # add no value over AUTO and confused users. Power users can still
    # override via BABEL_OPENVINO_DEVICE env var.
    openvino_device: str = "AUTO"
    # Pre-filter audio with Silero-VAD before feeding it to Whisper so silent
    # stretches never reach the decoder. Default ON — without it, the OpenVINO
    # backend hallucinates boilerplate ("Thank you.", "Thanks for watching.")
    # in silent regions because direct OVModel.generate() bypasses Whisper's
    # built-in no-speech / log-prob guards. Net runtime is also faster (skips
    # 30–50% of audio in a typical film). Off only as an escape hatch for
    # very-quiet-but-real-speech files where Silero may be too strict.
    #
    # This flag is openvino-only: the CPU/faster-whisper backend runs its
    # own internal VAD (always-on `vad_filter=True` in the .transcribe call)
    # which is unrelated to Silero, so toggling this setting has no effect
    # there. The cache key reflects that — vad_enabled is included in the
    # key only when whisper_backend=openvino.
    vad_enabled: bool = True

    # ── Translation LLM ───────────────────────────────────────────────────────
    # Translates subtitle cues. In cinematic mode, also receives per-cue frames
    # — must be vision-capable for that path. See translation_llm_supports_vision.
    translation_llm_type: str = "anthropic"   # anthropic | openai_compat
    translation_llm_model: str = "claude-opus-4-7"
    translation_llm_endpoint: str = "https://api.openai.com/v1"
    translation_llm_api_key: str | None = None
    translation_llm_supports_vision: bool = True

    # ── Vision LLM ────────────────────────────────────────────────────────────
    # Builds the scene bible: describes each shot's keyframe in 1-2 sentences.
    # Used by scene + cinematic modes. By construction, must be vision-capable.
    vision_llm_type: str = "anthropic"
    vision_llm_model: str = "claude-opus-4-7"
    vision_llm_endpoint: str = "https://api.openai.com/v1"
    vision_llm_api_key: str | None = None
    vision_llm_enabled: bool = True   # toggle off to disable scene/cinematic

    # Other translation providers
    deepl_api_key: str | None = None
    nllb_model: str = "facebook/nllb-200-distilled-600M"
    # Cues per generate() call for NLLB. 16 is balanced for distilled-600M
    # on most hardware; the bigger NLLB variants benefit from smaller batches
    # to stay under iGPU activation memory.
    # Default tuned conservatively for NLLB-1.3B — the KV cache during
    # generate() scales as batch_size × num_beams × seq_len × hidden_dim
    # × num_layers, which for the 1.3B variant at batch=16 is ~1.5 GB of
    # transient activation memory on top of the ~3 GB weight footprint.
    # batch=4 keeps the activation peak around ~400 MB so the whole
    # phase fits comfortably under a 12 GB cgroup with Whisper-large's
    # page cache also lingering. Users with smaller models or more
    # headroom can bump it via the Settings UI for throughput.
    nllb_batch_size: int = Field(4, ge=1, le=128)
    # Compress NLLB weights to int8 at load time (OpenVINO path only —
    # NNCF via optimum-intel). Halves resident weight memory (e.g.
    # ~3 GB → ~1.5 GB for distilled-1.3B) at the cost of a one-time
    # quantization step on first model load and a ~0.3 BLEU drop in
    # translation quality, which is below the noise floor for subtitle
    # work. Default ON because the 1.3B variant otherwise doesn't fit
    # alongside Whisper's page cache in a 12 GB cgroup. Toggle off if
    # you have headroom (16 GB+) and want full-precision weights.
    # No-op on the CPU/torch fallback path — that backend doesn't have
    # an in-process int8 quantization story without bitsandbytes (CUDA).
    nllb_load_in_8bit: bool = True
    # Cues per DeepL API request. 50 is the documented DeepL maximum;
    # raising it has no effect, lowering it makes more (smaller) calls.
    deepl_batch_size: int = Field(50, ge=1, le=50)
    translation_batch_size: int = Field(30, ge=1, le=200)

    # Audio segmentation for the OpenVINO STT path. Splits the extracted WAV
    # into N-second windows that are read, VAD-filtered, transcribed, and
    # released one at a time, so peak RAM stays bounded regardless of film
    # length. 600s (10 min) → ~75 MB float32 audio resident at any moment
    # for a 16 kHz mono track, vs. ~500 MB for the entire 2h12 file. Only
    # affects whisper_backend=openvino — the CPU/`faster-whisper` backend
    # streams from disk internally and ignores this setting.
    #
    # Trade-off: words straddling a segment boundary may be split into two
    # cues. With 600s segments and 1.5h-2h films that's only ~10 boundaries
    # — the artefact rate is acceptable. Lower values reduce RAM further but
    # multiply boundary artefacts; higher values keep more audio resident.
    stt_audio_segment_seconds: int = Field(600, ge=60, le=3600)

    # Pack multiple short speech regions into one 30 s Whisper decoder
    # window with brief silence pads between, then demultiplex cue
    # timestamps after decode. Cuts iGPU work 1.5-3× on dialog-heavy
    # films (typical regions are 3-10 s; without packing each becomes
    # a 30 s decoder window that's mostly zero-pad). Default ON — drop
    # to false as an escape hatch if you see misattributed cues at
    # region boundaries on a specific film. OpenVINO-only (the CPU/
    # faster-whisper backend handles its own longform batching).
    stt_region_packing: bool = True
    # Hard cap on how many speech regions get packed into one 30 s
    # Whisper window. Default 4 — keeps the silence-pad overhead
    # at ~5 % of the window's audio, which Whisper-turbo's
    # autoregressive timestamp prediction handles cleanly. With
    # no cap (the pre-0.7.11 behavior, =0) the planner could pack
    # 12+ regions per window and Whisper drifted enough to land
    # nearly half its cues in silence pads — see the Inception
    # post-mortem. Ignored when stt_region_packing is OFF.
    stt_max_regions_per_window: int = Field(4, ge=0, le=20)
    # Forward overlap (seconds) added to each STT audio-segment read so
    # speech regions straddling a segment boundary get processed in full
    # within one segment instead of being split. 30 s is one decoder
    # window — large enough to absorb the longest contiguous utterance
    # that could realistically span a boundary. Costs ~1.9 MB extra peak
    # RAM during the read.
    stt_segment_overlap_seconds: int = Field(30, ge=0, le=120)

    # Subtitle formatting
    max_line_chars: int = Field(42, ge=10, le=120)
    max_lines_per_cue: int = Field(2, ge=1, le=4)

    # ── Readability polish (0.7.17+) ─────────────────────────────────
    # Whisper outputs tight per-utterance timing — a "Yes." pronounced
    # in 0.3 s gets a 0.3 s cue. Pro subtitlers enforce minimum
    # display durations so viewers can actually read the text. These
    # knobs apply a final pass that (a) extends cues that are too
    # short, (b) optionally merges adjacent cues that are visually
    # connected. Comparison run on Inception showed Whisper produced
    # 42.8 % of cues under 1 s; the reference SRT had 0 cues under
    # 1 s. After polish with defaults, the generated .vtt distribution
    # matches the SRT shape much more closely.
    polish_enabled: bool = True
    # Minimum display duration in seconds, regardless of text length.
    # 1.2 s is the lower end of professional subtitling norms (BBC
    # subtitle guidelines: 0.7-1.5 s minimum; Netflix: 5/6 sec for
    # one-syllable utterances). Cues below this get their end
    # extended (never the start — keeps sync with the audio onset).
    min_cue_duration_seconds: float = Field(1.2, ge=0.0, le=5.0)
    # Reading-speed cap: each character claims at least this many
    # seconds of display time. 0.045 ≈ 22 chars/second, a relaxed
    # reading speed. A 40-char two-line cue would need 40 × 0.045 =
    # 1.8 s minimum even if it was uttered faster.
    min_seconds_per_char: float = Field(0.045, ge=0.0, le=0.2)
    # Merge adjacent short cues that fit together visually. Two cues
    # qualify when (a) the gap between them is under
    # max_gap_to_merge_seconds, (b) the combined text fits within
    # max_line_chars × max_lines_per_cue, (c) the combined display
    # duration stays under max_merged_cue_duration_seconds.
    merge_adjacent_cues: bool = True
    max_gap_to_merge_seconds: float = Field(0.3, ge=0.0, le=2.0)
    max_merged_cue_duration_seconds: float = Field(7.0, ge=1.0, le=15.0)
    # Minimum gap to keep between consecutive cues after polish, so
    # an extended cue's end doesn't bleed onto the next cue's start.
    # 0.05 s ≈ 1 frame at 24 fps — invisible but enough for renderers
    # that won't display two overlapping cues cleanly.
    cue_separation_seconds: float = Field(0.05, ge=0.0, le=1.0)

    # Defaults applied when the user clicks "Subtitle this" on a row or
    # "Subtitle selected" on the multi-select batch in the web UI without
    # overriding per-item.
    default_target_lang: str = "fr"
    # Source-track preference list for multi-audio films. Default prefers
    # English then any other language. Hidden from the Settings UI — this
    # is a niche power-user knob (most users have single-audio-track films
    # where the choice doesn't matter). Override via env var if needed.
    default_source_lang_priority: list[str] = ["en", "*"]
    # Default provider is `nllb`: free, fully local, no account, no API key.
    # Works on BOTH image flavors out of the box — the openvino image runs it
    # accelerated on the Intel iGPU via optimum-intel; the CPU image falls
    # back to plain PyTorch transformers (slower but no setup either way).
    # Either way the first call downloads the ~1.5 GB NLLB-200 model to
    # /cache/nllb-models. Users who want best quality flip this to `llm`.
    default_translation_provider: str = "nllb"
    default_skip_if_target_audio_exists: bool = True
    # When the source audio track has no language tag in the file's metadata,
    # Whisper's auto-detection still works for transcription itself. This flag
    # controls whether we ALSO write the detected ISO 639-2 code back into the
    # source file's audio stream metadata so Emby reads the right language on
    # next probe.
    #
    # Restricted to Matroska (.mkv/.mka/.webm) via `mkvpropedit`, which edits
    # ONLY the EBML header — never touches audio/video data, no re-encode, no
    # remux. For non-MKV containers (MP4/MOV/AVI/...) we deliberately skip the
    # write-back: an `ffmpeg -c copy` remux would technically preserve the
    # audio bitstream, but it rewrites the entire file and has known edge
    # cases (timestamp re-derivation on weird MP4s, lost obscure metadata,
    # full-I/O write window) that a media library shouldn't have to worry
    # about. Detection still drives transcription correctness for those files;
    # only the persist-to-Emby polish is skipped.
    #
    # Best-effort — failures don't fail the subtitling job.
    write_detected_language_to_file: bool = True
    # Quality tier: audio (default) | scene | cinematic.
    # `scene` adds an LLM-vision scene bible (one short description per shot)
    # for pronoun/gender disambiguation. `cinematic` additionally attaches
    # per-cue keyframes to translation calls. Both require
    # translation_provider="llm" with a vision-capable backend.
    default_mode: str = "audio"

    # Scene detection (used by scene + cinematic modes). Tuning these lets
    # operators trade detection sensitivity vs. cost.
    scene_detection_threshold: float = Field(0.4, ge=0.0, le=1.0)
    scene_min_length_seconds: float = Field(1.5, ge=0.0, le=60.0)
    scene_max_scenes: int = Field(500, ge=1, le=2000)
    scene_keyframe_position: str = "midpoint"      # start | midpoint | end
    scene_frame_max_size: int = Field(1024, ge=128, le=2048)   # long-edge px sent to the vision LLM
    scene_bible_batch_size: int = Field(10, ge=1, le=50)        # scenes per vision LLM call

    # Cinematic mode (per-cue frame attachment). Smaller frames + smaller batches
    # because each call ships up to N images.
    cinematic_frame_max_size: int = Field(768, ge=128, le=2048)
    cinematic_batch_size: int = Field(10, ge=1, le=50)
    # Frame-accurate seek for per-cue extraction. False (default) = fast
    # input-seek which snaps to the nearest keyframe; on a typical film
    # with a ~2 s keyframe interval, the extracted JPEG can be up to a
    # couple seconds off from the cue's actual midpoint. Acceptable for
    # most use cases (and 5-10× faster). True = combined seek:
    # `-ss <ts-5> -i <file> -ss 5 -frames:v 1` — fast input seek to ~5s
    # before, then accurate output seek of 5s. Frame-accurate at the
    # cost of decoding the intervening ~5s per cue. Recommended only
    # when extracted frames will be used for fine-grained visual
    # disambiguation (e.g. lip-sync verification, on-screen text OCR).
    cinematic_frame_accurate_seek: bool = False
    # Hard cap on how many cues get a per-cue frame attached. A 2h+ film with
    # heavy dialog can generate 1500+ cues — pre-extracting one JPEG per cue
    # and holding them all in RAM (plus base64 inflation per request) is what
    # blew up the TrueNAS host. With this cap, only the first N cues ship
    # frames; the remaining cues still translate, just text-only. Set to 0 to
    # downgrade cinematic to scene-only behavior on every job. The
    # processor extracts frames lazily per translation batch (not upfront)
    # so the cap also bounds peak RAM at one batch's worth of JPEGs.
    cinematic_max_cues_with_frames: int = Field(800, ge=0, le=5000)

    # Hard wall-clock cap on a single job (seconds). Whisper-large on a 3-hour
    # film at int8 on CPU can legitimately take ~2 hours; 5400s (90 min) is a
    # generous default that still kills genuinely wedged jobs. Set to 0 to
    # disable the timeout entirely. Enforced via Job.check_cancel — every
    # check_cancel call across the pipeline already gates on the cancel flag,
    # so deadline-cancel uses the same code paths.
    job_timeout_seconds: int = Field(5400, ge=0, le=86400)

    # Optional HTTP Basic auth. When set to "user:password" the entire web UI
    # and API surface require that credential, plus a same-origin check on
    # POST/PATCH/DELETE/PUT (so a CSRF page on a different LAN host can't
    # rinse your LLM API quota by submitting jobs through your browser).
    # Empty string (the default) disables auth — preserves the zero-config
    # first-boot experience. /health is always exempt so Docker healthchecks
    # work without credentials.
    auth_credentials: str | None = None

    # Optional shell command run by the "Update now" button on the
    # dashboard. Empty (default) means the button is hidden — clicking
    # "Check for updates" only surfaces the version comparison +
    # copy-paste commands. With a value set (e.g.
    # ``cd /mnt/.../subtitle-this && git pull && docker compose build &&
    # docker compose up -d``), the button executes that command via
    # subprocess and streams the output to the UI.
    #
    # Security: env-var-only by design — not exposed in the Settings
    # UI for editing. The command is YOURS, not user-supplied input,
    # so there's no injection path. Running it still requires whatever
    # privileges the container has — typically docker socket access
    # (mount /var/run/docker.sock if you want the command to invoke
    # docker compose).
    update_command: str | None = None

    # Media server (Emby / Jellyfin / Plex). Type drives:
    # - which client class is built (Emby+Jellyfin share one impl, Plex has its own)
    # - the auth header convention (X-Emby-Token vs X-Plex-Token)
    # - UI label cosmetics (badge says "Emby"/"Jellyfin"/"Plex")
    # The api_key field doubles as the Plex token when type=plex.
    media_server_type: str = "emby"   # emby | jellyfin | plex
    media_server_url: str | None = None
    media_server_api_key: str | None = None
    # Whether to verify the media server's TLS certificate. Default ON for
    # safety. Off for: Plex via LAN IP (cert claims *.plex.direct, hostname
    # won't match), Jellyfin/Emby with self-signed certs, or any homelab
    # reverse-proxy without a public CA-issued cert. The toggle only affects
    # this app's outbound calls — it does not weaken anything else. For a
    # middle ground (self-signed but trusted CA), advanced operators can
    # mount a CA bundle and set SSL_CERT_FILE in the container env; httpx
    # picks that up automatically and this toggle stays ON.
    media_server_verify_ssl: bool = True


# Set of fields that are sensitive — masked in UI GET responses, password input on edit.
SENSITIVE_FIELDS: set[str] = {
    "translation_llm_api_key",
    "vision_llm_api_key",
    "deepl_api_key",
    "media_server_api_key",
    "auth_credentials",
}

# Set of fields the UI cannot edit (operator-only via env).
READ_ONLY_FIELDS: set[str] = {"cache_dir"}


class SettingsStore:
    """Attribute-access proxy over env defaults + persisted user overrides."""

    def __init__(self, env_settings: _EnvSettings) -> None:
        self._env = env_settings
        self._file: Path = env_settings.cache_dir / "settings.json"
        # Concurrent settings PATCHes from the UI used to race read-modify-
        # write on _overrides. The lock is held only across the in-memory
        # rebind + the atomic file write; reads (attr access, all_values)
        # never touch the lock. Consistency across readers is provided by
        # the copy-on-write rebind in `update()` / `reset()` / `reset_all()`
        # — each mutation builds a NEW dict and atomically rebinds
        # `self._overrides`, so a reader either sees the full pre-update
        # snapshot or the full post-update one, never a half-applied state.
        self._write_lock = threading.Lock()
        self._overrides: dict[str, Any] = self._load()

    # ── public API ─────────────────────────────────────────────────────────────

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        if key in self._overrides:
            return self._overrides[key]
        return getattr(self._env, key)

    def known_fields(self) -> dict[str, Any]:
        """All field names + their pydantic types (for the settings UI)."""
        return self._env.model_fields

    def all_values(self, *, mask_sensitive: bool = False) -> dict[str, Any]:
        """Merged view of effective settings."""
        env_dump = self._env.model_dump()
        merged = {**env_dump, **self._overrides}
        if mask_sensitive:
            for k in SENSITIVE_FIELDS:
                v = merged.get(k)
                merged[k] = "[set]" if v else None
        return merged

    def update(self, kvs: dict[str, Any]) -> None:
        """Merge kvs into the user overrides. Validates each value against the
        pydantic model so junk like `whisper_model="lol but invalid type"` 400s
        at the UI/API instead of corrupting settings.json."""
        valid = set(self._env.model_fields.keys())
        for k in kvs:
            if k not in valid:
                raise ValueError(f"Unknown setting: {k!r}")
            if k in READ_ONLY_FIELDS:
                raise ValueError(f"Setting {k!r} is read-only")

        with self._write_lock:
            # Validate the post-update merged state against the pydantic
            # schema (inside the lock so two concurrent PATCHes can't both
            # validate independently and then race the dict write).
            proposed = {**self._env.model_dump(), **self._overrides, **kvs}
            try:
                _EnvSettings.model_validate(proposed)
            except Exception as e:
                raise ValueError(f"Invalid setting value: {e}") from e

            # Copy-on-write rebind: a concurrent __getattr__ never sees a
            # partially-applied dict because we publish the new state by
            # rebinding `self._overrides` (atomic at the Python level)
            # rather than mutating the existing dict in place.
            self._overrides = {**self._overrides, **kvs}
            self._save_locked()

    def reset(self, key: str) -> None:
        with self._write_lock:
            if key not in self._overrides:
                return
            new_overrides = dict(self._overrides)
            new_overrides.pop(key, None)
            self._overrides = new_overrides
            self._save_locked()

    def reset_all(self) -> None:
        with self._write_lock:
            self._overrides = {}
            self._save_locked()

    # ── internals ──────────────────────────────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        if not self._file.exists():
            return {}
        try:
            data = json.loads(self._file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            # Corrupt or unreadable settings.json: previously this silently
            # returned {} which wiped every user setting (including API keys)
            # without a trace. Move the corrupt file aside so the user can
            # recover it manually, log a clear warning, and start fresh.
            try:
                backup = self._file.with_name(
                    f"{self._file.name}.corrupt.{int(time.time())}"
                )
                self._file.rename(backup)
                _log.warning(
                    "settings.json was unreadable (%s); backed up to %s and starting "
                    "with defaults. Restore values manually from the backup if needed.",
                    e, backup,
                )
            except OSError as backup_err:
                _log.warning(
                    "settings.json was unreadable (%s) AND could not be backed up (%s); "
                    "starting with defaults.",
                    e, backup_err,
                )
            return {}

        # Snapshot for the version-tag + write-back logic below. We
        # serialize via sorted JSON so the comparison is insensitive to
        # dict ordering (some migrations rebuild dicts via comprehensions).
        before_serialized = json.dumps(data, sort_keys=True)

        for migration in _MIGRATIONS:
            data = migration(data)

        # Persist the migration result so subsequent startups don't
        # re-run them. Two signals trigger the write-back: (a) data
        # actually changed, (b) the persisted schema_version is stale
        # relative to the current build (catches the case where the
        # migrations were no-ops but we still want to bump the tag).
        from app import __version__
        prior_version = data.pop("_schema_version", None)
        # Always re-stamp with the current version so a downgrade leaves
        # a clear trail of "this file was last touched by X".
        data["_schema_version"] = __version__
        after_serialized = json.dumps(data, sort_keys=True)
        if before_serialized != after_serialized:
            try:
                self._file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._file.with_name(self._file.name + ".tmp")
                tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
                os.replace(tmp, self._file)
                if prior_version and prior_version != __version__:
                    _log.info(
                        "settings.json migrated from schema %s → %s",
                        prior_version, __version__,
                    )
                elif prior_version is None:
                    _log.info(
                        "settings.json stamped with schema_version=%s "
                        "(first run with the version-tag mechanism)",
                        __version__,
                    )
            except OSError as e:
                # Migration write-back is an optimization. If it fails,
                # the next startup re-runs the migrations from scratch —
                # not ideal but not catastrophic. Log and continue.
                _log.warning(
                    "settings.json migration write-back failed (%s); "
                    "migrations will re-apply at next startup.", e,
                )

        # Don't return _schema_version to the rest of the app — it's a
        # provenance tag, not a pydantic-model field.
        data.pop("_schema_version", None)
        return data

    def _save_locked(self) -> None:
        """Atomic write — holds self._write_lock so callers don't need to
        lock around their write. Writes to a sibling .tmp first, then
        os.replace's into place, so a crash/kill mid-write can never leave
        a half-written settings.json behind.
        """
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_name(self._file.name + ".tmp")
        tmp.write_text(json.dumps(self._overrides, indent=2, sort_keys=True))
        os.replace(tmp, self._file)

    # Back-compat alias so existing tests (and any external callers that
    # poke _save()) still work. New code should go through update/reset.
    def _save(self) -> None:
        with self._write_lock:
            self._save_locked()


# ── settings.json schema migrations ───────────────────────────────────────────
# Each migration is an idempotent dict→dict transform applied in order at load
# time. They handle field renames, schema collapses, and defaults backfill so
# users don't lose their settings across version bumps. To add a new
# migration, append a function to _MIGRATIONS at the bottom of this section.


def _rename_translation_provider_claude_to_llm(data: dict) -> dict:
    """Initial provider was named `claude` when the only supported LLM was
    Anthropic. Renamed to `llm` (which dispatches to whichever LLM backend
    is configured) when we abstracted the LLM layer."""
    if data.get("default_translation_provider") == "claude":
        data["default_translation_provider"] = "llm"
    return data


def _split_unified_llm_into_per_function_slots(data: dict) -> dict:
    """Old single-LLM config (`llm_backend`, `claude_model`,
    `openai_compat_*`, `llm_supports_vision`) was split into per-function
    slots: translation_llm_* and vision_llm_*. Mirror the legacy values
    into BOTH slots when the user hadn't already overridden them."""
    old_backend = data.pop("llm_backend", None)
    old_claude_model = data.pop("claude_model", None)
    old_oai_url = data.pop("openai_compat_base_url", None)
    old_oai_key = data.pop("openai_compat_api_key", None)
    old_oai_model = data.pop("openai_compat_model", None)
    old_supports_vision = data.pop("llm_supports_vision", None)

    if old_backend is not None:
        data.setdefault("translation_llm_type", old_backend)
        data.setdefault("vision_llm_type", old_backend)
    if old_backend == "anthropic" and old_claude_model is not None:
        data.setdefault("translation_llm_model", old_claude_model)
        data.setdefault("vision_llm_model", old_claude_model)
    if old_backend == "openai_compat":
        if old_oai_url is not None:
            data.setdefault("translation_llm_endpoint", old_oai_url)
            data.setdefault("vision_llm_endpoint", old_oai_url)
        if old_oai_key is not None:
            data.setdefault("translation_llm_api_key", old_oai_key)
            data.setdefault("vision_llm_api_key", old_oai_key)
        if old_oai_model is not None:
            data.setdefault("translation_llm_model", old_oai_model)
            data.setdefault("vision_llm_model", old_oai_model)
    if old_supports_vision is not None:
        data.setdefault("vision_llm_enabled", bool(old_supports_vision))
        data.setdefault("translation_llm_supports_vision", bool(old_supports_vision))
    return data


def _drop_shared_anthropic_api_key(data: dict) -> dict:
    """Earlier versions had a single `anthropic_api_key` shared by both
    translation and vision slots. Now each slot carries its own key.
    Backfill both slots from the shared one when they don't already
    have a value."""
    old = data.pop("anthropic_api_key", None)
    if old:
        data.setdefault("translation_llm_api_key", old)
        data.setdefault("vision_llm_api_key", old)
    return data


def _rename_emby_to_media_server(data: dict) -> dict:
    """`emby_url` / `emby_api_key` were renamed to `media_server_url` /
    `media_server_api_key` when we generalized to Emby + Jellyfin + Plex.
    Existing deployments had the server pre-configured under the old
    names — copy them over and default the new server-type to 'emby'
    since that's what they were running."""
    old_url = data.pop("emby_url", None)
    old_key = data.pop("emby_api_key", None)
    if old_url is not None:
        data.setdefault("media_server_url", old_url)
    if old_key is not None:
        data.setdefault("media_server_api_key", old_key)
    if (old_url or old_key) and "media_server_type" not in data:
        data["media_server_type"] = "emby"
    return data


def _drop_unknown_keys(data: dict) -> dict:
    """Remove keys that aren't in the current pydantic model — residue
    from past renames where the old key wasn't explicitly popped, or
    fields that were dropped between releases. Without this, a user's
    settings.json accumulates dead keys forever; with it, the file
    self-cleans on every upgrade.

    The schema_version key is preserved so the version-tag mechanism
    keeps working across this cleanup. Anything else not in the
    current model is silently removed (it had no effect anyway —
    pydantic ignores unknown fields)."""
    known = set(_EnvSettings.model_fields.keys()) | {"_schema_version"}
    return {k: v for k, v in data.items() if k in known}


_MIGRATIONS: list[Callable[[dict], dict]] = [
    _rename_translation_provider_claude_to_llm,
    _split_unified_llm_into_per_function_slots,
    _drop_shared_anthropic_api_key,
    _rename_emby_to_media_server,
    _drop_unknown_keys,
]


settings = SettingsStore(_EnvSettings())
