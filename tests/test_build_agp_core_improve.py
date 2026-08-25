"""AGP protocol core improve pass — review provenance ENFORCED, panel wired.

Family #1 / #4 hunt (PATTERNS.md): the engine's own seal-path comment claimed
"agp.ensemble marks same-model review as self_review and caps it at
SELF_REVIEW_CEILING" — but nothing called agp.ensemble. Both production ask
paths (--self-review: structural self-review; default: the SAME ProviderRouter
object acting as critic) sealed at full confidence while the record claimed a
0.54 ceiling. A check that exists, looks authoritative, and is inert.

Tests here are written failing-first against that gap:
  1. structural self-review (no adversary_router) -> ceiling actually applied
  2. shared-router critic (default `ask`)      -> ceiling actually applied
  3. distinct backend                          -> NOT capped (non-regression)
  4. AdversaryPanel wired through the engine   -> pooled verdicts recorded
  5. panel whose members resolve to one model  -> labelled self-review, capped
  6. property sweep                            -> application never raises
  7. `callisto ask --adversary-backend`        -> panel reachable from CLI
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from datetime import date
from contextlib import redirect_stderr, redirect_stdout

import pytest

from tests.helpers.no_socket import NoSocket

_guard = NoSocket()
_guard.install()

from agp import Domain, SourceClass  # noqa: E402
from agp.ensemble import (  # noqa: E402
    SELF_REVIEW_CEILING,
    AdversaryPanel,
)
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import (  # noqa: E402
    ResearchPipeline,
    fixture_transport,
)
from tools.pipeline.model import RouterModel, ScriptedModel  # noqa: E402

TODAY = date(2026, 8, 22)

OPENALEX_BODY = json.dumps({"results": [
    {"id": "W1", "title": "Semiconductor supply chain resilience review",
     "publication_year": 2025, "cited_by_count": 12},
]})


def _routes() -> dict[str, str]:
    return {"/works": OPENALEX_BODY}


def _decompose() -> str:
    return json.dumps({"sub_questions": [
        {"text": "what does scholarly research say about semiconductor "
                 "supply chain resilience",
         "kind": "descriptive",
         "question_type": "scholarly work search about semiconductors",
         "min_source_tier": 2, "min_independent_sources": 1},
    ]})


def _answer(conf=0.9) -> str:
    return json.dumps({"answer": "the literature supports the claim",
                       "proposed_confidence": conf})


class _QuietRouter:
    """Stands in for a ProviderRouter endpoint pool; answers every AGP role
    by task class (decompose / answer / approve) reporting one model name."""

    ARCHITECT_TCS = ("hypothesis_generation", "research_synthesis")
    MANAGER_TCS = ("extraction", "classification", "screening")

    def __init__(self, model: str = "stub-model", answer_conf: float = 0.9):
        self.model_name = model
        self.answer_conf = answer_conf

    async def complete(self, task_class, messages, schema=None, **_ig):
        if task_class in self.ARCHITECT_TCS:
            body = {"content": _decompose()}
        elif task_class in self.MANAGER_TCS:
            body = {"content": _answer(self.answer_conf)}
        else:
            body = {"parsed_json": {"objections": []}, "content": ""}
        body["model"] = self.model_name
        return body


def _pipeline(tmp_path, *, model, adversary_router):
    return ResearchPipeline(
        model=model, adversary_router=adversary_router,
        transport=fixture_transport(_routes()),
        store=ArtifactStore(root=tmp_path / "art"),
        ledger=ProvenanceLedger())


def _strong_pipeline(tmp_path, *, adversary_router):
    """Decomposition + confident answer + approving critic."""
    model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Manager": [{"content": _answer(0.9)}],
    })
    return _pipeline(tmp_path, model=model, adversary_router=adversary_router)


def _run(pipe):
    return asyncio.run(pipe.run(
        "What does recent scholarly research say about semiconductor "
        "supply chain resilience?", today=TODAY))


# ── 1. structural self-review: the claimed cap becomes real ──────────────

def test_structural_self_review_ceiling_enforced(tmp_path):
    pipe = _strong_pipeline(tmp_path, adversary_router=None)
    assert pipe._adversary_is_self_review
    result = _run(pipe)
    assert result.sealed, result.refusal_reason
    assert result.confidence_score <= SELF_REVIEW_CEILING, (
        f"self-review sealed at {result.confidence_score} — the ceiling the "
        f"notes claim was not applied")
    assert any("self-review" in n.lower() for n in result.notes)


# ── 2. default `ask` wiring: critic on the author's own router ───────────

def test_shared_router_critic_counts_as_self_review(tmp_path):
    router = _QuietRouter("one-model")
    # production `ask` shape: RouterModel wraps the SAME router object that
    # reviews the conclusion.
    pipe = ResearchPipeline(
        model=RouterModel(router), adversary_router=router,
        transport=fixture_transport(_routes()),
        store=ArtifactStore(root=tmp_path / "art"),
        ledger=ProvenanceLedger())
    result = _run(pipe)
    assert result.sealed, result.refusal_reason
    assert result.confidence_score <= SELF_REVIEW_CEILING, (
        f"same-router review sealed at {result.confidence_score} as if "
        f"independent")
    assert any("self-review" in n.lower() for n in result.notes)


# ── 3. distinct backend stays independent (non-regression pin) ───────────

def test_distinct_adversary_backend_not_capped(tmp_path):
    pipe = _strong_pipeline(
        tmp_path, adversary_router=_QuietRouter("other-model"))
    result = _run(pipe)
    assert result.sealed, result.refusal_reason
    assert result.confidence_score > SELF_REVIEW_CEILING


# ── 4-5. the panel, finally reachable from the engine ────────────────────

class _ObjectionRouter(_QuietRouter):
    def __init__(self, model, text, severity="MAJOR"):
        super().__init__(model)
        self.text, self.severity = text, severity

    async def complete(self, task_class, messages, schema=None, **_ig):
        return {"model": self.model_name,
                "parsed_json": {"objections": [
                    {"text": self.text, "kind": "refuting_evidence",
                     "severity": self.severity}]}}


def test_panel_pooled_verdict_recorded(tmp_path):
    panel = AdversaryPanel([
        __import__("agp.adversary", fromlist=["Adversary"]).Adversary(
            _QuietRouter("critic-a")),
        __import__("agp.adversary", fromlist=["Adversary"]).Adversary(
            _ObjectionRouter("critic-b", "the sample covers one vendor only")),
    ])
    pipe = _strong_pipeline(tmp_path, adversary_router=panel)
    result = _run(pipe)
    texts = [getattr(o, "text", "") for o in result.objections]
    assert any("one vendor" in t for t in texts), (
        "panel objection never reached the seal record")
    assert any("independent" in n.lower() or "review" in n.lower()
               for n in result.notes)


def test_panel_same_model_members_labelled_and_capped(tmp_path):
    adv_mod = __import__("agp.adversary", fromlist=["Adversary"])
    panel = AdversaryPanel([
        adv_mod.Adversary(_QuietRouter("same-model")),
        adv_mod.Adversary(_QuietRouter("same-model")),
    ])
    pipe = _strong_pipeline(tmp_path, adversary_router=panel)
    result = _run(pipe)
    assert result.sealed, result.refusal_reason
    assert result.confidence_score <= SELF_REVIEW_CEILING, (
        "a panel of one model reviewed itself into full confidence")
    assert any("self-review" in n.lower() for n in result.notes)


# ── 6. asymmetry property sweep on the pure clamp helpers ────────────────

def test_property_application_never_raises_score():
    from agp.adversary import AdversaryObjection
    from agp.ensemble import PanelVerdict, ReviewProvenance
    import random
    rng = random.Random(20260824)
    for _ in range(500):
        base = rng.random()
        n_obj = rng.randint(0, 4)
        objs = [AdversaryObjection(
            claim_id="c", text=f"objection {i}",
            kind=rng.choice(["refuting_evidence", "selection_effect"]),
            severity=rng.choice(["MINOR", "MAJOR", "BLOCKING"]),
            model=rng.choice(["m1", "m2", ""])) for i in range(n_obj)]
        verdict = PanelVerdict(
            objections=objs,
            provenance=ReviewProvenance(
                author_model=rng.choice(["m1", "author", ""]),
                reviewer_models=[rng.choice(["m1", "m2", "", "(unattributed)"])
                                 for _ in range(rng.randint(1, 3))]),
            ensemble_spread_ceiling=(
                rng.choice([None, 0.3, 0.54, 0.8])))
        clamped, _reason = verdict.apply(base)
        assert 0.0 <= clamped <= max(base, SELF_REVIEW_CEILING) + 1e-9
        if verdict.has_blocking:
            assert clamped <= base + 1e-9


# ── 7. the human door: `callisto ask --adversary-backend X` ──────────────

def test_ask_parser_accepts_repeated_adversary_backend():
    import callisto
    parser = callisto.build_parser()
    args = parser.parse_args([
        "ask", "q", "--backend", "ox_alpha",
        "--adversary-backend", "local", "--adversary-backend", "ox_alpha"])
    assert args.adversary_backend == ["local", "ox_alpha"]


def test_make_engine_builds_panel_from_extra_backends():
    import callisto

    class _R:
        endpoints = {"ox_alpha": object(), "local": object()}
        task_classes = {}
        default_tier_name = "ox_alpha"

    engine = callisto._make_engine(
        _R(), self_review=False,
        extra_adversaries=["local"])
    from agp.ensemble import AdversaryPanel as P
    assert isinstance(engine._adversary_router, P)
    solo = callisto._make_engine(_R(), self_review=False)
    assert not isinstance(solo._adversary_router, P)


def test_make_engine_refuses_unknown_adversary_backend(capsys):
    import callisto

    class _R:
        endpoints = {"ox_alpha": object()}
        task_classes = {}
        default_tier_name = "ox_alpha"

    rc = callisto._validate_adversary_backends(_R(), ["nope"])
    assert rc == 2
