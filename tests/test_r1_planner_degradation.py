"""R1 follow-up (findings/battery_rerun.md): a planner exception must
degrade to an honest gap for THAT source only — never abort the whole
question. Exercises the planner-mode routing loop in retrieval.retrieve().
"""
from __future__ import annotations

import json

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from unittest.mock import patch  # noqa: E402

from agp.research_program import (  # noqa: E402
    EvidenceRequirement, QuestionKind, ResearchQuestion, SourceClassRank)
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.pipeline.engine import fixture_transport  # noqa: E402
from tools.pipeline.retrieval import (  # noqa: E402
    IterativeRetriever, RelevanceGate)
from tools.sources.registry import SourceRegistry, SourceAdapter  # noqa: E402
from tools.sources.base import SourceSpec  # noqa: E402


def _q(text="which companies and people were involved?"):
    rq = ResearchQuestion(text=text, kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=1)
    return rq


_BODY = json.dumps({"results": [
    {"id": "W1", "title": "companies and people registry", "year": 2024}]})


def _registry() -> SourceRegistry:
    reg = SourceRegistry()

    def make_adapter(source):
        class _Ad:
            def __getattr__(self, method_name):
                def call(*args, **kwargs):
                    return json.loads(_BODY)
                return call
        return _Ad()

    spec = SourceSpec(name="openalex", base_url="https://oa.example",
                      description="", answers=("companies",),
                      cannot_answer=("x",), tier=1, min_interval_s=0.0)
    reg.register(SourceAdapter(spec=spec, make_adapter=make_adapter))
    return reg


def test_planner_exception_degrades_to_gap_not_abort():
    """build_plan raising must skip only that source; the question run
    completes with the remaining sources' evidence."""
    from tools.sources import query_builder as qb
    retriever = IterativeRetriever(
        registry=_registry(), ledger=ProvenanceLedger(),
        transport=fixture_transport({"/": _BODY}),
        gate=RelevanceGate(min_coverage=0.25))
    with patch.object(qb, "build_plan",
                      side_effect=TypeError("bad operand type for unary -")):
        trace = retriever.retrieve(_q(), "", min_independent=1)
    skipped = {s["name"]: s["reason"] for s in trace.skipped_sources}
    assert "planner error:" in "".join(skipped.values())
