"""NLLB-200 (Meta's No Language Left Behind) translation provider.

Runs fully locally — no API keys, no accounts, no cloud calls. Two backends,
auto-selected at load time:

- `optimum-intel` (preferred, openvino image): exports the model to OpenVINO
  IR and runs the encoder/decoder on the Intel iGPU. 5-10× faster than the
  CPU fallback for typical N305-class hardware.
- `transformers` (CPU image fallback): plain PyTorch on the CPU. Slower
  (a 1000-cue film takes ~5-10 min on a modern CPU) but means the default
  `nllb` provider works on the CPU image too without requiring the user to
  switch to deepl/llm.

Either backend exposes the same `model.generate(...)` API, so the inference
loop below doesn't need to know which one it got.
"""
from functools import lru_cache
from typing import Callable

from app.config import settings
from app.pipeline.openvino_introspect import log_selected_device
from app.pipeline.stt import Cue
from app.pipeline.translate.base import TranslationError


def _noop_progress(frac: float) -> None: ...
def _noop_cancel() -> None: ...


# ISO 639-1 -> FLORES-200 code (the 30 most common languages NLLB-200 supports).
# The full list (200 langs) is in NLLB-200's docs; extend this map as needed.
_FLORES = {
    "en": "eng_Latn", "fr": "fra_Latn", "es": "spa_Latn", "de": "deu_Latn",
    "it": "ita_Latn", "pt": "por_Latn", "ru": "rus_Cyrl", "ja": "jpn_Jpan",
    "ko": "kor_Hang", "zh": "zho_Hans", "ar": "arb_Arab", "hi": "hin_Deva",
    "tr": "tur_Latn", "vi": "vie_Latn", "th": "tha_Thai", "pl": "pol_Latn",
    "nl": "nld_Latn", "sv": "swe_Latn", "no": "nob_Latn", "da": "dan_Latn",
    "fi": "fin_Latn", "cs": "ces_Latn", "el": "ell_Grek", "he": "heb_Hebr",
    "hu": "hun_Latn", "ro": "ron_Latn", "uk": "ukr_Cyrl", "id": "ind_Latn",
    "ms": "zsm_Latn", "tl": "tgl_Latn", "ca": "cat_Latn", "bn": "ben_Beng",
}

# Max input + output tokens per NLLB call. Subtitle cues are short — almost
# always under 30 words in source language, rarely exceeding 50 tokens after
# tokenization, and the translated output is similarly bounded. 128 covers
# every realistic cue with comfortable margin; the old 256 doubled the KV
# cache footprint without ever being needed in practice. KV cache scales
# linearly with seq_len, so halving this halves the activation memory peak.
_MAX_LEN = 128


@lru_cache(maxsize=1)
def _model_and_tokenizer(model_id: str, device: str, cache_root: str, load_in_8bit: bool):
    """Cache keyed by config so settings changes reload the model. Tries the
    OpenVINO-accelerated backend first; falls back to plain PyTorch transformers
    on the CPU image. Both backends are available out of the box — the default
    `nllb` provider works on either image flavor.

    maxsize=1 — switching nllb_model in the UI evicts the previous variant
    rather than keeping both resident. NLLB-1.3B is ~3 GB; the user toggling
    sizes shouldn't double their RAM footprint.

    load_in_8bit: when True (default for the OV path), weights are compressed
    to int8 via NNCF at load. Cuts resident weight memory in half — essential
    on a 12 GB cgroup where the fp32 1.3B variant blew through the cap on top
    of Whisper's lingering page cache. Quality cost is ~0.3 BLEU, below the
    noise floor for subtitles. The CPU/torch fallback ignores this flag since
    bitsandbytes int8 is CUDA-only in practice and not in our base image."""
    from pathlib import Path

    try:
        from transformers import AutoTokenizer
    except ImportError as e:
        raise TranslationError(
            "NLLB requires the `transformers` package (and either `optimum-intel` for "
            "OpenVINO acceleration or `torch` for the CPU fallback). Both Docker images "
            "ship with these by default — install them in your environment, or pick the "
            "'deepl' or 'llm' provider in Settings."
        ) from e

    cache_dir = Path(cache_root) / "nllb-models"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(model_id, cache_dir=str(cache_dir))

    # Preferred: OpenVINO IR via optimum-intel (5-10× faster on Intel iGPU).
    try:
        from optimum.intel import OVModelForSeq2SeqLM
        kwargs = dict(
            export=True,
            device=device,
            cache_dir=str(cache_dir),
        )
        if load_in_8bit:
            # NNCF int8 weight compression. First-time export pays a 1-2 min
            # quantization cost; the resulting IR is cached so subsequent
            # loads are fast and the int8 weights persist on disk.
            kwargs["load_in_8bit"] = True
        model = OVModelForSeq2SeqLM.from_pretrained(model_id, **kwargs)
        log_selected_device(
            "nllb:" + model_id + ("/int8" if load_in_8bit else "/fp32"),
            requested=device, model=model,
        )
        return model, tokenizer
    except ImportError:
        pass   # CPU image — fall through to plain transformers below.

    # Fallback: plain PyTorch transformers on the CPU. Slower but means the
    # default `nllb` provider works on the CPU image too. load_in_8bit is
    # ignored here — bitsandbytes int8 is CUDA-only and not in our base image.
    try:
        from transformers import AutoModelForSeq2SeqLM
        model = AutoModelForSeq2SeqLM.from_pretrained(model_id, cache_dir=str(cache_dir))
        return model, tokenizer
    except ImportError as e:
        raise TranslationError(
            "NLLB needs `torch` for the CPU fallback (or `optimum-intel` for the "
            "OpenVINO-accelerated path). Install one, or pick 'deepl' / 'llm' in Settings."
        ) from e


