"""Measure retrieval-round marginal value on golden runs.

For every golden run (pipeline fixtures + retrodiction question set), run the
REAL IterativeRetriever and record, after EACH round:

  - the cumulative admitted-fetch fingerprint (source names + content hashes)
  - the independent-key set
  - the derived conclusion inputs: best source class, n_independent

A round is CONCLUSION-MOVING iff the derived (tier-determining class,
n_independent) changed vs the previous round; otherwise it is PURE COST —
no model call downstream could return anything different, because the
evidence set it would see is identical up to irrelevant duplicates of an
already-seen source.

This is MEASUREMENT ONLY: nothing here changes what the retriever fetches,
admits, or returns. The stop rule (if the data supports one) comes later.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers.no_socket import NoSocket  # noqa: E402

NoSocket().install()

from agp import Domain  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402


CLASS_RANK = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}


def instrumented_run(question_text: str, routes: dict[str, str],
                     decompose: str, answers: list[str],
                     today: date):
    """Run the real pipeline with a per-round spy inside the retriever.

    Returns (PipelineResult, list-of-round-records).
    """
    model = ScriptedModel(default={"content": answers[-1] if answers else "{}"})
    model.script("Architect", {"content": decompose})
    for a in answers:
        model.script("Manager", {"content": a})

    ledger = ProvenanceLedger()
    store = ArtifactStore(root=tempfile.mkdtemp(prefix="rounds_meas_"))
    pipeline = ResearchPipeline(
        model=model, adversary_router=_Quiet(),
        transport=fixture_transport(routes), store=store, ledger=ledger)

    rounds_seen: list[dict] = []
    orig_retrieve = None
    from tools.pipeline import retrieval as R

    _orig = R.IterativeRetriever.retrieve

    def spying_retrieve(self, question, question_type, min_independent):
        trace = _orig(self, question, question_type, min_independent)
        # Derive per-round conclusion state from the trace's round log.
        # The engine's leaf confidence depends on exactly:
        #   best_class(admitted), len(independent_keys), sandbox-ok.
        # Reconstruct cumulatively. round_detail records admitted sources in
        # order; we map source -> class via the ledger assignments recorded
        # during the real run by re-running assign_source_class cheaply.
        rounds_seen.append({
            "qid": question.question_id,
            "rounds": json.loads(json.dumps(trace.rounds)),
            "independent_keys": sorted(trace.independent_keys),
            "n_admitted": len(trace.admitted),
            "stop_reason": trace.stop_reason,
        })
        return trace

    R.IterativeRetriever.retrieve = spying_retrieve
    try:
        result = asyncio.get_event_loop().run_until_complete(
            pipeline.run(question_text, domain=Domain.GENERAL, today=today))
    finally:
        R.IterativeRetriever.retrieve = _orig
    return result, rounds_seen


class _Quiet:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


if __name__ == "__main__":
    print("module ok")
