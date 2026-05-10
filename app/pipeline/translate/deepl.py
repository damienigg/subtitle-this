from typing import Callable

import httpx

from app.config import settings
from app.pipeline.stt import Cue
from app.pipeline.translate._util import batches
from app.pipeline.translate.base import TranslationError


def _noop_progress(frac: float) -> None: ...
def _noop_cancel() -> None: ...


# DeepL request constants. Per the DeepL API docs, a single /v2/translate
# call accepts up to 50 `text=` fields; the batch size is configurable via
# settings.deepl_batch_size for users who want smaller batches (more
# granular retry behavior at the cost of more round-trips).
_DEEPL_TIMEOUT = 60.0      # seconds

# ISO 639-1 -> DeepL language code. DeepL uses uppercase ISO 639-1, with a
# few exceptions (no -> NB, etc.).
_TARGET_LANG = {
    "en": "EN-US", "fr": "FR", "de": "DE", "es": "ES", "it": "IT", "pt": "PT-PT",
    "ja": "JA", "zh": "ZH", "ru": "RU", "pl": "PL", "nl": "NL", "sv": "SV",
    "da": "DA", "fi": "FI", "no": "NB", "cs": "CS", "el": "EL", "hu": "HU",
    "ro": "RO", "sk": "SK", "sl": "SL", "et": "ET", "lv": "LV", "lt": "LT",
    "bg": "BG", "tr": "TR", "uk": "UK", "id": "ID", "ko": "KO", "ar": "AR",
}
# Source language is plain ISO 639-1 uppercase, no regional variant.
_SOURCE_LANG = {k: k.upper() if "-" not in v else v.split("-")[0] for k, v in _TARGET_LANG.items()}


class DeepLProvider:
    def __init__(self) -> None:
        key = settings.deepl_api_key
        if not key:
            raise TranslationError("BABEL_DEEPL_API_KEY is not set")
        # DeepL convention: free-tier keys end in ":fx"
        endpoint = (
            "https://api-free.deepl.com/v2/translate"
            if key.endswith(":fx")
            else "https://api.deepl.com/v2/translate"
        )
        self._endpoint = endpoint
        self._client = httpx.Client(
            headers={"Authorization": f"DeepL-Auth-Key {key}"},
            timeout=_DEEPL_TIMEOUT,
        )

    def translate(
        self,
        cues: list[Cue],
        source_lang: str,
        target_lang: str,
        context=None,
        *,
        progress: Callable[[float], None] = _noop_progress,
        check_cancel: Callable[[], None] = _noop_cancel,
    ) -> list[Cue]:
        # DeepL is text-only — `context` (scene bible / per-cue frames) is silently
        # ignored. The processor enforces that scene/cinematic modes use the LLM provider.
        if target_lang not in _TARGET_LANG:
            raise TranslationError(f"DeepL does not support target language {target_lang!r}")

        out: list[Cue] = []
        total = max(1, len(cues))
        batch_size = max(1, min(50, int(settings.deepl_batch_size or 50)))
        for batch in batches(cues, batch_size):
            check_cancel()
            out.extend(self._translate_batch(batch, source_lang, target_lang))
            progress(len(out) / total)
        progress(1.0)
        return out

    def _translate_batch(self, batch: list[Cue], source_lang: str, target_lang: str) -> list[Cue]:
        # Use a list of (key, value) tuples so 'text' can repeat — httpx encodes
        # this as text=...&text=... which is what DeepL expects.
        data: list[tuple[str, str]] = [("text", c.text) for c in batch]
        data.append(("target_lang", _TARGET_LANG[target_lang]))
        if source_lang in _SOURCE_LANG:
            data.append(("source_lang", _SOURCE_LANG[source_lang]))
        data.append(("preserve_formatting", "1"))
        data.append(("split_sentences", "0"))   # one cue in -> one cue out

        try:
            resp = self._client.post(self._endpoint, data=data)
        except httpx.HTTPError as e:
            raise TranslationError(f"DeepL request failed: {type(e).__name__}: {e}") from e
        if resp.status_code != 200:
            raise TranslationError(f"DeepL HTTP {resp.status_code}: {resp.text}")

        try:
            translations = resp.json().get("translations", [])
        except ValueError as e:
            raise TranslationError(f"DeepL returned invalid JSON: {e}") from e
        if len(translations) != len(batch):
            raise TranslationError(
                f"DeepL returned {len(translations)} translations for {len(batch)} cues"
            )
        return [
            Cue(id=c.id, start=c.start, end=c.end, text=t["text"])
            for c, t in zip(batch, translations)
        ]
