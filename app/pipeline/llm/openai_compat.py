"""OpenAI-compatible LLM client. Works with OpenAI proper, Ollama, LocalAI,
OpenRouter, Together, Groq, Gemini's OpenAI-compat endpoint, vLLM, llama.cpp's
http server, and LM Studio — anyone who speaks Chat Completions.

Wire-format note: we send user content as a list of typed-dict parts
(`[{"type":"text", ...}, {"type":"image_url", ...}]`), which is the
documented OpenAI shape and is supported by every actively-maintained
OpenAI-compat server we test against (Ollama 0.5+, LM Studio 0.3+,
LocalAI 2.x, vLLM, OpenRouter, OpenAI proper). Very old llama.cpp HTTP
servers (pre-2024) and ancient Ollama (<0.4) only accepted a plain
string for `content` — if you hit a "messages.X.content: must be a
string" error from such a backend, upgrade the backend rather than
patching this client; the API has settled on the typed-list form."""
import base64
import json

import openai

from app.pipeline.llm.base import (
    ContentBlock, ImageContent, LLMError, SystemBlock, TextContent,
)


# Aligned with AnthropicLLM: explicit timeout so a wedged backend doesn't
# park a job indefinitely. 5 min is generous for a 30-cue batch generation.
_LLM_TIMEOUT_SECONDS = 300


class OpenAICompatLLM:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        supports_vision: bool = False,
    ) -> None:
        if not base_url:
            raise LLMError("OpenAI-compat base URL is not set")
        # Many local servers (Ollama, LocalAI) don't authenticate; the SDK
        # still requires a non-empty key, so substitute a placeholder.
        self._client = openai.OpenAI(
            base_url=base_url.rstrip("/"),
            api_key=api_key or "not-required",
            timeout=_LLM_TIMEOUT_SECONDS,
        )
        self._model = model
        self._supports_vision = supports_vision

    def supports_vision(self) -> bool:
        return self._supports_vision

    def chat(
        self,
        *,
        system: list[SystemBlock],
        content: list[ContentBlock],
        max_tokens: int = 16000,
        response_schema: dict | None = None,
    ) -> str:
        system_text = "\n\n".join(sb.text for sb in system)

        if response_schema is not None:
            # Most OpenAI-compat servers don't support `json_schema` strict mode.
            # Use `json_object` mode (broadly supported) and inject the schema
            # description into the system prompt so the model follows it.
            system_text += (
                "\n\nYou MUST respond with valid JSON matching this schema exactly. "
                "No prose, no code fences, just the JSON object:\n"
                + json.dumps(response_schema)
            )

        user_content: list[dict] = []
        for cb in content:
            if isinstance(cb, TextContent):
                user_content.append({"type": "text", "text": cb.text})
            elif isinstance(cb, ImageContent):
                b64 = base64.standard_b64encode(cb.data).decode()
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{cb.media_type};base64,{b64}"},
                })

        messages = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_content},
        ]

        kwargs: dict = dict(
            model=self._model,
            messages=messages,
            max_tokens=max_tokens,
        )
        if response_schema is not None:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = self._client.chat.completions.create(**kwargs)
        except openai.OpenAIError as e:
            # Symmetric with AnthropicLLM's `except anthropic.APIError`.
            # Bare `except Exception` was masking unrelated bugs as
            # LLMErrors; OpenAIError is the parent of every networking,
            # auth, rate-limit, and API-level failure the SDK raises.
            raise LLMError(f"OpenAI-compat API error ({type(e).__name__}): {e}") from e

        if not response.choices:
            raise LLMError("OpenAI-compat returned no choices")
        text = response.choices[0].message.content
        if not text:
            raise LLMError("OpenAI-compat returned empty content")
        return text
