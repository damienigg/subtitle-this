"""Inspect what device an optimum-intel OVModel actually got placed on.

When you call `.from_pretrained(..., device='AUTO')`, OpenVINO's AUTO plugin
silently picks GPU or CPU based on heuristics — and never tells you which one
it picked. That's a debugging black hole when "AUTO" lands on CPU even though
the iGPU is available, because the user just sees a slow, CPU-pinned job and
no signal that anything went wrong.

This helper digs the actual selected device out of the underlying CompiledModel
and logs it. Called once per model load (on cache miss), so the cost is zero
in steady state.

Output goes to the `subtitle_this` logger → docker logs → user. Format:

    [openvino] whisper:small  requested=AUTO  selected=GPU
    [openvino] nllb:facebook/nllb-200-distilled-600M  requested=AUTO  selected=CPU

If introspection fails (different optimum-intel version, internal API drift),
we log a warning instead of raising — knowing the device is nice-to-have, not
load-bearing.
"""
import logging


_log = logging.getLogger("subtitle_this")


def log_selected_device(label: str, *, requested: str, model) -> None:
    """Log the OpenVINO device(s) actually executing this model.

    `label` is a short identifier for the model (e.g. "whisper:small"). It
    appears in the log line so multi-model deployments can tell which load
    landed where.

    `requested` is what the caller asked for ('AUTO' / 'GPU' / 'CPU' / etc.).

    `model` is the OVModel returned by optimum-intel. We poke through the
    inner CompiledModel and read its EXECUTION_DEVICES property (an OpenVINO
    convention for "which actual device did AUTO route inference to").
    """
    selected = _selected_devices(model)
    if selected is None:
        _log.warning(
            "[openvino] %s  requested=%s  selected=? "
            "(could not introspect — optimum-intel layout may have changed)",
            label, requested,
        )
        return
    _log.info(
        "[openvino] %s  requested=%s  selected=%s",
        label, requested, ",".join(selected) or "?",
    )


def _selected_devices(model) -> list[str] | None:
    """Best-effort traversal across optimum-intel model shapes.

    Different OVModel subclasses expose their CompiledModel(s) under
    different attributes, and the layout has shifted between optimum-intel
    versions. We try the known spots in order and bail out gracefully if
    none hit.

    Returns the list of execution devices (typically one entry like ['GPU']
    or ['CPU']) or None if nothing usable was found.
    """
    candidates = [
        # Whisper / encoder-decoder: separate request handles per submodel.
        # We surface the encoder's device since it's the heaviest part for
        # Whisper; if the decoder differs the user can dig further.
        getattr(getattr(model, "encoder", None), "request", None),
        getattr(getattr(model, "decoder", None), "request", None),
        # Generic seq2seq / single-graph models.
        getattr(model, "request", None),
        getattr(model, "compiled_model", None),
    ]
    for c in candidates:
        if c is None:
            continue
        try:
            value = c.get_property("EXECUTION_DEVICES")
        except Exception:
            continue
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return list(value)
    return None
