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

    def __init__(self, *, model, routes: Optional[dict[str, str]] = None,
                 adversary_router=None, store=None,
                 descendant_resolutions: Optional[list] = None,
                 claim_date: Optional[date] = None):
        self.model = model
        self.routes = dict(routes) if routes is not None else None
        self.adversary_router = adversary_router or _AdversaryRouterStub()
        self.store = store
        self.descendant_resolutions = list(descendant_resolutions or [])
        self.claim_date = claim_date
        self.results: list = []

    def answer(self, prompts: list[dict],
               evidence: list[RetroEvidenceRecord],
               loops: int = 1) -> list[Prediction]:
<<<<<<< HEAD
        # The batch runner executes sync researchers on a worker thread, where
        # no current event loop exists (get_event_loop() would raise). Create
        # and close our own — this method owns its loop lifetime completely.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self.answer_async(prompts, evidence, loops))
        finally:
            loop.close()
=======
        # Batch callers (tools/retrodiction/batch.py) invoke this from inside
        # a running loop; standalone harness callers do not. Support both.
        try:
            asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(1) as ex:
                return ex.submit(
                    lambda: asyncio.run(
                        self.answer_async(prompts, evidence, loops))
                ).result()
        except RuntimeError:
            return asyncio.run(self.answer_async(prompts, evidence, loops))
>>>>>>> origin/build/dd-decomposition-diversity

    async def answer_async(self, prompts, evidence, loops=1) -> list[Prediction]:
        # Cutoff enforcement already happened in the harness (CutoffEnforcer);
        # records arriving here are proven-admitted.
        # routes=dict  -> fixture_transport serves exactly those bytes
        #                 (tests; no socket)
        # routes=None  -> real HTTP transport through the source registry
        #                 (live batch runs)
        transport = (fixture_transport(self.routes)
                     if self.routes is not None else None)
        out: list[Prediction] = []
        for p in prompts:
            pipeline = ResearchPipeline(
                model=self.model,
                adversary_router=self.adversary_router,
                transport=transport,
                store=self.store,
                descendant_resolutions=self.descendant_resolutions,
            )
            result = await pipeline.run(p["text"],
                                        today=self.claim_date)
            self.results.append(result)
            conf = result.confidence_score if result.sealed else 0.0
            # DECLARED stance, not a keyword scan. _leans_yes searched the
            # conclusion for six English phrases and defaulted to YES, so
            # "The merger completed on schedule ... no evidence of regulatory
            # objection" scored NO, and "The trial missed its primary
            # endpoint" scored YES. The sign of every forecast was set by
            # incidental wording, which is the shape of the only live batch we
            # have: predicted 0.33 against a realised 0.60.
            stance = getattr(result, "stance", "UNDETERMINED")
            if stance == "AFFIRMS":
                prob = 0.5 + conf / 2.0
            elif stance == "DENIES":
                prob = 0.5 - conf / 2.0
            else:
                # UNDETERMINED means "the evidence does not settle it", which
                # is p=0.5 — no lean in either direction. A scorer that cannot
                # tell must say so, not guess.
                prob = 0.5
            out.append(Prediction(
                question_id=p["question_id"], probability=max(0.0, min(1.0, prob)),
                config_label=f"{self.name}@{loops}", loops=loops))
        return out

