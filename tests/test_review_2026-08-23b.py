"""STANDING REVIEW — run 6 (2026-08-23, reviewer ox-alpha).

Hunted families: #1 (verification layer that cannot fail), #2 (fix lands in
one copy), #6 (rounding direction of error), and re-checked the newest fixes
(S5 vacuous claims, D2 stage rename, D3 split-world, estimate wiring).

Every test here FAILS against current master BY DESIGN. No production code
was edited.
"""
from __future__ import annotations

import asyncio
import json
import sys
import hashlib
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


# ── fixtures ────────────────────────────────────────────────────────────────

QUESTION = ("what does the literature say about semiconductor supply chain "
            "resilience")
LEAF_SPEC = [{
    "text": QUESTION,
    "kind": "descriptive",
    "question_type": "scholarly work search",
    "min_source_tier": 1,
    "min_independent_sources": 1,
    "quant_required": False,
}]
RELEVANT_BODY = json.dumps({"results": [{
    "title": "semiconductor supply chain resilience literature review",
    "abstract": ("a study of semiconductor supply chain resilience and the "
                 "scholarly literature")}]})

from tools.pipeline import checkpoint as ckpt  # noqa: E402
from tools.pipeline import engine as eng       # noqa: E402


class _StubModel:
    """Decomposes into one leaf, then proposes *conf* with no hedging."""

    def __init__(self, conf: float):
        self.conf = conf

    async def complete(self, task_class, messages, schema=None):
        if any("sub_questions" in str(m) for m in messages):
            return {"parsed_json": {"sub_questions": LEAF_SPEC},
                    "model": "stub"}
        return {"parsed_json": {"answer": "foundry concentration is key",
                                "stance": "AFFIRMS",
                                "proposed_confidence": self.conf},
                "model": "stub"}


class _QuietAdversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _pipeline(conf: float):
    return eng.ResearchPipeline(
        model=_StubModel(conf), adversary_router=_QuietAdversary(),
        transport=eng.fixture_transport({"openalex": RELEVANT_BODY}))


def _run(conf: float):
    return asyncio.run(_pipeline(conf).run(QUESTION))


# ── RV1 (HIGH): engine.py rounds the SEALED leaf confidence with round(),
# which can RAISE it — the exact defect family #6 documents, preserved
# verbatim by the estimate-wiring refactor ("historical equivalence" pinned
# the bug too). round(0.836, 2) == 0.84 is an automated actor raising a
# confidence score. Reproduced END TO END: one fetch, model proposes 0.836,
# the stored leaf outcome reads 0.84.

def test_leaf_confidence_is_never_raised_by_rounding():
    out = _run(0.836)
    leaf = max(out.leaves, key=lambda l: l.confidence)
    assert leaf.confidence <= 0.836 + 1e-9, (
        f"sealed leaf confidence {leaf.confidence} exceeds the model's "
        f"proposed 0.836 — round() raised an automated score (family 6)")


# ── RV2 (HIGH): "PRIMARY" provenance is minted from ANY HTTP-200 body.
# RestSource._record records primary=True unconditionally, so one topical
# fetch — relevant by word overlap, substantively worthless — assigns
# SourceClass.PRIMARY, whose ceiling is 1.0. The evidence-requirement gate
# then passes on min_source_tier=1 (PRIMARY required, PRIMARY granted by the
# same unconditional flag) and the leaf stores the model's OWN number
# (0.95 -> VERIFIED tier) with zero downward mechanism between proposal and
# storage. The architecture's premise is that confidence is assigned by the
# code path that fetched bytes; here the fetched-bytes label does no work at
# all beyond "we got a 200".

def test_single_fetch_cannot_store_model_confidence_verbatim():
    out = _run(0.95)
    leaf = max(out.leaves, key=lambda l: l.confidence)
    assert leaf.tier != "VERIFIED", (
        f"a single HTTP-200 fetch let the model's self-reported "
        f"{leaf.confidence} store as {leaf.tier} — PRIMARY was minted from "
        f"the status code alone")


def test_primary_assignment_requires_more_than_a_200():
    """The class assignment must be falsifiable: feed the ledger bytes that
    were NEVER fetched and PRIMARY must not be reachable. It currently is,
    because any record_tool_result(primary=True) call mints it."""
    from agp.provenance import ProvenanceLedger
    from agp import Evidence
    ledger = ProvenanceLedger()
    ledger.record_tool_result("any_tool", "fabricated bytes", primary=True)
    ev = Evidence(content="fabricated bytes", source_class="INFERRED",
                  confidence_score=0.3, origin_agent="test")
    assert ledger.assign_source_class(ev).value != "PRIMARY", (
        "unfetched bytes became PRIMARY because the flag is set by the "
        "caller, not derived from the fetch record")


# ── RV3 (MEDIUM): D3's shared predicate protects the ENGINE's two call
# sites, but replay_ledger() itself still authenticates nothing — it is a
# public function whose entire job is minting ledger entries, and under a
# KEYED regime it happily replays a checkpoint whose HMAC fails. The D3
# finding file says "A signature that fails is not replayed AT ALL"; that is
# true of engine.py's path and false of the module's own API. Same shape as
# every family-1 instance: the check exists one frame up and guards nothing
# one frame down.

def test_replay_ledger_refuses_bad_signature_when_keyed():
    key = "unit-test-key"
    ck = ckpt.Checkpoint(
        key="rk:fetch_leaf:abc", run="rk", stage="fetch_leaf",
        input_hash="ih", produced_at="2026-08-23T12:00:00",
        payload={"fetches": [{
            "body": "hello", "url": "u",
            "content_sha256": hashlib.sha256(b"hello").hexdigest()}]})
    signed = ck.signed(key)
    tampered = ckpt.Checkpoint(**{
        **signed.__dict__,
        "payload": {"fetches": [{
            "body": "EVIL", "url": "u",
            "content_sha256": hashlib.sha256(b"EVIL").hexdigest()}]}})
    assert not tampered.verify_signature(key)

    class L:
        stored: list = []

        def record_tool_result(self, *a, **k):
            L.stored.append((a, k))

        def has_observation(self, b):
            return any(a[0] == b for a, _ in L.stored)

    report = ckpt.replay_ledger(L(), [tampered])
    assert report["integrity_failures"], (
        "replay_ledger minted attacker bytes PRIMARY under a keyed regime "
        "because signature checking lives only in partition_admissibility")
