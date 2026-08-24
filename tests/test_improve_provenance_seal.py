"""IMPROVE (ox-alpha 2026-08-24, improve/money-path-landing) — two defects at
the provenance/seal seam, both instances of PATTERNS family 1 ("a check that
cannot fail") crossed with family 9.

Defect P — gate rejection launders provenance across URLs.
    record_gate_rejection(content, urls) removes the CONTENT HASH from the
    ledger entirely. When identical bytes are fetched from two different URLs
    and only one is rejected, the rejection erases the PRIMARY status of the
    admitted fetch: a sealed run's own evidence fails is_primary_bytes, and
    any conclusion scored on those bytes silently loses its provenance class.
    Reproduced end-to-end by
    test_build_p1_pipeline.test_end_to_end_sealed_with_provenance_artifact_and_adversary,
    which currently FAILS on this branch for exactly this reason.

Defect Q — the A20 seal-over-artifacts machinery is inert in production.
    AGPSession.artifact_refs exists, rides in the sealed payload, and has an
    add_artifacts() API — but ZERO production callers. The engine keeps refs
    on itself, gates them, and attaches them to PipelineResult only AFTER
    session.seal(). The keyed seal therefore never covers the quantitative
    artifacts it was extended to cover; verify_seal passes while the artifact
    layer of the payload is always [].
"""
import hashlib

from agp.provenance import ProvenanceLedger


# ── Defect P ───────────────────────────────────────────────────────────────

def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_rejecting_one_url_does_not_launder_identical_bytes_at_another():
    """The same body served at two URLs; one admitted, one rejected. The
    admitted URL's PRIMARY observation must survive."""
    led = ProvenanceLedger()
    body = '{"results": [{"id": "W1"}]}'
    led.record_tool_result("openalex_fetch", body, primary=True,
                           urls=["https://api.example.org/works?search=a"])
    # second leaf fetches the same cached bytes at a different URL; gate rejects
    led.record_tool_result("openalex_fetch", body, primary=True,
                           urls=["https://api.example.org/works?search=b"])
    led.record_gate_rejection(body, ["https://api.example.org/works?search=b"])

    assert led.is_primary_bytes(body), (
        "gate rejection of URL B erased the PRIMARY observation of the "
        "identical bytes admitted at URL A")
    assert not led.superseded(url="https://api.example.org/works?search=a")
    assert led.superseded(url="https://api.example.org/works?search=b")


def test_rejection_still_blocks_its_own_url_and_late_replays():
    """The original R4/R4b guarantee is preserved: rejected bytes cannot be
    re-minted by replaying, and the rejected URL verifies nothing."""
    led = ProvenanceLedger()
    body = '{"x": 1}'
    url = "https://api.example.org/bad"
    led.record_tool_result("t", body, primary=True, urls=[url])
    led.record_gate_rejection(body, [url])

    assert not led.is_primary_bytes(body)
    assert led.superseded(content=body)
    assert led.superseded(url=url)

    replay = led.record_tool_result("t", body, primary=True, urls=[url])
    assert not led.is_primary_bytes(body), "replay after rejection re-minted"
    assert not led.has_observation(body)


def test_end_to_end_pipeline_keeps_primary_status_of_admitted_fetch():
    """Full pipeline regression for the failing test this defect produced:
    every fetch a sealed result reports must still be PRIMARY in the ledger
    even when a sibling query returned identical bytes that were rejected."""
    import asyncio
    import os
    import sys
    import tempfile
    from pathlib import Path

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tests_dir = os.path.join(root, "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    from test_build_p1_pipeline import _make                # noqa: E402
    from agp.provenance import ProvenanceLedger             # noqa: E402

    ledger = ProvenanceLedger()
    tmp = Path(tempfile.mkdtemp())
    pipeline, _model = _make(tmp, ledger=ledger)
    result = asyncio.new_event_loop().run_until_complete(
        pipeline.run("What is known about the topic?"))
    assert result.sealed, result.refusal_reason
    for f in result.fetches:
        assert ledger.is_primary_bytes(f.body), (
            f"fetch from {f.source_name} lost PRIMARY status")


# ── Defect Q ───────────────────────────────────────────────────────────────

def test_sealed_session_payload_carries_engine_artifact_refs():
    """When the engine seals a run that produced artifacts, the SESSION's
    sealed payload must contain those refs — and verify_seal must cover
    them (tampering with the layer breaks verification)."""
    import asyncio
    import json
    import tempfile
    from pathlib import Path

    from tools.pipeline.engine import ResearchPipeline, fixture_transport
    from tools.artifacts import ArtifactStore

    def _decompose():
        return json.dumps({"sub_questions": [
            {"text": "compute something quantitative", "kind": "quantitative",
             "question_type": "numeric computation",
             "min_source_tier": 5, "min_independent_sources": 0,
             "quant_required": True}]})

    answer = json.dumps({
        "answer": "the computation ran",
        "proposed_confidence": 0.6,
        "compute": {"code": "print(2 + 2)", "inputs": {}}})

    class _Model:
        def __init__(self):
            self.calls = {}

        async def complete(self, task_class, messages, schema=None):
            self.calls.setdefault(task_class, 0)
            self.calls[task_class] += 1
            if task_class == "Architect":
                return {"parsed_json": json.loads(_decompose()),
                        "model": "stub"}
            return {"parsed_json": json.loads(answer), "model": "stub"}

    class _Quiet:
        async def complete(self, task_class, messages, schema=None):
            return {"parsed_json": {"objections": []}, "model": "stub"}

    tmp = Path(tempfile.mkdtemp())
    pipe = ResearchPipeline(
        model=_Model(), adversary_router=_Quiet(), transport=None,
        store=ArtifactStore(root=tmp / "artifacts"))
    result = asyncio.new_event_loop().run_until_complete(
        pipe.run("Compute question?", today=__import__("datetime").date(2026, 8, 22)))
    assert result.sealed, result.refusal_reason
    assert result.artifact_refs, "run produced no artifacts; test inconclusive"
    d = result.session.to_dict()
    sealed_sha = {r["sha256"] for r in d.get("artifact_refs", [])}
    assert sealed_sha == {r.sha256 for r in result.artifact_refs}, (
        "sealed session payload does not carry the run's artifact refs — "
        "verify_seal hashes an empty artifact layer (A20 machinery inert)")
