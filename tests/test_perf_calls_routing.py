"""PERF — route by difficulty (tests/test_perf_calls_routing.py).

config/providers.yaml already declares task classes with a cheap grind tier
(gpu1_fast) for screening/extraction/classification and the strong tier for
framing and adversarial review. The pipeline's role->task_class mapping is
the routing decision; these pins hold RouterModel to it:

  - Manager (leaf answer = extraction grind) routes to a GRIND class
    first — it must never silently ride the strong synthesis class.
  - Architect (framing) and Adversary (criticism) route to judgment
    classes — difficulty routing must not downgrade the critic.
  - Overrides are explicit, per-role, and None restores the default.
"""
from __future__ import annotations

import asyncio

from agp.adversary import AGPRole
from tools.pipeline.model import RouterModel

GRIND_CLASSES = {"screening", "extraction", "classification"}
JUDGMENT_CLASSES = {"hypothesis_generation", "research_synthesis",
                    "adversarial_review"}


class RecordingRouter:
    """Stands in for ProviderRouter; records task_class per call."""

    name = "recording"

    def __init__(self):
        self.classes: list[str] = []

    async def complete(self, task_class, messages, **kw):
        self.classes.append(task_class)
        return {"content": "{}", "model": "recording"}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _msgs() -> list[dict]:
    return [{"role": "user", "content": "q"}]


def test_default_manager_routes_to_a_grind_class():
    r = RecordingRouter()
    m = RouterModel(r)
    _run(m.complete("Manager", _msgs()))
    assert r.classes[-1] in GRIND_CLASSES


def test_default_architect_and_adversary_route_to_judgment_classes():
    r = RecordingRouter()
    m = RouterModel(r)
    _run(m.complete("Architect", _msgs()))
    _run(m.complete("Adversary", _msgs()))
    assert r.classes[-2] in JUDGMENT_CLASSES
    assert r.classes[-1] in JUDGMENT_CLASSES


def test_role_difficulty_table_is_honest():
    d = RouterModel.ROLE_DIFFICULTY
    assert d["Manager"] == "grind"
    assert d["Architect"] == "judgment"
    assert d["Adversary"] == "judgment"
    # every AGP role the pipeline uses is classified
    for role in (AGPRole.ARCHITECT, AGPRole.MANAGER,
                 AGPRole.SENTINEL, AGPRole.ADVERSARY):
        assert role in d


def test_explicit_override_replaces_only_the_named_role():
    r = RecordingRouter()
    m = RouterModel(r, role_task_classes={
        "Manager": ["my_cheap_class"]})
    _run(m.complete("Manager", _msgs()))
    _run(m.complete("Architect", _msgs()))
    assert r.classes[-2] == "my_cheap_class"
    assert r.classes[-1] == AGPRole.ROLE_TASK_CLASSES["Architect"][0]


def test_none_override_restores_default():
    r = RecordingRouter()
    m = RouterModel(r, role_task_classes={"Manager": ["x"], "Sentinel": None})
    _run(m.complete("Sentinel", _msgs()))
    assert r.classes[-1] == AGPRole.ROLE_TASK_CLASSES["Sentinel"][0]
