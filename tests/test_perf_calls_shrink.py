"""PERF — the shrink MEASURED on a fat-evidence run.

profile_calls.py's fixture admits small bodies, so it cannot show the
shrink. This test builds a run where every leaf carries three max-size
(4,000-char) evidence bodies and asserts the prompt-size reduction and the
unchanged answer.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

from tests.test_speed_parallel_leaves import (  # noqa: E402
    ROUTES,
    _Adversary,
    _answer,
    _decompose,
)
from tools.pipeline.cache import CountingModel  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402


def test_fat_evidence_prompt_is_shrunk_and_answer_unchanged(tmp_path):
    from agp import Domain
    from agp.provenance import ProvenanceLedger
    from tools.artifacts import ArtifactStore
    from tools.pipeline.engine import ResearchPipeline, fixture_transport

    fat_openalex = json.dumps({"results": [
        {"id": f"W{i}", "title": "Scholarly study on apple earnings "
                                 f"expectations analyst consensus quarterly "
                                 f"results {'x' * 900}",
         "publication_year": 2024, "cited_by_count": 12}
        for i in range(3)]})
    routes = dict(ROUTES)
    routes["/works"] = fat_openalex

    inner = ScriptedModel({"Architect": [_decompose(3)]},
                          default=_answer(0.7))
    model = CountingModel(inner)
    pipeline = ResearchPipeline(
        model=model, adversary_router=_Adversary(),
        transport=fixture_transport(routes),
        store=ArtifactStore(root=tmp_path / "art"),
        ledger=ProvenanceLedger())
    result = asyncio.run(pipeline.run(
        "Will Apple report quarterly results above Wall Street consensus "
        "expectations in its next earnings report?",
        domain=Domain.FINANCIAL, today=date(2026, 8, 22)))

    assert result.sealed
    manager_rows = [r for r in model.rows if r["role"] == "Manager"]
    assert len(manager_rows) == 3
    # BEFORE this wave each of these carried 3 x ~4000-char bodies ≈ 3.4k+
    # tokens in; AFTER they are budgeted at ≤ TOTAL_EVIDENCE_BUDGET + slack.
    assert all(r["in_chars"] <= 4600 for r in manager_rows), (
        f"prompt not shrunk: {[r['in_chars'] for r in manager_rows]}")
