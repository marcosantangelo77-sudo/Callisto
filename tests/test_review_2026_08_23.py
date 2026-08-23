"""REVIEW 2026-08-23 — standing review role, reproductions for findings.

Each defect found while auditing recent commits is reproduced here as an
xfail(strict=True) test, per the convention set by 29b7afd: the suite stays
green, the failure stays visible, and a future fix flips these loudly.

Findings reproduced (see findings/review_2026-08-23.md):

  R1  _trace_from_payload does not restore trace.admitted although its own
      docstring claims "admitted fetches" are restored (fix a5292e5 /
      f702bd6).
  R2  The fetch_leaf payload omits trace.rounds entirely, so on resume
      classify_null() loses source-error disclosure and can claim "no fetch
      was attempted" for a leaf whose fetches were attempted and errored.
      Same commit claims "a resumed run scores exactly what the live run
      scored"; the honesty-reporting layer does not.
  R3  tools/sources/base.independence_family() is a THIRD copy of the
      family-membership rule, still unnormalised ('semantic_scholar' does
      not match member 'semanticscholar') — exactly the bug 102f319 said
      was eliminated ("one membership rule, not two"). Currently inert
      (no callers), which is how it survived.

Positive controls (no xfail) prove the harnesses work, so the xfails fail
for the right reason.
"""
from __future__ import annotations

import pytest

from tests.helpers.no_socket import NoSocket

_guard = NoSocket()
_guard.install()

from tools.gaps import classify_gap  # noqa: E402
from tools.pipeline.engine import _trace_from_payload  # noqa: E402
from tools.pipeline.synthesis import classify_null  # noqa: E402
from tools.sources.base import independence_family  # noqa: E402


# ── fixtures ────────────────────────────────────────────────────────────────

def _payload(*, with_rounds=False):
    """A fetch_leaf payload shaped exactly as engine._fetch_payload writes
    it (engine.py stores fetches/rejections/independent_keys/queries/
    stop_reason — nothing else)."""
    p = {
        "fetches": [
            {"source_name": "openalex", "url": "https://api.openalex.org/w",
             "content_sha256": "ab" * 32, "body": "{}", "parsed": "{}",
             "question_id": "q1"},
        ],
        "rejections": [
            {"source_name": "gdelt", "url": "https://g/1",
             "reason": "content covers 8% of the question's topical words",
             "relevance_score": 0.08, "content_sha256": "cd" * 32},
        ],
        "independent_keys": ["api.openalex.org"],
        "queries": ["semiconductor supply chain resilience"],
        "stop_reason": "sufficient: 1 independent sources >= required 1",
    }
    if with_rounds:
        # What the LIVE run's trace.rounds held — never checkpointed.
        p["rounds"] = [{
            "round": 1,
            "query": p["queries"][0],
            "sources": [{"name": "gdelt", "admitted": True},
                        {"name": "openalex", "admitted": True}],
            "admitted": 2,
        }]
    return p


def _question():
    rq = type("Q", (), {})()
    rq.question_id = "q1"
    rq.text = "What does recent scholarly research say about semiconductor " \
              "supply chain resilience?"
    rq.evidence_requirements = None
    return rq


def _registry():
    class _Adapter:
        def __init__(self, spec):
            self.spec = spec

    class _Reg:
        def names(self):
            return ["openalex"]

        def select_explained(self, q):
            return []

        def get(self, name):
            if name == "openalex":
                return _Adapter(type("S", (), {
                    "name": "openalex",
                    "answers": ("scholarly work search by title, author, "
                                "or topic",),
                    "cannot_answer": (),
                })())
            return None

        def specs(self):
            return []
    return _Reg()


# ── positive controls ───────────────────────────────────────────────────────

def test_control_restored_trace_carries_rejections_and_keys():
    tr = _trace_from_payload("q1", _payload())
    assert len(tr.rejected) == 1
    assert tr.independent_keys == {"api.openalex.org"}
    assert tr.queries == ["semiconductor supply chain resilience"]


def test_control_live_equivalent_trace_classifies_as_honest_null():
    """With rounds present, classify_null reports the honest null and
    discloses what happened — this is the behaviour resume must match."""
    from tools.pipeline.retrieval import RejectedItem, RetrievalTrace
    tr = RetrievalTrace(question_id="q1")
    tr.queries = ["semiconductor supply chain resilience"]
    tr.rounds = [{
        "round": 1,
        "query": "semiconductor supply chain resilience",
        "sources": [{"name": "gdelt", "rejected": "below coverage"},
                    {"name": "openalex", "error": "HTTP 503"}],
        "admitted": 0,
    }]
    tr.rejected = [RejectedItem(
        source_name="gdelt", url="https://g/1",
        reason="content covers 8% of the question's topical words",
        relevance_score=0.08, content_sha256="cd" * 32)]
    tr.stop_reason = "terminator"
    v = classify_null(tr)
    assert "HTTP 503" in v.explanation


# ── R1: admitted fetches are not restored ───────────────────────────────────

@pytest.mark.xfail(strict=True, reason=(
    "R1: _trace_from_payload restores rejections/keys/queries/stop_reason "
    "but leaves trace.admitted == [], contradicting its docstring "
    "('admitted fetches ... restored'). Consumers of n_admitted see zero "
    "admissions on every resumed run."))
def test_resumed_trace_restores_admitted_fetches():
    tr = _trace_from_payload("q1", _payload())
    assert len(tr.admitted) == len(_payload()["fetches"])
    assert tr.n_admitted >= 1


@pytest.mark.xfail(strict=True, reason=(
    "R1 consumer view: gaps.classify_gap reads trace.admitted; on a "
    "restored trace it must report the admissions the live run recorded, "
    "not zero."))
def test_gap_classifier_sees_admissions_on_restored_trace():
    gap = classify_gap(_registry(), _trace_from_payload("q1", _payload()),
                       _question(), "scholarly work search")
    assert gap.n_admitted >= 1


# ── R2: rounds are lost, so null classification loses the plot ─────────────

@pytest.mark.xfail(strict=True, reason=(
    "R2: the fetch_leaf payload omits trace.rounds, so a resumed empty "
    "leaf whose sources ERRORED (no rejections) loses all error "
    "disclosure and attempted_anything goes False — classify_null then "
    "claims 'no fetch was attempted' for fetches that were attempted "
    "and failed."))
def test_resumed_empty_leaf_does_not_claim_no_fetch_attempted():
    p = _payload(with_rounds=True)
    p["fetches"] = []
    p["rejections"] = []
    p["rounds"][0]["sources"] = [{"name": "openalex", "error": "HTTP 503"}]
    tr = _trace_from_payload("q1", p)
    v = classify_null(tr)
    assert "no fetch was attempted" not in v.explanation
    assert "HTTP 503" in v.explanation


# ── R3: third copy of the membership rule, still unnormalised ──────────────

@pytest.mark.xfail(strict=True, reason=(
    "R3: tools/sources/base.independence_family() is a third copy of the "
    "family-membership rule and still uses raw 'in members'. 102f319 "
    "declared 'one membership rule, not two'; this copy predates that "
    "commit and survives beside it. Inert today (zero callers) — the test "
    "pins the invariant so wiring it cannot silently re-inflate "
    "independence."))
def test_independence_family_membership_is_normalised():
    assert independence_family("semantic_scholar") == "scholarly-aggregator"
    assert independence_family("Semantic Scholar") == "scholarly-aggregator"
