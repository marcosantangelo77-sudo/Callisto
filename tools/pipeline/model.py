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
    """First parseable JSON object in *text*, or None.

    Robust to prose wrapping (battery finding D3): hermes_cli and chatty
    backends wrap the JSON verdict in leading commentary, trailing
    explanation, or markdown fences. Strategy, most-specific first:
      1. whole text parses (the clean case);
      2. each ```fenced``` block;
      3. every balanced {...} candidate — an earlier UNPARSEABLE candidate
         no longer poisons the scan (a prose sentence like "result {see
         below}" used to abort extraction entirely).
    Returns None only when nothing in the text is a valid JSON object.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    def _try(candidate: str) -> Optional[dict]:
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        obj = _try(m.group(1))
        if obj is not None:
            return obj

    # Every balanced-brace span; skip candidates that fail to parse instead
    # of giving up (the old first-candidate-or-None behaviour).
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        esc = False
        end = -1
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
                    end = i
                    break
        if end < 0:
            break  # unmatched brace — no further complete candidate possible
        obj = _try(text[start:end + 1])
        if obj is not None:
            return obj
        start = text.find("{", start + 1)
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
    turns they care about). Every call is recorded for assertions.

    TAGGED QUEUES (speed run 2026-08-23): the parallel pipeline issues
    concurrent per-leaf calls, so arrival order no longer identifies the
    leaf. A test that needs DIFFERENT responses per leaf registers them
    with script_for(tag, role, ...) and the engine passes _call_tag;
    untagged calls keep the exact legacy FIFO behaviour.
    """

    name = "scripted"

    def __init__(self, responses: Optional[dict[str, list]] = None,
                 default: Optional[dict] = None):
        self.responses: dict[str, list] = {k: list(v) for k, v in
                                           (responses or {}).items()}
        self.default = default or {"content": "{}"}
        self.calls: list[tuple[str, str]] = []
        self.tagged_responses: dict[tuple[str, str], list] = {}

    def script(self, role: str, *responses) -> "ScriptedModel":
        self.responses.setdefault(role, []).extend(responses)
        return self

    def script_for(self, tag: str, role: str, *responses) -> "ScriptedModel":
        """Responses consumed FIFO by (tag, role) before the legacy queue."""
        self.tagged_responses.setdefault((tag, role), []).extend(responses)
        return self

    async def complete(self, role: str, messages: list[dict],
                       *, _call_tag: Optional[str] = None,
                       **_ignored) -> dict:
        prompt = "\n".join(m.get("content", "") for m in messages)
        self.calls.append((role, prompt[:200]))
        resp = None
        if _call_tag is not None:
            tq = self.tagged_responses.get((_call_tag, role))
            if tq:
                resp = tq.pop(0)
        if resp is None:
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


def decompose_messages(root_query: str) -> list[dict]:
    return [{"role": "system", "content": DECOMPOSE_SYSTEM},
            {"role": "user", "content": f"QUESTION: {root_query}"}]


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
    "computation must run before an answer exists. Also return "
    "\"answers_question\": true | false — DECLARED, structural, and read "
    "as-is: does the evidence actually bear on and settle THIS question? "
    "Excellent provenance does not make evidence an answer; if the items "
    "determine nothing about the question asked (wrong period, wrong "
    "measure, intraday asked but only daily published), say false even if "
    "they are authentic and topically adjacent. Never leave it absent."
)


def answer_messages(question_text: str, evidence_items: list[str]) -> list[dict]:
    ev = "\n".join(f"- [{i}] {e}" for i, e in enumerate(evidence_items)) or "(none)"
    return [{"role": "system", "content": ANSWER_SYSTEM},
            {"role": "user",
             "content": f"QUESTION: {question_text}\nEVIDENCE:\n{ev}"}]
