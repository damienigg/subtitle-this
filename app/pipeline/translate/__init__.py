from app.pipeline.translate.base import TranslationError, TranslationProvider


def get_provider(name: str) -> TranslationProvider:
    """Build a fresh translation provider by short name. The legacy `claude`
    string is rewritten to `llm` by the settings.json migration in
    app.config._load() before any consumer lands here, so we don't need to
    accept it as a synonym at this layer."""
    name = (name or "").lower()
    if name == "llm":
        from app.pipeline.translate.llm import LLMTranslationProvider
        return LLMTranslationProvider()
    if name == "deepl":
        from app.pipeline.translate.deepl import DeepLProvider
        return DeepLProvider()
    if name == "nllb":
        from app.pipeline.translate.nllb import NLLBProvider
        return NLLBProvider()
    raise ValueError(f"Unknown translation provider: {name!r}. Choose llm, deepl, or nllb.")


__all__ = ["TranslationProvider", "TranslationError", "get_provider"]
