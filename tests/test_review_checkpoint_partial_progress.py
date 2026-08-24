"""REVIEW — crash mid-phase must preserve per-leaf checkpoint progress.

The serial engine wrapped EVERY leaf's fetch and answer in ckpt.run_stage
inside the per-leaf loop, so a run that died while working on leaf k had
already saved checkpoints for leaves 0..k-1 — "resume from the last good
step" is the W3 contract (MORNING_REPORT: 'Checkpointing — resume from
the last good step').

The parallel restructure (perf/standing-speed-0823-150903) moved the
answer-stage cp.save into the ordered-assembly loop, which only runs
after asyncio.gather completes with ZERO exceptions. When leaf 1's model
call raises, leaves 0 and 2 answered successfully — and their answer
checkpoints are silently dropped. A retry redoes paid model calls.
(Phase A has the same shape one level up: any retrieval error aborts
before ANY fetch checkpoint is saved.)

This test pins the serial contract. It FAILS on the parallel engine —
that is the deliverable. See findings/review_2026-08-23.md, finding R1.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp import Domain  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.checkpoint import FileCheckpointer  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402

OPENALEX_BODY = json.dumps({
    "results": [
        {"id": "W1", "title": "Scholarly study on apple earnings expectations:"
                              " analyst consensus and quarterly results",
         "publication_year": 2024, "cited_by_count": 12},
    ],
})
ROUTES = {"/works": OPENALEX_BODY}


def _decompose(n_leaves: int) -> str:
    subs = []
    for i in range(n_leaves):
        subs.append({
            "text": f"leaf {i}: what does the evidence say about apple "
                    "earnings expectations and analyst consensus",
            "kind": "descriptive",
            "question_type": "scholarly work search",
            "min_source_tier": 1,
            "min_independent_sources": 1,
            "quant_required": False,
            "horizon_days": None,
        })
    return json.dumps({"sub_questions": subs})


def _answer(conf=0.7) -> str:
    return json.dumps({"answer": "the evidence supports the claim",
                       "proposed_confidence": conf, "compute": None})


class _Adversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub-adversary"}


class BoomAtLeafAnswer:
    """Answers every leaf except one. Fails on LEAF TEXT in the prompt, not
    on _call_tag, so the injection fires identically under the serial
    engine (which never passes _call_tag) and the parallel one."""

    def __init__(self, fail_token: str):
        self.fail_token = fail_token

    async def complete(self, role, messages, **k):
        if role == "Manager" and self.fail_token in messages[-1]["content"]:
            raise ValueError("boom at leaf answer")
        if role == "Architect":
            return {"content": _decompose(3)}
        return {"content": _answer(0.7)}


def _pipeline(tmp_path, cp, model):
    return ResearchPipeline(
        model=model, adversary_router=_Adversary(),
        transport=fixture_transport(dict(ROUTES)),
        store=ArtifactStore(root=tmp_path / "art"),
        ledger=ProvenanceLedger(),
        checkpointer=cp)


def test_crash_during_answers_keeps_completed_answer_checkpoints(tmp_path):
    """Serial contract: when leaf 1's answer fails, the leaves that already
    ANSWERED SUCCESSFULLY have their answer_leaf stages checkpointed. The
    serial engine saved each answer inside the per-leaf loop (leaf 0's was
    on disk before leaf 1 ran). A parallel engine may legitimately save
    MORE — but never fewer than the serial contract."""
    cp = FileCheckpointer(root=tmp_path / "ckpt")
    pipeline = _pipeline(tmp_path, cp, BoomAtLeafAnswer("leaf 1"))
    with pytest.raises(ValueError, match="boom at leaf answer"):
        asyncio.run(pipeline.run("Q?", domain=Domain.FINANCIAL,
                                 today=date(2026, 8, 22)))
    saved_stages = sorted(ck.stage for ck in cp.list_all())
    assert "fetch_leaf" in saved_stages, (
        "even the FETCH checkpoints are gone — nothing survived the abort")
    n_answers = saved_stages.count("answer_leaf")
    assert n_answers >= 1, (
        f"completed leaves' answer stages were not checkpointed before "
        f"the run aborted (found {n_answers}); a resume redoes model calls "
        f"that already succeeded — the serial engine saved each leaf's "
        f"answer immediately after it completed")
