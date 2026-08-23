"""Standing review, run 3 — 2026-08-23 (branch review/rotating-0823-155500).

Findings in findings/review_2026-08-23c.md. Convention: OPEN defects are
xfail(strict=True) so the suite stays green while the repro provably fails
for the stated reason; controls prove each xfail fails for the RIGHT reason.
Regression/characterisation pins are plain tests. No production code edited.
"""
import inspect

import pytest

from agp import Domain, Evidence, SourceClass
from agp.adversary import Adversary
from agp.claims import AttachedEvidence, recompute_confidence
from agp.ensemble import PanelVerdict
from agp.thresholds import floor_conf
from tools.pipeline.checkpoint import Checkpoint, RunTrace, StageOutcome, seal_guard
from tools.pipeline.engine import _trace_from_payload
from tools.pipeline.retrieval import RelevanceGate
from tools.pipeline.synthesis import (
    EvidenceItem,
    confidence_from_agreement,
    triangulate,
)


# ── P1: synthesis mixed provenance still reaches VERIFIED ──────────────────

def _group_items():
    return [
        EvidenceItem(claim="unemployment is 4 percent", source_name="bls",
                     base_url="https://bls.gov", source_class="PRIMARY"),
        EvidenceItem(claim="unemployment is 4 percent", source_name="gossip_blog",
                     base_url="https://rumors.example", source_class="INFERRED"),
        EvidenceItem(claim="unemployment is 4 percent", source_name="mirror_site",
                     base_url="https://other.example", source_class="INFERRED"),
    ]


class TestP1SynthesisMixedProvenanceStillVerifies:
    @pytest.mark.xfail(strict=True,
                       reason="P1/F5: group rides strongest member's ceiling")
    def test_mixed_group_never_exceeds_secondaries_ceiling(self):
        group = triangulate(_group_items())[0]
        score, _ = confidence_from_agreement(group)
        assert score <= 0.75  # majority INFERRED gossip must not score VERIFIED

    def test_control_single_class_group_is_capped(self):
        items = [EvidenceItem(claim="x", source_name=f"s{i}",
                              base_url=f"https://s{i}.example",
                              source_class="INFERRED") for i in range(3)]
        score, _ = confidence_from_agreement(triangulate(items)[0])
        assert score <= 0.55

    def test_control_demonstrates_the_escape_live(self):
        # documents the defect precisely: 1.0 from one PRIMARY voice
        score, reasons = confidence_from_agreement(triangulate(_group_items())[0])
        assert score == 1.0


# ── P2: self-review cap unreachable on the engine path; empty panel approves ─

class TestP2SelfReviewCapUnreachableInEngine:
    @pytest.mark.xfail(strict=True,
                       reason="P2/F6a: engine never builds ReviewProvenance")
    def test_engine_adversary_call_threads_author_model(self):
        src = inspect.getsource(Adversary.attack)
        assert "author_model" in src
        eng = inspect.getsource(
            __import__("tools.pipeline.engine", fromlist=["x"]))
        call = [l for l in eng.splitlines() if ".adversary.attack(" in l]
        assert any("author_model" in l for l in call), (
            "engine's adversary.attack() passes no author_model")

    def test_control_empty_panel_with_backend_failures_approves(self):
        # F6c unchanged: all-critics-failed verdict clamps nothing
        v = PanelVerdict(objections=[], backend_failures=3)
        assert v.apply(0.99) == (0.99, "")


# ── R5 (re-recorded): claims clamp rounds UP across tier boundaries ────────

def _ev(cls):
    e = Evidence(content="x" * 200, source_class=cls,
                 confidence_score=0.9, domain=Domain.GENERAL,
                 origin_agent="review")
    return AttachedEvidence(evidence=e, assigned_class=cls)


