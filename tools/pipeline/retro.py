"""Bridge: the REAL pipeline as a retrodiction-harness Researcher.

Before this module, tools/retrodiction/harness.py could only run against
StubResearcher — the harness was built but nothing connected it to actual
research machinery. PipelineResearcher implements the harness's Researcher
interface by running ResearchPipeline end-to-end per question:

  - decompose (via the injected model)
  - fetch evidence with publication proofs (fixture transport; no network)
  - synthesize + propose confidence
  - adversary attack applied

The probability returned is the sealed conclusion's confidence mapped onto
the question's binary: P(True) = 0.5 + (conf/2) when the synthesized answer
leans yes, 0.5 − (conf/2) when it leans no. Confidence never exceeds what
provenance allowed — that is the whole point of running the real path.
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Optional

from tools.pipeline.engine import ResearchPipeline, fixture_transport
from tools.retrodiction.cutoff import EvidenceRecord as RetroEvidenceRecord
from tools.retrodiction.harness import Researcher
from tools.retrodiction.scoring import Prediction


class _AdversaryRouterStub:
    """Offline stand-in for the adversary backend. The real deployment passes
    the ProviderRouter; tests pass this. It attacks honestly given only what
    the pipeline shows it — here it raises no objections unless scripted."""

    def __init__(self, objections: Optional[list[dict]] = None):
        self.objections = list(objections or [])

    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": self.objections},
                "model": "adversary-stub"}


class PipelineResearcher(Researcher):
    """Runs the full P1 pipeline for each retrodiction question."""

    name = "pipeline"

    def __init__(self, *, model, routes: dict[str, str],
                 adversary_router=None, store=None,
                 descendant_resolutions: Optional[list] = None,
                 claim_date: Optional[date] = None):
        self.model = model
        self.routes = dict(routes)
        self.adversary_router = adversary_router or _AdversaryRouterStub()
        self.store = store
        self.descendant_resolutions = list(descendant_resolutions or [])
        self.claim_date = claim_date
        self.results: list = []

    def answer(self, prompts: list[dict],
               evidence: list[RetroEvidenceRecord],
               loops: int = 1) -> list[Prediction]:
        return asyncio.get_event_loop().run_until_complete(
            self.answer_async(prompts, evidence, loops))

    async def answer_async(self, prompts, evidence, loops=1) -> list[Prediction]:
        # Cutoff enforcement already happened in the harness (CutoffEnforcer);
        # records arriving here are proven-admitted. We surface their URLs as
        # fixture routes so the source layer can serve exactly those bytes.
        out: list[Prediction] = []
        for p in prompts:
            pipeline = ResearchPipeline(
                model=self.model,
                adversary_router=self.adversary_router,
                transport=fixture_transport(self.routes),
                store=self.store,
                descendant_resolutions=self.descendant_resolutions,
            )
            result = await pipeline.run(p["text"],
                                        today=self.claim_date)
            self.results.append(result)
            conf = result.confidence_score if result.sealed else 0.0
            leans_yes = self._leans_yes(result.conclusion)
            prob = (0.5 + conf / 2.0) if leans_yes else (0.5 - conf / 2.0)
            out.append(Prediction(
                question_id=p["question_id"], probability=max(0.0, min(1.0, prob)),
                config_label=f"{self.name}@{loops}", loops=loops))
        return out

    @staticmethod
    def _leans_yes(conclusion: str) -> bool:
        text = conclusion.lower()
        negations = ("no evidence", "does not", "not supported", "unlikely",
                     "falsified", "refused")
        if any(n in text for n in negations):
            return False
        return True
