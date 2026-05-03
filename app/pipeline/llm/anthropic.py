"""Anthropic-native LLM client. Preserves the Anthropic SDK's strengths —
adaptive thinking, prompt caching, strict JSON-schema output enforcement."""
import base64

import anthropic

from app.pipeline.llm.base import (
    ContentBlock, ImageContent, LLMError, SystemBlock, TextContent,
)


# Aligned with OpenAICompatLLM: explicit timeout so a wedged backend doesn't
# park a job indefinitely. 5 min is generous for a 30-cue batch generation.
_LLM_TIMEOUT_SECONDS = 300


class AnthropicLLM:
    def __init__(self, *, api_key: str, model: str) -> None:
        if not api_key:
            raise LLMError("Anthropic API key is not set")
        self._client = anthropic.Anthropic(api_key=api_key, timeout=_LLM_TIMEOUT_SECONDS)
        self._model = model

    def supports_vision(self) -> bool:
        return True

    def chat(
        self,
        *,
        system: list[SystemBlock],
        content: list[ContentBlock],
        max_tokens: int = 16000,
        response_schema: dict | None = None,
    ) -> str:
        last_cacheable_idx = -1
        for i, sb in enumerate(system):
            if sb.cacheable:
                last_cacheable_idx = i

        system_blocks: list[dict] = []
        for i, sb in enumerate(system):
            block: dict = {"type": "text", "text": sb.text}
            if i == last_cacheable_idx:
                block["cache_control"] = {"type": "ephemeral"}
            system_blocks.append(block)

        user_content: list[dict] = []
        for cb in content:
            if isinstance(cb, TextContent):
                user_content.append({"type": "text", "text": cb.text})
            elif isinstance(cb, ImageContent):
                user_content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": cb.media_type,
                        "data": base64.standard_b64encode(cb.data).decode(),
                    },
                })

        kwargs: dict = dict(
            model=self._model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            system=system_blocks,
            messages=[{"role": "user", "content": user_content}],
        )
        if response_schema is not None:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": response_schema},
            }

        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.APIError as e:
            raise LLMError(f"Anthropic API error ({type(e).__name__}): {e}") from e

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise LLMError("Anthropic returned no text block")
        return text
