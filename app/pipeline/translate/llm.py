"""LLM-backed translation provider. Delegates to the configured LLM backend
(Anthropic native or OpenAI-compatible) via app.pipeline.llm. The translation
prompt and JSON schema are the same regardless of backend; only the wire
format differs."""
import json
from typing import Callable

from app.config import settings
from app.pipeline.llm import (
    ContentBlock, ImageContent, LLMError, SystemBlock, TextContent, get_translation_llm,
)
from app.pipeline.stt import Cue
from app.pipeline.translate._util import batches
from app.pipeline.translate.base import TranslationContext, TranslationError


def _noop_progress(frac: float) -> None: ...
def _noop_cancel() -> None: ...


_SYSTEM_PROMPT = """You are a professional subtitle translator producing high-quality dialogue subtitles for an audiovisual production.

# Translation principles
- Produce natural, idiomatic target-language phrasing. Avoid word-for-word literal translation when it sounds unnatural.
- Preserve speaker tone, register (formal/informal), and emotional content of the original line.
- Use cultural and idiomatic equivalents for slang, idioms, and culturally-specific references when a direct translation would lose meaning.
- Preserve proper nouns (names of people, places, brands) unless the target language has an established convention for them.
- For ambiguous gender or number, choose the most natural option in the target language given the surrounding context.
- When the source uses profanity, render it at the same intensity in the target language. Do not soften or strengthen.
- Honorifics, titles, and forms of address should be adapted to target-language conventions.
- Numbers, dates, currencies, and units of measurement follow target-language formatting conventions when natural to do so.

# Subtitle constraints
- Cues must be concise. Prefer shorter, punchier translations over wordy ones; subtitles compete with the picture for the viewer's attention.
- Match the emotional pacing of the source: short staccato lines must remain short.
- Avoid restating information that is already obvious from preceding cues.
- Punctuation should follow target-language conventions, not the source's.

# Optional context (scene/cinematic modes)
- A `scene bible` (a list of {index, start, end, description}) may be included in the system prefix. It describes what is visible at each moment in the film. Use it to disambiguate pronouns, choose correct gendered/numbered agreement, and identify referents like "this", "that", "here", "she", "he", "they".
- Each cue in the input may carry an extra `scene` field — the description of the scene that cue belongs to. This is shorthand for the relevant bible entry.
- Image blocks labelled "Frame for cue N:" may appear before the cue payload. When present, they show the on-screen moment for that cue. Use them to inform translation choices: who is on screen, where, doing what, and any visible text or signage.
- Never translate the bible, scene fields, or labels. They are context only.

# Output format
You will receive a JSON array of subtitle cues, each with an integer `id` and a `text` field (and possibly a `scene` field, which is context — not for translation).
Return a JSON object with a `translations` array of the same length, in the same order, where each entry has the matching `id` and the translated `text`.
Do not add, remove, reorder, merge, or split cues. The output array length must exactly equal the input array length, with the same ids in the same order.
"""


_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


class LLMTranslationProvider:
    def __init__(self) -> None:
        try:
            self._client = get_translation_llm()
        except LLMError as e:
            raise TranslationError(str(e)) from e

    def translate(
        self,
        cues: list[Cue],
        source_lang: str,
        target_lang: str,
        context: TranslationContext | None = None,
        *,
        progress: Callable[[float], None] = _noop_progress,
        check_cancel: Callable[[], None] = _noop_cancel,
    ) -> list[Cue]:
        cinematic = bool(context and context.cue_frames)
        if cinematic and not self._client.supports_vision():
            raise TranslationError(
                "cinematic mode attaches per-cue frames to translation calls, so "
                "the translation LLM must be vision-capable. Either switch the "
                "translation LLM type to anthropic, pick a vision-capable openai_compat "
                "model (gpt-4o, qwen2.5-vl, etc.), or toggle on "
                "translation_llm_supports_vision in Settings."
            )
        batch_size = settings.cinematic_batch_size if cinematic else settings.translation_batch_size

        out: list[Cue] = []
        total = max(1, len(cues))
        for batch in batches(cues, batch_size):
            check_cancel()
            out.extend(self._translate_batch(batch, source_lang, target_lang, context))
            progress(len(out) / total)
        progress(1.0)
        return out

    def _translate_batch(
        self,
        batch: list[Cue],
        source_lang: str,
        target_lang: str,
        context: TranslationContext | None,
    ) -> list[Cue]:
        scene_by_index = {s.index: s for s in (context.scenes if context else [])}
        payload = []
        for c in batch:
            entry: dict = {"id": c.id, "text": c.text}
            if context and c.id in context.cue_to_scene:
                scene = scene_by_index.get(context.cue_to_scene[c.id])
                if scene and scene.description:
                    entry["scene"] = scene.description
            payload.append(entry)

        # Build the user content: optional per-cue frames first, then the JSON payload.
        user_content: list[ContentBlock] = []
        if context and context.cue_frames:
            for c in batch:
                kf = context.cue_frames.get(c.id)
                if not kf:
                    continue
                user_content.append(TextContent(text=f"Frame for cue {c.id}:"))
                user_content.append(ImageContent(data=kf, media_type="image/jpeg"))
        user_content.append(TextContent(text=json.dumps(payload, ensure_ascii=False)))

        # Build the system: principles (cacheable) + optional scene bible (cacheable)
        # + per-job lang config (cacheable, last → caches everything before too).
        system: list[SystemBlock] = [SystemBlock(text=_SYSTEM_PROMPT, cacheable=True)]
        if context and context.scenes:
            bible = [
                {"index": s.index, "start": round(s.start, 2), "end": round(s.end, 2),
                 "description": s.description}
                for s in context.scenes if s.description
            ]
            if bible:
                system.append(SystemBlock(
                    text=("Scene bible (visual context for the whole film — do not translate this):\n"
                          + json.dumps(bible, ensure_ascii=False)),
                    cacheable=True,
                ))
        system.append(SystemBlock(
            text=f"Source language: {source_lang}\nTarget language: {target_lang}",
            cacheable=True,
        ))

        try:
            text = self._client.chat(
                system=system,
                content=user_content,
                max_tokens=16000,
                response_schema=_OUTPUT_SCHEMA,
            )
        except LLMError as e:
            raise TranslationError(str(e)) from e

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise TranslationError(f"LLM returned invalid JSON: {e}") from e

        translations = parsed.get("translations", [])
        if len(translations) != len(batch):
            raise TranslationError(
                f"Length mismatch: expected {len(batch)} cues, got {len(translations)}"
            )

        by_id = {t["id"]: t["text"] for t in translations}
        out: list[Cue] = []
        for c in batch:
            if c.id not in by_id:
                raise TranslationError(f"Missing translation for cue id {c.id}")
            out.append(Cue(id=c.id, start=c.start, end=c.end, text=by_id[c.id]))
        return out
