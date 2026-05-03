"""Scene-bible generation. Sends keyframes to the configured Vision LLM (any
backend the Vision-model slot points at — Anthropic native, OpenAI, Ollama,
LM Studio, etc.) and fills in Scene.description for each scene. Cached on disk
per (file fingerprint, vision LLM model id, detection threshold) so re-runs
reuse the bible across modes and target languages."""
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from app.config import settings
from app.pipeline.llm import (
    ImageContent, LLMError, SystemBlock, TextContent, get_vision_llm,
)
from app.pipeline.scenes import Scene
from app.pipeline.translate._util import batches
from app.pipeline.translate.base import TranslationError


_DESCRIBE_PROMPT = """For each frame, write a 1-2 sentence description capturing:
- Characters visible (count, age range, role/relationship if inferable)
- Setting (location, time of day, indoor/outdoor)
- Notable on-screen text (signs, notes, screens) — quote it exactly
- Mood/tone if it's clearly conveyed

Be concise and factual. Do not speculate beyond what's visible. Each description
goes to a translator who needs context for pronouns, gendered agreement, and the
broader scene — keep it terse and informational, not narrative."""


_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "description": {"type": "string"},
                },
                "required": ["index", "description"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scenes"],
    "additionalProperties": False,
}


def _bible_cache_path(media_fingerprint: str) -> Path:
    """Build the bible cache path. EVERY setting that affects bible content
    must be in the key — otherwise switching one in the UI silently serves
    a stale bible. See `_BIBLE_KEY_INPUTS` for the canonical list; add to
    that tuple when introducing a new setting that influences bible output."""
    parts = [f"bible|{media_fingerprint}"]
    for label, value in _BIBLE_KEY_INPUTS():
        parts.append(f"{label}={value}")
    raw = "|".join(parts)
    key = hashlib.sha256(raw.encode()).hexdigest()[:24]
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    return settings.cache_dir / f"scene-bible-{key}.json"


def _BIBLE_KEY_INPUTS() -> list[tuple[str, str]]:
    """All settings that affect the generated bible. Order doesn't matter
    for correctness but matters for the resulting key being stable across
    runs, so keep the order fixed.
    - vision_llm_model: the LLM that produces the scene descriptions
    - scene_detection_threshold: changes which shots are detected
    - scene_min_length_seconds: filters short shots from the bible
    - scene_max_scenes: hard cap on number of scenes
    - scene_keyframe_position: which frame of each scene is described
    - scene_frame_max_size: image resolution sent to the vision LLM
    - scene_bible_batch_size: scenes per LLM call (changes prompt structure)
    """
    return [
        ("vllm",  settings.vision_llm_model),
        ("thr",   f"{settings.scene_detection_threshold:.3f}"),
        ("min",   f"{settings.scene_min_length_seconds:.3f}"),
        ("max",   str(settings.scene_max_scenes)),
        ("kfpos", settings.scene_keyframe_position),
        ("kfsz",  str(settings.scene_frame_max_size)),
        ("batch", str(settings.scene_bible_batch_size)),
    ]


def load_cached_bible(media_fingerprint: str) -> list[Scene] | None:
    p = _bible_cache_path(media_fingerprint)
    if not p.exists():
        return None
    try:
        return [Scene(**d) for d in json.loads(p.read_text())]
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def store_cached_bible(media_fingerprint: str, scene_list: list[Scene]) -> None:
    _bible_cache_path(media_fingerprint).write_text(
        json.dumps([asdict(s) for s in scene_list])
    )


def _noop_cancel() -> None: ...


def describe_scenes(
    scene_list: list[Scene],
    keyframes: dict[int, bytes],
    *,
    check_cancel: Callable[[], None] = _noop_cancel,
) -> list[Scene]:
    """Fill in Scene.description for each scene whose keyframe bytes are in
    `keyframes` (mapping scene.index → JPEG bytes). Modifies in place.

    Calls `check_cancel` between LLM batches so a cancel click during a long
    bible build (5-15 min on a feature film) takes effect within one batch
    instead of blocking until the whole bible is done."""
    try:
        client = get_vision_llm()
    except LLMError as e:
        raise TranslationError(str(e)) from e

    by_index = {s.index: s for s in scene_list}
    system = [SystemBlock(text=_DESCRIBE_PROMPT, cacheable=True)]

    for batch in batches(scene_list, settings.scene_bible_batch_size):
        check_cancel()
        content = []
        for scene in batch:
            kf = keyframes.get(scene.index)
            if not kf:
                continue
            content.append(TextContent(text=f"Scene {scene.index}:"))
            content.append(ImageContent(data=kf, media_type="image/jpeg"))
        if not content:
            continue

        try:
            text = client.chat(
                system=system, content=content,
                max_tokens=8000, response_schema=_OUTPUT_SCHEMA,
            )
        except LLMError as e:
            raise TranslationError(f"Scene-bible generation failed: {e}") from e

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise TranslationError(f"LLM returned invalid JSON for scenes: {e}") from e

        for entry in parsed.get("scenes", []):
            scene = by_index.get(entry.get("index"))
            if scene:
                scene.description = entry.get("description") or ""

    return scene_list
