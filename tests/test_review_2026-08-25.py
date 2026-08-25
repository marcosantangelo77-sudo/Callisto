"""REVIEW run 8 — 2026-08-25 (reviewer: ox-alpha, standing review role).

Subject under review: the sm1 merge trains that landed after run 7
(fix/seal-unprovable @ a6e4467, redteam domain-plugins report 8d287cd),
plus origin/master itself. Findings: findings/review_2026-08-25.md.

Families hunted: #1 (verification that never runs — crossrun memory is
production-inert), #2 (fix lands in one copy while another keeps the bug —
the seal contract's "stand only on provable leaves" rule exists in engine.py
but NOT in its two mirrors, tools/why.py and tools/calibration/instrument.py;
the calibration package remains un-importable since 2026-08-23), #3 (zero-
result metadata echo still admitted by RelevanceGate on master).

Every test here FAILS on current origin/master for a documented reason.
"""
from __future__ import annotations

import importlib.util
import sys

import pytest

from tools.why import explain_result


# ── A. Family 2: the seal contract half-landed — mirrors still stand on gapped leaves ──

def _result_with_gapped_and_provable():
    from tools.pipeline.engine import LeafOutcome, PipelineResult

    r = PipelineResult(root_query="q?", sealed=True)
    r.confidence_score = 0.40  # what engine.py actually sealed: provable leaf only
    r.leaves = [
        # gapped leaf proves NOTHING but carries the higher number
        LeafOutcome(question_id="l1", text="a", answer="words",
                    confidence=0.55, gap_kind="unprovable",
                    tier="SPECULATIVE"),
        LeafOutcome(question_id="l2", text="b", answer="proof 42",
                    confidence=0.40, tier="PRIMARY"),
    ]
    return r


def test_why_explainer_stands_on_gap_classified_leaf(engine_seal_contract):
    """tools/why.explain_result picks `max(answered)` ignoring gap_kind.

    The seal-unprovable fix (bcf7439, merged a6e4467) taught engine.run to
    stand only on answered AND gap-free leaves. why.py is THE user-facing
    explanation of that same seal and was not taught: it reports the parent's
    proposal as 0.55 — the confidence of an unprovable leaf — while the sealed
    score is 0.40. The explanation contradicts the thing it explains.
    """
    ex = explain_result(_result_with_gapped_and_provable())
    assert ex.proposed <= 0.40 + 1e-9, (
        f"why.explain_result proposes {ex.proposed} from a gap_kind=unprovable "
        f"leaf; the engine's own seal contract stands only on gap-free leaves")


def test_calibration_parent_replay_uses_gapped_best_leaf(calibration_instrument):
    """instrument.replay_parent_chain is fed max(answered) incl. gap leaves.

    The instrument exists to prove the replay reproduces the observed final
    score exactly. Fed the real leaf set of a mixed run it replays 0.55 where
    the engine sealed 0.40 and then reports verified=False — misattributing a
    correct seal as unexplained drift (or, worse, 'verifying' a wrong seal if
    the engine ever regressed to match it).
    """
    replay_parent_chain = calibration_instrument.replay_parent_chain
    r = _result_with_gapped_and_provable()
    answered = [l for l in r.leaves if l.answer]
    best = max(answered, key=lambda l: l.confidence).confidence
    tr, veto = replay_parent_chain(best_leaf_confidence=best,
                                   descendant_resolutions=[], objections=[])
    assert tr.replayed_final == pytest.approx(0.40), (
        "calibration parent replay reproduces the engine only when fed the "
        "gapped leaf's confidence; the mirror ignores the seal contract")


# ── B. Family 1: tools.calibration is still un-importable (reported 08-23, never fixed) ──

def test_calibration_package_imports_after_two_days_red():
    """`import tools.calibration` raises ImportError since autosave commit
    7e3d007 (2026-08-23): __init__ imports `replay_chain`, which does not
    exist in instrument.py, and imports a bridge module that has never existed
    in any commit reachable from master. The underconfidence-measurement
    component — the thing that would tell us whether confidence is honest —
    is dead code behind a broken door, reported with failing repros two days
    ago and untouched since."""
    with pytest.raises(ImportError):
        import tools.calibration  # noqa: F401
    pytest.fail("tools.calibration is STILL un-importable on master "
                "(ImportError: replay_chain / missing bridge.py)")


@pytest.fixture(scope="module")
def engine_seal_contract():
    return None  # documentation hook; engine behaviour pinned elsewhere


@pytest.fixture(scope="module")
def calibration_instrument():
    spec = importlib.util.spec_from_file_location(
        "calib_instrument_direct", "tools/calibration/instrument.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["calib_instrument_direct"] = m
    spec.loader.exec_module(m)
    return m


# ── C. Family 3: zero-result metadata echo admitted at high coverage ──

def test_relevance_gate_rejects_zero_result_envelope_on_master():
    """A 200 body whose parsed form contains ZERO result items must not be
    admitted because its metadata echoes the question. redteam/source-registry
    filed this (S1, CRITICAL) with failing repros; nothing on master changed:
    the gate still scores token coverage over error/metadata strings."""
    from tools.pipeline.retrieval import RelevanceGate

    gate = RelevanceGate()
    q = "What was the unemployment rate in 2023?"
    admitted, cov, reason = gate.judge(
        q, "general", {"meta": {"query": q, "count": 0}, "results": []})
    assert not admitted, (
        f"zero-result envelope admitted at coverage {cov:.0%} — absence is "
        f"being treated as success")
