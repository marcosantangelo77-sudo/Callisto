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
    sealed payload must contain those refs — not just the PipelineResult."""
    import inspect

    from tools.pipeline import engine

    src = inspect.getsource(engine.ResearchPipeline._run_inner)
    # The engine must attach pending refs to the session BEFORE sealing.
    attach_pos = min(
        src.find("session.add_artifacts"),
        src.find("session.artifact_refs =") if
        "session.artifact_refs =" in src else len(src))
    seal_pos = src.find("session.seal()")
    assert attach_pos != -1 and attach_pos < seal_pos, (
        "engine seals the session without attaching artifact refs — the A20 "
        "seal-over-artifacts layer is present but never populated in "
        "production")
