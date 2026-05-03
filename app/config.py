"""Layered settings: code defaults < env vars < /cache/settings.json (mutated by UI).

Existing code reads `from app.config import settings` and uses attribute access
(`settings.whisper_model`). The `Settings` object below is a SettingsStore that
proxies attribute access — values written via the UI override env-bound defaults.
"""
import json
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class _EnvSettings(BaseSettings):
    """Bootstrap defaults from env vars. Never written to at runtime."""
    model_config = SettingsConfigDict(env_prefix="BABEL_", env_file=".env", extra="ignore")

    cache_dir: Path = Path("/cache")

    # STT
    whisper_backend: str = "cpu"
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    openvino_device: str = "GPU"

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
    translation_batch_size: int = 30

    # Subtitle formatting
    max_line_chars: int = 42
    max_lines_per_cue: int = 2

    # Defaults applied when the user clicks "Subtitle this" or "Sweep library"
    # in the web UI without overriding per-item.
    default_target_lang: str = "fr"
    default_source_lang_priority: list[str] = ["en", "ja", "*"]
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
    scene_detection_threshold: float = 0.4
    scene_min_length_seconds: float = 1.5
    scene_max_scenes: int = 500
    scene_keyframe_position: str = "midpoint"      # start | midpoint | end
    scene_frame_max_size: int = 1024               # long-edge px sent to the vision LLM
    scene_bible_batch_size: int = 10               # scenes per vision LLM call

    # Cinematic mode (per-cue frame attachment). Smaller frames + smaller batches
    # because each call ships up to N images.
    cinematic_frame_max_size: int = 768
    cinematic_batch_size: int = 10

    # Media server (Emby / Jellyfin / Plex). Type drives:
    # - which client class is built (Emby+Jellyfin share one impl, Plex has its own)
    # - the auth header convention (X-Emby-Token vs X-Plex-Token)
    # - UI label cosmetics (badge says "Emby"/"Jellyfin"/"Plex")
    # The api_key field doubles as the Plex token when type=plex.
    media_server_type: str = "emby"   # emby | jellyfin | plex
    media_server_url: str | None = None
    media_server_api_key: str | None = None


# Set of fields that are sensitive — masked in UI GET responses, password input on edit.
SENSITIVE_FIELDS: set[str] = {
    "translation_llm_api_key",
    "vision_llm_api_key",
    "deepl_api_key",
    "media_server_api_key",
}

# Set of fields the UI cannot edit (operator-only via env).
READ_ONLY_FIELDS: set[str] = {"cache_dir"}


class SettingsStore:
    """Attribute-access proxy over env defaults + persisted user overrides."""

    def __init__(self, env_settings: _EnvSettings) -> None:
        self._env = env_settings
        self._file: Path = env_settings.cache_dir / "settings.json"
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

        # Validate the post-update merged state against the pydantic schema.
        proposed = {**self._env.model_dump(), **self._overrides, **kvs}
        try:
            _EnvSettings.model_validate(proposed)
        except Exception as e:
            raise ValueError(f"Invalid setting value: {e}") from e

        self._overrides.update(kvs)
        self._save()

    def reset(self, key: str) -> None:
        self._overrides.pop(key, None)
        self._save()

    def reset_all(self) -> None:
        self._overrides = {}
        self._save()

    # ── internals ──────────────────────────────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        if not self._file.exists():
            return {}
        try:
            data = json.loads(self._file.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

        # Migration: claude → llm provider rename
        if data.get("default_translation_provider") == "claude":
            data["default_translation_provider"] = "llm"

        # Migration: collapse the old single-LLM config (llm_backend +
        # claude_model + openai_compat_*) into the per-function slots
        # (translation_llm_*, vision_llm_*). Run only if any old key is present.
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

        # Migration: drop the shared anthropic_api_key fallback. Each slot now
        # carries its own key. Copy the legacy shared key into both slots when
        # they don't already have one.
        old_anthropic_key = data.pop("anthropic_api_key", None)
        if old_anthropic_key:
            data.setdefault("translation_llm_api_key", old_anthropic_key)
            data.setdefault("vision_llm_api_key", old_anthropic_key)

        # Migration: emby_url / emby_api_key were renamed to media_server_url /
        # media_server_api_key when we generalized to Emby + Jellyfin + Plex.
        # Existing deployments had their server pre-configured under the old
        # names; copy them over and default the new server-type to 'emby'
        # (since that's what they were using).
        old_emby_url = data.pop("emby_url", None)
        old_emby_key = data.pop("emby_api_key", None)
        if old_emby_url is not None:
            data.setdefault("media_server_url", old_emby_url)
        if old_emby_key is not None:
            data.setdefault("media_server_api_key", old_emby_key)
        if (old_emby_url or old_emby_key) and "media_server_type" not in data:
            data["media_server_type"] = "emby"

        return data

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(self._overrides, indent=2, sort_keys=True))


settings = SettingsStore(_EnvSettings())