def _load() -> tuple:
    return _model_and_tokenizer(
        settings.nllb_model,
        settings.openvino_device,
        str(settings.cache_dir),
        bool(settings.nllb_load_in_8bit),
    )


def _to_flores(code: str) -> str:
    if code not in _FLORES:
        raise TranslationError(
            f"NLLB language map does not include {code!r}. Extend _FLORES in nllb.py "
            f"with its FLORES-200 code (e.g. 'fra_Latn'). NLLB-200 supports 200 languages."
        )
    return _FLORES[code]


class NLLBProvider:
    def __init__(self) -> None:
        # Trigger the heavy import upfront so config errors fail at provider
        # construction rather than mid-translation.
        _load()

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
        # NLLB is text-only — `context` is silently ignored. The processor enforces
        # that scene/cinematic modes use the LLM provider.
        try:
            model, tokenizer = _load()

            src = _to_flores(source_lang)
            tgt = _to_flores(target_lang)
            tokenizer.src_lang = src
            tgt_token_id = tokenizer.convert_tokens_to_ids(tgt)

            import gc
            from app.pipeline.stt import try_malloc_trim

            out: list[Cue] = []
            total = max(1, len(cues))
            batch_size = max(1, int(settings.nllb_batch_size or 4))
            # Periodic cleanup interval. Every N batches we drop tensor
            # refs explicitly, run gc, and trim glibc arenas. Empirically
            # without this the resident set drifts upward through a long
            # translation (mmap page cache + allocator fragmentation) and
            # eventually trips a 12 GB cgroup with Whisper's pages also
            # still in cache. Every 10 batches keeps overhead negligible
            # while preventing the drift.
            CLEANUP_EVERY = 10
            for i in range(0, len(cues), batch_size):
                check_cancel()
                batch = cues[i:i + batch_size]
                inputs = tokenizer(
                    [c.text for c in batch],
                    return_tensors="pt",
                    padding=True,
                    truncation=True,
                    max_length=_MAX_LEN,
                )
                # num_beams=1 (greedy) instead of 2. The KV cache scales
                # linearly with beam count, so dropping to greedy halves
                # the activation memory peak for this call. Quality
                # difference on subtitle-length cues is negligible — beam
                # search shines on long-form generation where late tokens
                # can recover from early choices, but a 5-15-word
                # utterance rarely benefits.
                generated = model.generate(
                    **inputs,
                    forced_bos_token_id=tgt_token_id,
                    max_length=_MAX_LEN,
                    num_beams=1,
                )
                decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)
                for c, t in zip(batch, decoded):
                    out.append(Cue(id=c.id, start=c.start, end=c.end, text=t))
                progress(len(out) / total)

                # Explicit del so the next iteration's `inputs = ...` doesn't
                # have to wait for Python's refcount-drop to happen as a
                # side effect of rebinding. With NLLB-1.3B these tensors
                # hold a non-trivial chunk of activation memory, and on
                # OpenVINO the runtime keeps internal pools alive longer
                # than Python's refcount alone would suggest.
                del inputs, generated, decoded

                # Heavier cleanup at intervals. gc.collect walks cycles
                # that explicit del misses; try_malloc_trim returns the
                # freed glibc arenas to the kernel so the cgroup sees the
                # memory back instead of just inside this process.
                batch_index = i // batch_size
                if (batch_index + 1) % CLEANUP_EVERY == 0:
                    gc.collect()
                    try_malloc_trim()
            # Final cleanup before returning — the caller (processor) is
            # about to write the .vtt and then return through the runner,
            # so leaving any transient activation memory resident would
            # accumulate across back-to-back jobs.
            gc.collect()
            try_malloc_trim()
            progress(1.0)
            return out
        except TranslationError:
            raise
        except Exception as e:
            raise TranslationError(f"NLLB inference failed: {type(e).__name__}: {e}") from e