class TestR5ClaimsClampRoundsUp:
    @pytest.mark.xfail(strict=True, reason="R5: round() can raise to ceiling")
    @pytest.mark.parametrize("cls,claimed,cap", [
        (SourceClass.SECONDARY, 0.7499, 0.75),
        (SourceClass.INFERRED, 0.5451, 0.55),
    ])
    def test_clamped_confidence_never_exceeds_claimed(self, cls, claimed, cap):
        out = recompute_confidence([ev(cls)], claimed)
        assert out <= claimed < cap

    def test_control_shared_quantiser_floors_same_input(self):
        assert floor_conf(0.7499) == 0.74

    def test_control_low_claims_pass_through_unraised(self):
        assert recompute_confidence([], 0.31) == 0.31


# ── R9: C2 scope filter source pin (run 2's missing pin, re-landed here) ───

class TestC2ScopeFilterIsLoadBearingSourcePin:
    def test_seal_guard_filters_foreign_checkpoints_in_source(self):
        src = inspect.getsource(seal_guard)
        assert "ck.run == trace.run" in src, (
            "C2 scope fix deleted — cross-run laundering returns and every "
            "shipped C1/C2 test stays green (run-2 P1)")

    def test_behavioural_pin_on_resumed_trace(self):
        ledger = _RecordingLedger()

        class _ForeignCK(Checkpoint):
            pass

        foreign = Checkpoint(key="kA", run="other-run", stage="fetch_leaf",
                             input_hash="ih",
                             payload={"fetches": [{"body": "foreign-bytes",
                                                   "url": "https://a/x"}]})
        trace = RunTrace(run="this-run")
        trace.stages.append(StageOutcome(stage="fetch_leaf", resumed=True,
                                         payload={},
                                         produced_at="2026-08-23T00:00:00"))
        verdict, _ = seal_guard(trace, [foreign], ledger)
        assert verdict == "SEAL"
        assert ledger.records == [], "foreign-run bytes entered this ledger"


class _RecordingLedger:
    def __init__(self):
        self.records = []

    def record_tool_result(self, tool, body, primary=True, urls=None):
        self.records.append((body, primary))

    def has_observation(self, body):
        return any(b == body for b, _ in self.records)


# ── R6/run-1 R1: _trace_from_payload still doesn't restore admitted fetches ─

class TestTraceRestorePopulatesAdmitted:
    @pytest.mark.xfail(strict=True,
                       reason="R6/R1: docstring claims admitted restoration; "
                              "body never touches trace.admitted")
    def test_trace_restore_populates_admitted(self):
        payload = {"fetches": [{"url": "https://a/x",
                                "content_sha256": "deadbeef",
                                "source_name": "s"}],
                   "rejections": [], "independent_keys": ["k1"],
                   "queries": ["q"], "stop_reason": "done"}
        t = _trace_from_payload("qid", payload)
        assert len(t.admitted) == 1

    def test_control_payload_actually_carries_fetches(self):
        payload = {"fetches": [{"url": "https://a/x"}], "rejections": []}
        assert payload["fetches"]


# ── R8: characterisation pin on the gate's real semantics ──────────────────

class TestRelevanceGateCharacterisation:
    def test_gate_holds_coverage_invariant_exactly(self):
        import random
        import string
        g = RelevanceGate(min_coverage=0.5)
        rng = random.Random(7)
        for _ in range(500):
            words = ["".join(rng.choice(string.ascii_lowercase)
                             for _ in range(6))
                     for _ in range(rng.randint(2, 8))]
            k = rng.randint(0, len(words))
            ok, cov, _ = g.judge(" ".join(words), "",
                                 {"text": " ".join(words[:k] + ["zzzqqq"])})
            assert ok == (cov >= g.min_coverage - 1e-9)

    def test_short_word_overlap_admits_semantically_empty_content(self):
        # Documents WHY word-overlap is not relevance (R8). At the production
        # default (0.25) this content would be admitted into evidence.
        g = RelevanceGate(min_coverage=0.5)
        q = ("what does recent research say about united states "
             "semiconductor supply chain resilience")
        ok, cov, _ = g.judge(q, "", {"text": "states supply the chain"})
        assert ok and cov >= 0.5
