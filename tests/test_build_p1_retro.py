"""P1 — the retrodiction harness driven by the REAL pipeline.

Before this, tools/retrodiction/harness.py only ever ran against
StubResearcher. PipelineResearcher runs the whole P1 chain (decompose ->
source selection -> fixture fetch -> provenance -> synthesis -> adversary)
per question. No network: fixture transport; no live model: ScriptedModel.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

from tests.helpers.no_socket import NoSocket

NoSocket().install()


from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tools.pipeline.retro import PipelineResearcher  # noqa: E402
from tools.retrodiction.harness import RunConfig  # noqa: E402
from tools.retrodiction.questions import RetrodictionQuestion  # noqa: E402
from tools.retrodiction.scoring import score_brier  # noqa: E402


def _question(qid: str, answer: bool) -> RetrodictionQuestion:
    return RetrodictionQuestion(
        question_id=qid,
        text="what does the literature say about the topic",
        domain="GENERAL",
        claim_date=date(2024, 1, 1),
        resolution_date=date(2024, 6, 1),
        answer_binary=answer,
    )


def _model() -> ScriptedModel:
    decompose = json.dumps({"sub_questions": [
        {"text": "what does the literature say about the topic",
         "kind": "descriptive", "question_type": "scholarly work search",
         "min_source_tier": 2, "min_independent_sources": 2}]})
    answers = [
        {"content": json.dumps({"answer": "the evidence supports the claim",
                                "proposed_confidence": 0.7})},
        {"content": json.dumps({"answer": "the evidence supports the claim",
                                "proposed_confidence": 0.4})},
    ]
    m = ScriptedModel(default={"content": json.dumps(
        {"answer": "no evidence either way", "proposed_confidence": 0.3})})
    m.script("Architect", {"content": decompose})
    m.script("Manager", *answers)
    return m


ROUTES = {"/works": json.dumps({"results": [{"id": "W1"}]})}


def test_pipeline_researcher_scores_against_retrodiction_questions(tmp_path):
    researcher = PipelineResearcher(model=_model(), routes=ROUTES,
                                    store=ArtifactStore(root=tmp_path / "a"),
                                    claim_date=date(2024, 1, 1))
    questions = [_question("q1", True), _question("q2", False)]
    prompts = [q.prompt_for_researcher() for q in questions]
    predictions = asyncio.get_event_loop().run_until_complete(
        researcher.answer_async(prompts, [], loops=1))
    assert len(predictions) == 2
    # Brier computable end to end through the real pipeline path.
    brier = score_brier(predictions, questions)
    assert 0.0 <= brier <= 1.0
    # The full chain actually ran per question.
    assert len(researcher.results) == 2
    assert all(r.program is not None for r in researcher.results)
    assert any(r.fetches for r in researcher.results)


def test_harness_run_ab_with_real_pipeline(tmp_path):
    from tools.retrodiction.harness import run_ab

    def factory():
        return PipelineResearcher(
            model=_model(), routes=ROUTES,
            store=ArtifactStore(root=tmp_path / f"a{id(factory) % 99999}"),
            claim_date=date(2024, 1, 1))

    questions = [_question(f"q{i}", i % 2 == 0) for i in range(2)]
    results = run_ab([RunConfig(label="pipeline",
                                researcher_factory=factory)],
                     questions, [])
    r = results["pipeline"]
    assert r.n_scored == 2
    assert 0.0 <= r.brier <= 1.0
