"""RTR — per-role model attribution for the retrodiction batch.

The batch runner scores a QUESTION once, but the pipeline behind it ran
THREE roles (Architect decomposed it, Manager synthesized, the Adversary
attacked). Empirical routing needs to know WHICH MODEL played WHICH ROLE,
so the wire between tools/retrodiction and tools/routing must capture role
usage at the pipeline's own seam: PipelineModel.complete(role, messages).

RoleTrackingModel wraps any PipelineModel. Every complete(role, ...) call is
recorded as (role -> backend model name, taken from the response's "model"
field when present). Nothing else about the wrapped model changes. The batch
then writes one score-store record PER ROLE actually used on that question —
so if one model played every role in a run, that becomes ONE observation
about that model in several roles, not several independent observations of
several models.

Honesty rules enforced here:
  - A question where all tracked roles were served by the SAME backend
    contributes ONE effective observation about that model per question;
    the per-role records are marked correlated=True with a shared run_id so
    downstream analysis can never count them as independent.
  - A question where roles genuinely ran on different backends contributes
    one observation per distinct backend.
  - Only questions that actually SCORED are recorded; nulls/refusals/errors
    leave no trace (absence of a record is honest).
"""

from __future__ import annotations

import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional


class RoleTrackingModel:
    """Transparent PipelineModel wrapper logging which model served which
    role. Wraps ANYTHING with async complete(role, messages, **kw) -> dict."""

    def __init__(self, inner):
        self._inner = inner
        self._lock = threading.Lock()
        # run_id -> Counter({role: n_calls})
        self.role_calls: dict[str, Counter] = {}
        # run_id -> {role: last backend model name reported ("" if none)}
        self.role_models_seen: dict[str, dict[str, str]] = {}

    @property
    def inner(self):
        return self._inner

    @property
    def name(self) -> str:
        return getattr(self._inner, "name", "wrapped")

    @property
    def results(self):
        # PipelineResearcher reads `researcher.results` after answer();
        # expose the wrapped researcher's list transparently.
        return getattr(self._inner, "results", None)

    @property
    def current_run_id(self) -> str | None:
        return self._run_id

    def start_run(self) -> str:
        """Begin a new attribution run (call before each question).
        One run == one question."""
        rid = uuid.uuid4().hex[:12]
        with self._lock:
            self.role_calls[rid] = Counter()
            self.role_models_seen[rid] = {}
            self._run_id = rid
        return rid

    async def complete(self, role: str, messages: list, **kwargs) -> dict:
        resp = await self._inner.complete(role, messages, **kwargs)
        with self._lock:
            self.role_calls.setdefault(self._run_id, Counter())[str(role)] += 1
            m = resp.get("model") if isinstance(resp, dict) else None
            self.role_models_seen.setdefault(
                self._run_id, {})[str(role)] = str(m or "")
        return resp


def roles_for_run(tracker: RoleTrackingModel,
                  run_id: str | None = None) -> dict[str, int]:
    """{role: n_complete_calls} for one run. Empty dict = nothing captured."""
    rid = run_id or tracker.current_run_id
    return dict(tracker.role_calls.get(rid, Counter()))


@dataclass
class RunAttribution:
    """What one scored question says about which models did what."""
    run_id: str
    #: {role: model_name} for every role that ran
    role_models: dict[str, str]
    #: number of DISTINCT backends across the run's roles
    n_distinct_models: int
    #: True when ONE backend played every tracked role — the resulting
    #: per-role records are CORRELATED, not independent evidence.
    single_model_run: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "role_models": dict(self.role_models),
            "n_distinct_models": self.n_distinct_models,
            "single_model_run": self.single_model_run,
            "notes": list(self.notes),
        }


def attribute_run(run_id: str,
                  roles_used: dict[str, int],
                  default_model: str,
                  role_models_seen: dict[str, str] | None = None) \
        -> RunAttribution:
    """Honest attribution for one scored question.

    `roles_used` is {role: call_count} captured by the tracker. Per-role
    model names come from `role_models_seen` (responses carrying "model",
    i.e. a router-backed pipeline); roles without a reported name fall back
    to `default_model` — the single configured backend, which is what served
    them in every deployment this repo has today.
    """
    seen = role_models_seen or {}
    role_models = {
        r: (seen.get(r) or default_model)
        for r, n in sorted(roles_used.items()) if n > 0
    }
    if not role_models:
        return RunAttribution(
            run_id=run_id, role_models={}, n_distinct_models=0,
            single_model_run=False,
            notes=["no complete() calls captured — nothing attributable"])
    distinct = set(role_models.values())
    notes = []
    if len(role_models) == 1:
        notes.append(f"only the {next(iter(role_models))} role ran")
    elif len(distinct) == 1:
        notes.append("one model played every role — these observations are "
                     "CORRELATED; count as ONE observation per question")
    return RunAttribution(run_id=run_id, role_models=role_models,
                          n_distinct_models=len(distinct),
                          single_model_run=(len(role_models) > 1
                                            and len(distinct) == 1),
                          notes=notes)


def effective_observation_count(attributions: list[RunAttribution],
                                model: str) -> int:
    """How many INDEPENDENT observations a set of runs gives about `model`.

    Runs where `model` played EVERY role count ONCE each (correlated), even
    though several per-role store records exist. This is the anti-inflation
    rule: routing must never claim n=90 from 30 questions one model ran
    end-to-end. In mixed runs (some roles on other models) each of this
    model's roles counts, because those judgments are separable by model.
    """
    n = 0
    for a in attributions:
        roles_of_model = [r for r, m in a.role_models.items() if m == model]
        if not roles_of_model:
            continue
        others_played = any(m != model for m in a.role_models.values())
        n += len(roles_of_model) if others_played else 1
    return n
