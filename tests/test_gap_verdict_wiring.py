"""The three-way gap verdict is carried onto the sealed result.

Contract under test:

  1. HONEST NULL        searched competently and found nothing.
  2. RETRIEVAL FAILURE  we failed to FETCH — must NEVER read as an honest
                        null ("we could not look" is not "nothing there").
  3. UNPROVABLE         evidence came back but cannot meet our own bar.

CLASSIFICATION ONLY: no test here tolerates any confidence movement from
the verdict — the sealed numbers are identical with and without it.
"""
from __future__ import annotations

import pytest

from tools.gaps import NullKind, classify_null_kind
from tools.pipeline.engine import LeafOutcome
from tools.pipeline.retrieval import RetrievalTrace


def trace(rounds=None, rejected=None, stop="done"):
    return RetrievalTrace(question_id="q1", rounds=rounds or [],
                          rejected=rejected or [], stop_reason=stop)


class TestClassifyNullKind:
    def test_gate_rejections_are_honest_null(self):
        kind, expl = classify_null_kind(trace(
            rounds=[{"round": 1, "sources": [
                {"name": "openalex", "rejected": "covers 0%"}]}],
            stop="stagnant"))
        assert kind == NullKind.HONEST_NULL.value

    def test_source_error_is_retrieval_failure(self):
        kind, expl = classify_null_kind(trace(
            rounds=[{"round": 1, "sources": [
                {"name": "fred", "error": "HTTP 429 rate limit"}]}],
            stop="terminator"))
        assert kind == NullKind.RETRIEVAL_FAILURE.value
        # The conflation guard is IN THE PROSE, not just the enum:
        assert "RETRIEVAL FAILURE" in expl
        assert "do not read" in expl

    def test_nothing_attempted_is_retrieval_failure(self):
        kind, expl = classify_null_kind(trace(rounds=[], stop=""))
        assert kind == NullKind.RETRIEVAL_FAILURE.value

    def test_skipped_source_is_retrieval_failure_any_skip_reason(self):
        # Drift regression: the old synthesis copy matched only the exact
        # string "no generic route"; ANY skip means we never fetched there.
        kind, _ = classify_null_kind(trace(
            rounds=[{"round": 1, "sources": [
                {"name": "treasury", "skipped": "planner authored nothing"}]}],
            stop="selected sources lack fetch routes"))
        assert kind == NullKind.RETRIEVAL_FAILURE.value

    def test_mixed_null_discloses_partial_coverage(self):
        # Some sources answered-and-rejected, one errored: still an honest
        # null on what was obtained, but the partial coverage is visible.
        kind, expl = classify_null_kind(trace(
            rounds=[{"round": 1, "sources": [
                {"name": "openalex", "rejected": "irrelevant"},
                {"name": "fred", "error": "HTTP 500"}]}],
            rejected=[]))
        assert kind == NullKind.HONEST_NULL.value
        assert "errored" in expl


class TestLeafCarriesVerdict:
    """LeafOutcome carries gap_kind/gap_explanation as inert metadata."""

    def test_fields_exist_and_default_empty(self):
        leaf = LeafOutcome(question_id="q", text="t")
        assert leaf.gap_kind == ""
        assert leaf.gap_explanation == ""
        # roundtrip through asdict (checkpointing) keeps them
        import dataclasses
        d = dataclasses.asdict(leaf)
        assert d["gap_kind"] == "" and "gap_explanation" in d

    def test_unprovable_is_a_distinct_kind(self):
        assert NullKind.UNPROVABLE.value == "unprovable"
        assert (NullKind.HONEST_NULL.value !=
                NullKind.RETRIEVAL_FAILURE.value !=
                NullKind.UNPROVABLE.value)


class TestClassificationNeverMovesConfidence:
    """GATE RULE: the verdict is classification only."""

    def test_classify_null_kind_returns_only_strings(self):
        out = classify_null_kind(trace())
        assert isinstance(out, tuple) and len(out) == 2
        assert all(isinstance(x, str) for x in out)

    def test_leaf_confidence_untouched_by_verdict_assignment(self):
        leaf = LeafOutcome(question_id="q", text="t", confidence=0.42,
                           tier="SPECULATIVE")
        leaf.gap_kind = NullKind.RETRIEVAL_FAILURE.value
        leaf.gap_explanation = "we could not look"
        assert leaf.confidence == 0.42
        assert leaf.tier == "SPECULATIVE"
