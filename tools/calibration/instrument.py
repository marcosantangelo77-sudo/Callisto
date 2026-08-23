"""Instrumentation: capture the RAW model estimate before it is clamped.

Finding #1 of the underconfidence investigation: engine._answer_leaf reads
`proposed_confidence` from the model's parsed proposal (tools/pipeline/
engine.py:379), immediately clamps it, and DISCARDS the raw value — the one
number calibration needs to be scored on does not survive the run. The
smoke5 batch therefore cannot be rescored on estimates; only reconstructed.

This module recovers the number WITHOUT editing tools/pipeline/ or
agp/ (owned concurrently by other instances): wrap_model(model) returns a
proxy whose complete() sniffs `proposed_confidence` out of each response —
the exact same parse the engine runs, applied to the same response object —
and appends it to a caller-supplied log. Stock behaviour is otherwise
untouched; unwrapping restores byte-identical behaviour.

Usage:
    from tools.calibration.instrument import wrap_model
    raw_log: list[dict] = []
    model = wrap_model(HermesCliModel(...), raw_log)
    researcher = PipelineResearcher(model=model, ...)
"""
from __future__ import annotations


def _sniff(resp, raw_log: list) -> None:
    """Extract proposed_confidence from a model response, if present.

    Uses tools.pipeline.model.parse_model_json — the SAME parser the engine
    uses — so what we record is exactly what the engine saw.
    """
    try:
        from tools.pipeline.model import parse_model_json
        proposal = parse_model_json(resp if isinstance(resp, dict) else {})
        if proposal and "proposed_confidence" in proposal:
            raw_log.append({"raw_estimate":
                            float(proposal["proposed_confidence"])})
    except Exception:  # noqa: BLE001 — measurement must never break a run
        pass


def wrap_model(model, raw_log: list):
    """Return a proxy around `model` that logs every raw pre-clamp estimate.
    All other attributes pass through unchanged."""
    class _Instrumented(type(model)):         # same interface, sniffing added
        async def complete(self, role, messages, **kw):
            resp = await super().complete(role, messages, **kw)
            _sniff(resp, raw_log)
            return resp

    inst = _Instrumented.__new__(_Instrumented)
    inst.__dict__.update(model.__dict__)
    return inst
