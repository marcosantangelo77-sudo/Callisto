"""REVIEW RUN 9 — 2026-08-25 (branch review/ox-alpha-0824e vs origin/master 6977793).

Families hunted: #2 (fix lands in one copy while mirrors keep the bug),
#3 (absence treated as success), #1/#4 (reviewer defect ledger silently
lost by merge trains).

All tests below are EXPECTED TO FAIL on current origin/master.
Each failure is the reproduction of a recorded defect.
"""
import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.pipeline.engine import LeafOutcome          # noqa: E402
from tools.pipeline.retrieval import RelevanceGate     # noqa: E402


# ── A · family 2, sixth recorded instance ─────────────────────────────────
# The R3 fix added answers_question to the ONE predicate inside engine.run,
# but tools/why.explain_result still picks max(answered) ignoring BOTH
# answers_question AND gap_kind. On a mixed run where the engine correctly
# sealed at the answering leaf's 0.40, the user-facing explanation reports
# proposed=0.90 — the confidence of a leaf that determines nothing.

def _mixed_leaves():
    return [
        LeafOutcome(question_id="l1", text="intraday rate?", answer="4.1",
                    confidence=0.90, answers_question=False),
        LeafOutcome(question_id="l2", text="annual change?", answer="higher",
                    confidence=0.40, answers_question=True),
    ]


class _Result:
    objections = []
    gap_kinds = []
    fetches = []
    notes = []
    session = None
    root_query = "Is unemployment higher now than two years ago?"
    confidence_score = 0.40
    refusal_reason = None
    sealed = True
    confidence_tier = "SPECULATIVE"


def test_why_explainer_stands_on_nonanswering_leaf():
    from tools.why import explain_result
    r = _Result()
    r.leaves = _mixed_leaves()
    ex = explain_result(r)
    assert ex.proposed == pytest.approx(0.40), (
        "why.py explains a seal that stood ONLY on the answering leaf "
        f"(0.40) but reports proposed={ex.proposed} — the non-answering "
        "leaf's confidence. Third copy of the best-leaf rule, third bug."
    )


def test_why_explainer_still_ignores_gap_kind():
    """Run 8's HIGH defect A, first half — still live after the merge train."""
    from tools.why import explain_result
    r = _Result()
    r.leaves = [
        LeafOutcome(question_id="l1", text="a?", answer="x", confidence=0.90,
                    gap_kind="unprovable"),
        LeafOutcome(question_id="l2", text="b?", answer="y", confidence=0.55,
                    answers_question=True),
    ]
    ex = explain_result(r)
    assert ex.proposed == pytest.approx(0.55)


# ── B · family 1 — tools.calibration un-importable, day 4 ─────────────────
# ed1cc34 (08-23) filed it, run 8 re-filed it, three merge trains since.

def test_calibration_package_imports_after_two_days_red():
    try:
        import tools.calibration  # noqa: F401
    except ImportError as e:  # pragma: no cover - this IS the failure path
        pytest.fail(f"tools.calibration un-importable (inert behind it): {e}")


# ── C · family 3 — zero-result echo envelope now admits at FULL coverage ──
# S1 (redteam_source_registry, marked CRITICAL) was live at 75% before the
# R2/R2b gate fix merged (8740be5). The fix's own denominator/prefix work
# made it WORSE: extract_text walks meta.query too, so an empty envelope
# echoing the question matches EVERY question token — 100% coverage, top
# reason string. Absence is not just success now; it is a perfect score.

def test_relevance_gate_rejects_zero_result_envelope():
    g = RelevanceGate()
    q = "What was the unemployment rate in January 2023?"
    envelope = {"meta": {"query": q, "count": 0}, "results": []}
    admitted, coverage, reason = g.judge(q, "factual", envelope)
    assert not admitted, (
        "RelevanceGate admits a zero-result envelope whose only content is "
        f"the echoed question itself (coverage={coverage:.0%}, "
        f"reason={reason!r}). Empty evidence must fail closed; here absence "
        "scores 100%."
    )
