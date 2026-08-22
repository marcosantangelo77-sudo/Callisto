import asyncio
import json
from datetime import date

from tests.test_build_w1_retrieval import IRRELEVANT_BODY
from tools.pipeline.model import ScriptedModel
from tools.pipeline.engine import ResearchPipeline, fixture_transport
from tools.artifacts import ArtifactStore


def test_debug_engine(tmp_path):
    decompose = json.dumps({"sub_questions": [{
        "text": "what does scholarly research say about semiconductor "
                "supply chain resilience",
        "kind": "descriptive",
        "question_type": "scholarly literature",
        "min_source_tier": 2, "min_independent_sources": 1}]})
    model = ScriptedModel({
        "Architect": [{"content": decompose}],
        "Manager": [{"content": json.dumps(
            {"answer": "no relevant evidence found",
             "proposed_confidence": 0.8})}],
    })

    class _Quiet:
        async def complete(self, task_class, messages, schema=None):
            return {"parsed_json": {"objections": []}, "model": "stub"}

    pipe = ResearchPipeline(
        model=model, adversary_router=_Quiet(),
        transport=fixture_transport({"/a?": IRRELEVANT_BODY}),
        store=ArtifactStore(root=tmp_path / "art"))
    result = asyncio.run(pipe.run(
        "What does research say about semiconductor supply chain "
        "resilience?", today=date(2026, 8, 22)))
    print("NOTES:", result.notes)
    print("FETCHES:", result.fetches)
    print("SEALED:", result.sealed, result.refusal_reason)
    assert True
