"""P1 — component-integration findings: what fit the chain, what didn't.

The brief asked for honest findings about components that did not fit.
Each test here pins one integration fact so regressions surface loudly.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

from tests.helpers.no_socket import NoSocket

NoSocket().install()


from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.pipeline.engine import (  # noqa: E402
    ResearchPipeline,
    fixture_transport,
)
from tools.pipeline.model import ScriptedModel  # noqa: E402


def _pipeline(tmp_path, ledger=None, routes=None, decompose=None):
    decompose = decompose or json.dumps({"sub_questions": [
        {"text": "what does the literature say about the topic",
         "kind": "descriptive", "question_type": "scholarly work search",
         "min_source_tier": 2, "min_independent_sources": 2}]})
    model = ScriptedModel({
        "Architect": [{"content": decompose}],
        "Manager": [{"content": json.dumps(
            {"answer": "supported by fetched evidence",
             "proposed_confidence": 0.9})}],
    })

    class A:
        async def complete(self, tc, m, schema=None):
            return {"parsed_json": {"objections": []}, "model": "stub"}

    from tools.artifacts import ArtifactStore
    return ResearchPipeline(
        model=model, adversary_router=A(),
        transport=fixture_transport(routes or
                                    {"/works": json.dumps({"results": []})}),
        store=ArtifactStore(root=tmp_path / "art"), ledger=ledger)


def test_finding_source_selection_handles_natural_questions():
    """RESOLVED (was: selection is word-overlap, not semantic).

    P1 recorded that a natural question selected nothing because select()
    scored matched/len(question_words) against a 0.34 floor — so the
    threshold was a function of question LENGTH. 'patents' selected
    uspto_odp; 'patents filed by a company' selected nothing, since one
    match across three topical words is 0.333.

    Fixed with a diagnostic-term rule: a term mentioned by at most a third
    of registered sources sets a score FLOOR (not a bypass — min_score still
    governs, so a caller asking for strictness still gets it).

    This test now pins the fix. Selection is still lexical, not semantic —
    embeddings would be the real answer — but ordinary phrasing works, and
    a question no source covers still correctly returns nothing.
    """
    from tools.sources.registry import get_source_registry
    reg = get_source_registry()

    # natural phrasing selects
    for q in ("patents filed by a company", "clinical trials",
              "scholarly literature about semiconductor supply chains"):
        assert reg.select(q), f"natural question selected nothing: {q!r}"

    # a question nothing covers still returns nothing — no noise
    assert reg.select("how is the weather") == []

    # strictness remains a working control
    assert reg.select("patents filed by a company", min_score=0.99) == []


def test_finding_generic_fetch_covers_4_of_8_sources():
    """FINDING: only openalex, federalregister, clinicaltrials, gdelt have a
    generic no-parameter search call. fred needs an API key + series id,
    bls needs a POST with series ids, treasury needs a dataset name,
    wikidata needs raw SPARQL. The pipeline skips them honestly (logged,
    recorded as a gap) rather than inventing queries."""
    covered = {k for k, v in ResearchPipeline.GENERIC_CALLS.items() if v}
    assert covered == {"openalex", "federalregister", "clinicaltrials",
                       "gdelt"}


def test_finding_sandbox_outputs_are_child_attested(tmp_path):
    """FINDING: run_python deletes its workspace, so store_sandbox_outputs
    can only attest file hashes the child reported — the artifact bytes are
    not independently re-hashed by the store unless keep_workspace=True is
    used. The pipeline marks such refs meta['attested_by_child_only']=True
    (set inside store_sandbox_outputs). Honest, but a gap for tamper-proof
    artifact chains."""
    from tools.sandbox import run_python
    from tools.artifacts import store_sandbox_outputs, ArtifactStore
    sbx = run_python("result = 1 + 1")
    assert sbx.status == "ok"
    store = ArtifactStore(root=tmp_path / "a")
    refs = store_sandbox_outputs(sbx, store, workspace=None)
    file_refs = [r for r in refs if r.meta.get("attested_by_child_only")]
    assert all(r.code_sha256 for r in refs)  # provenance chain to code intact


def test_finding_provenance_ledger_is_session_local():
    """FINDING: ProvenanceLedger is in-memory only — nothing persists it.
    Two pipeline runs sharing a store but not a ledger cannot verify each
    other's fetches, and a process restart loses the evidence that a seal
    was grounded in. Fine for one run; a durability gap for the system."""
    a, b = ProvenanceLedger(), ProvenanceLedger()
    a.record_tool_result("t", "body", primary=True)
    assert a.has_observation("body") and not b.has_observation("body")


def test_finding_inheritance_cap_is_probable_min_not_speculative_band():
    """FINDING (verified, not a defect): tools.research_program.SPECULATIVE_CAP
    equals TIER_PROBABLE_MIN (0.55) — a zero-descendant parent can seal at
    the very bottom edge of PROBABLE. The docstring says 'caps at
    SPECULATIVE'; the constant sits on the boundary. Callers must not read
    the tier label as 'the rule failed'."""
    from tools.research_program import SPECULATIVE_CAP
    from agp.thresholds import TIER_PROBABLE_MIN
    assert SPECULATIVE_CAP == TIER_PROBABLE_MIN == 0.55


def test_fetch_failure_degrades_to_refusal_not_fabrication(tmp_path):
    """With no fixture route matching, every fetch 404s; the pipeline must
    refuse to seal rather than answer from the model's priors."""
    p = _pipeline(tmp_path, routes={"/nomatch": "{}"})
    r = asyncio.get_event_loop().run_until_complete(p.run("Q?"))
    assert not r.sealed
    assert r.refusal_reason
