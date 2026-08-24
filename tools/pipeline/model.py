"""The model seam. The pipeline never imports a provider.

Production passes an adapter over the ProviderRouter (provider-agnostic by
mandate — no hardcoded model anywhere). Tests pass ScriptedModel, a
deterministic fake whose responses are queued per role. Everything the
pipeline asks of "the model" is one of three narrow judgments:

  decompose  — turn the root question into sub-question specs (JSON)
  answer     — synthesize a leaf answer + proposed confidence (JSON)
  attack     — handled by agp.adversary.Adversary through its OWN router
               seam; the pipeline passes this same object there.
"""
from __future__ import annotations

import json
import re
from typing import Optional


class PipelineModel:
    """Interface: complete(role, messages) -> dict with at least 'content'.

    complete() also accepts **_ignored so it can stand in as an adversary
    backend (the Adversary calls complete(task_class, messages, schema=...)
    by keyword). A signature mismatch here used to surface as a fail-closed
    adversary veto rather than an error — much harder to diagnose."""

    name = "abstract"

    async def complete(self, role: str, messages: list[dict],
                       **_ignored) -> dict:
        raise NotImplementedError


def extract_json(text: str) -> Optional[dict]:
    """First balanced JSON object in *text*, or None."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_model_json(resp: dict) -> Optional[dict]:
    """parsed_json field if present, else first JSON object in content."""
    if isinstance(resp.get("parsed_json"), dict):
        return resp["parsed_json"]
    return extract_json(resp.get("content") or "")


class RouterModel(PipelineModel):
    """Adapter over anything exposing ``await complete(task_class, messages,
    schema=...)`` — i.e. inference.ProviderRouter. Roles map to task classes
    via agp.adversary.AGPRole.ROLE_TASK_CLASSES so model-per-role stays a
    config concern."""

    def __init__(self, router, role_task_classes: Optional[dict] = None):
        self.router = router
        self._rtc = dict(role_task_classes or {})
        if not self._rtc:
            from agp.adversary import AGPRole
            self._rtc = dict(AGPRole.ROLE_TASK_CLASSES)

    @property
    def name(self) -> str:  # type: ignore[override]
        return getattr(self.router, "name", "router")

    async def complete(self, role: str, messages: list[dict],
                       **_ignored) -> dict:
        task_class = self._rtc.get(role, [role])[0]
        return await self.router.complete(task_class, messages)


class ScriptedModel(PipelineModel):
    """Deterministic offline fake. Responses are consumed FIFO per role;
    when a queue is empty, `default` is returned (so tests script only the
    turns they care about). Every call is recorded for assertions."""

    name = "scripted"

    def __init__(self, responses: Optional[dict[str, list]] = None,
                 default: Optional[dict] = None):
        self.responses: dict[str, list] = {k: list(v) for k, v in
                                           (responses or {}).items()}
        self.default = default or {"content": "{}"}
        self.calls: list[tuple[str, str]] = []

    def script(self, role: str, *responses) -> "ScriptedModel":
        self.responses.setdefault(role, []).extend(responses)
        return self

    async def complete(self, role: str, messages: list[dict],
                       **_ignored) -> dict:
        prompt = "\n".join(m.get("content", "") for m in messages)
        self.calls.append((role, prompt[:200]))
        queue = self.responses.get(role)
        resp = queue.pop(0) if queue else self.default
        if isinstance(resp, str):
            resp = {"content": resp}
        return dict(resp)


# ── Prompt builders (shared by engine and tests) ──────────────────────────

DECOMPOSE_SYSTEM = (
    "You are the Architect. Decompose the research question into 2-5 "
    "sub-questions that would settle it. Return JSON only: "
    '{"sub_questions": [{"text": ..., "kind": "descriptive|causal|predictive", '
    '"question_type": short phrase naming what kind of source answers it, '
    '"min_source_tier": 1-3, "min_independent_sources": int, '
    '"quant_required": bool, "horizon_days": int or null}]}.'
    "\n\nHARD CONSTRAINT: if kind is \"predictive\", horizon_days MUST be a "
    "positive integer — an undated prediction cannot ever resolve, so it is "
    "rejected. If you cannot name a resolution horizon in days, the question "
    "is not predictive: use \"descriptive\" or \"causal\" instead."
)

# The diversity mandate is appended to the system prompt whenever a registry
# exists; DECOMPOSE_SYSTEM above stays the offline fallback shape.
DIVERSITY_MANDATE = (
    "\n\nDIVERSITY MANDATE: sub-questions must span SOURCE KINDS, not just "
    "facets of one topic — five sub-questions that all want scholarly papers "
    "are ONE independent voice no matter how many adapters exist.\n\n"
    "HONESTY CONSTRAINT: do not invent a source kind the question does not "
    "need. If the root question is genuinely answerable from one kind of "
    "source, a single-family decomposition is correct — say so via "
    "\"single_family_ok\": true rather than fabricating a market or news "
    "angle."
)


def _default_registry_or_none():
    """The live source registry, or None when it cannot be built offline."""
    try:
        from tools.sources.registry import get_source_registry
        return get_source_registry()
    except Exception:
        return None


def decompose_messages(root_query: str, registry=None) -> list[dict]:
    """Build the Architect conversation.

    The system prompt is the DIVERSITY form
    (tools.pipeline.decompose.build_decompose_system): it feeds the
    registry's own answer vocabulary and instructs the Architect to span
    source kinds — five sub-questions that all want scholarly papers are
    one independent voice no matter how many adapters exist. Falls back to
    the bare prompt only when no registry exists at all (offline unit
    contexts), so a missing catalog degrades to the pre-change behaviour
    rather than raising.
    """
    if registry is None:
        registry = _default_registry_or_none()
    if registry is None:
        return [{"role": "system", "content": DECOMPOSE_SYSTEM},
                {"role": "user", "content": f"QUESTION: {root_query}"}]
    from tools.pipeline.decompose import build_decompose_system
    return [
        {"role": "system",
         "content": DIVERSITY_MANDATE + "\n\n" +
                    build_decompose_system(registry)},
        {"role": "user", "content":
            f"QUESTION: {root_query}\n\n"
            "Decompose per the system mandate: span source kinds unless the "
            "question is honestly single-source."}]


ANSWER_SYSTEM = (
    "You are the research synthesizer. Given evidence items (each tagged "
    "with its provenance-assigned class), answer the question. Cite ONLY "
    "evidence you were given. If the evidence requires arithmetic, say "
    "COMPUTE and give python code instead of asserting numbers. Return "
    "JSON: {\"answer\": str, \"proposed_confidence\": float, "
    "\"stance\": \"AFFIRMS\" | \"DENIES\" | \"UNDETERMINED\", "
    "\"compute\": null | {\"code\": str, \"inputs\": {}}} "
    "stance is whether the EVIDENCE supports the claim in the question: "
    "AFFIRMS = yes it happened/is true, DENIES = no it did not, "
    "UNDETERMINED = the evidence does not settle it. Say UNDETERMINED "
    "whenever you are not answering the question asked — it is a real "
    "answer, not a failure. "
    "or {\"compute\": {\"code\":..., \"inputs\":...}, \"answer\": null} when "
    "computation must run before an answer exists."
)


def answer_messages(question_text: str, evidence_items: list[str]) -> list[dict]:
    ev = "\n".join(f"- [{i}] {e}" for i, e in enumerate(evidence_items)) or "(none)"
    return [{"role": "system", "content": ANSWER_SYSTEM},
            {"role": "user",
             "content": f"QUESTION: {question_text}\nEVIDENCE:\n{ev}"}]
