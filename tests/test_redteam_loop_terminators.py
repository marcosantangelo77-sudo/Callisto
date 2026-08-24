"""RED TEAM H2/H4/H5 — the loop's termination and memory metrics.

H2: the information-gain terminator is satisfiable by making confidence
    STOP MOVING — including by producing nothing. A run that emits the
    same number every iteration "learns" its way to a stop.
H4: compact_state keeps contradicting items only if they ARRIVE labelled
    "contradicting". Anything upstream does to the label (or to the item)
    before compaction makes the bias unreachable.
H5: progress metrics reward volume, not value: one signal resets the spin
    detector; a resolution recorded for a claim that never resolved moves
    the promotion-adjacent counters.
"""
import pytest

from tools.loop_quality import (
    InformationGainTerminator,
    LoopCalibrationTrace,
    compact_state,
    evaluate_progress_window,
)


# ── H2: stopping by stagnation without learning ─────────────────────────

def test_terminator_stops_a_run_that_never_learned_anything():
    """Constant confidence from iteration 1 → stop at exactly min+needed-1.
    The run produced zero evidence and zero movement; the terminator reads
    the plateau as 'additional evidence no longer materially moving the
    estimate' — but there never WAS any estimate movement to stall."""
    t = InformationGainTerminator(min_iterations=3, stagnant_iterations_needed=2)
    decisions = [t.record(0.50) for _ in range(3)]
    assert decisions[-1].stop is True
    assert decisions[-1].code == "info_gain_stalled"
    # The reason claims evidence stopped moving the estimate; nothing
    # distinguishes this from a run whose evidence stream was EMPTY.
    assert "no longer materially moving" in decisions[-1].reason


def test_terminator_cannot_distinguish_null_run_from_converged_run():
    """Two trajectories, identical terminator verdicts:
      A) confidence moved 0.30 -> 0.52 over real evidence then plateaued;
      B) a broken retrieval returned zero fetches every iteration and the
         model defaulted 0.50 forever.
    The StopDecision carries no evidence counts, so downstream consumers
    cannot tell honest convergence from productive-looking idling."""
    a = InformationGainTerminator(min_iterations=3, stagnant_iterations_needed=2)
    for c in (0.30, 0.52, 0.52, 0.52):
        da = a.record(c)
    b = InformationGainTerminator(min_iterations=3, stagnant_iterations_needed=2)
    for c in (0.50, 0.50, 0.50):
        db = b.record(c)
    assert da.stop and db.stop
    assert da.code == db.code == "info_gain_stalled"
    # Same code, same shape — the ONLY difference lives outside the decision.
    assert a.decisions()[-1].marginal_confidence_delta == \
           pytest.approx(0.0) and \
           b.decisions()[-1].marginal_confidence_delta == pytest.approx(0.0)


def test_terminator_rewards_wobbling_over_learning():
    """A model that perturbs its confidence by ±0.021 each iteration NEVER
    stops early — it burns the full max_iterations budget while learning
    nothing, and its 'information gain alive' log lines look like diligence."""
    t = InformationGainTerminator(min_iterations=3, max_iterations=8,
                                  confidence_delta_threshold=0.02,
                                  stagnant_iterations_needed=2)
    conf = 0.5
    codes = []
    for i in range(10):
        conf = 0.5 + (0.021 if i % 2 == 0 else -0.021)
        dec = t.record(conf)
        if dec.iteration >= t.max_iterations:
            assert dec.stop and dec.code == "max_iterations"
            return
        codes.append(dec.code)
    # If we get here the wobble survived past max without a stall code:
    pytest.fail("wobbling run terminated before max_iterations")


def test_calibration_trace_flags_overconfidence_but_not_underproduction():
    trace = LoopCalibrationTrace(subject="rt")
    trace.add_iteration(confidence=0.40, evidence_counts={"confirming": 0, "disconfirming": 0, "neutral": 0})
    s_empty = trace.summary()
    assert s_empty["evidence_gain"] == 0
    # Zero evidence across iterations is NOT flagged by anything:
    assert "underproduction" not in str(s_empty)
    assert s_empty.get("overconfidence_suspected") in (False, None)


# ── H4: disconfirming evidence can vanish before compaction sees it ─────

BASE = {"id": "x", "content": "c", "tier": 1, "iteration": 1}


def test_compaction_preserves_only_arriving_contradicting_labels():
    kept, dropped = compact_state([{**BASE, "stance": "contradicting"}])
    assert len(kept) == 1 and not dropped


@pytest.mark.parametrize("label", ["CONTRADICTING ", "contra", "against",
                                   "refuting", "disconfirms", "", None])
def test_unrecognised_labels_get_reclassified_neutral_and_budget_capped(label):
    """Anything not spelled exactly 'supporting'/'contradicting' becomes
    neutral — and neutral has a budget of 4. A producer that mislabels
    dissent ('stance: refutes claim') gets it DROPPED under budget while
    the 'contradicting are never dropped' guarantee stays technically true.
    This is the pre-compaction disappearance channel of H4."""
    many = [{**BASE, "id": f"d{i}", "stance": label} for i in range(6)]
    kept, dropped = compact_state(many)
    assert len(dropped) >= 1  # dissent destroyed by a spelling mismatch


def test_mislabelling_supporting_as_neutral_evicts_real_dissent():
    """Six genuine dissents mislabelled neutral + four neutrals: the
    neutrals with better tiers evict the dissent from the budget."""
    items = (
        [{**BASE, "id": f"mis{i}", "stance": "neutral", "tier": 1}
         for i in range(4)]
        + [{**BASE, "id": f"diss{i}", "stance": "neutral", "tier": 3}
           for i in range(6)]
    )
    kept, dropped = compact_state(items)
    kept_ids = {k["id"] for k in kept}
    assert all(f"diss{i}" in dropped_reason_ids(dropped) for i in range(2))


def dropped_reason_ids(dropped):
    return {d["id"] for d in dropped}


# ── H5: progress dressed as progress ────────────────────────────────────

def test_one_trivial_signal_resets_the_spin_detector():
    """evaluate_progress_window treats ANY new signal as productivity. A
    threshold-noise blip producing a single garbage signal every 9 cycles
    keeps the loop permanently classified as 'productive' — no diagnosis,
    ever, while promotions stay at zero for months."""
    prev = {"cycle": 10, "promotions": 0, "total_signals": 100}
    curr_noise = {"cycle": 20, "promotions": 0, "total_signals": 101}
    v = evaluate_progress_window(prev, curr_noise, consecutive_no_progress=2,
                                 already_diagnosed_this_episode=False)
    assert v.progressing is True          # noise clears the streak
    assert v.consecutive_no_progress == 0  # and erases the history


def test_signal_count_is_volume_not_value():
    """The snapshot metric cannot see signal QUALITY: retroactively
    rewriting historical signal_generated flags (the documented 2026-08
    contamination) would register as a productivity surge."""
    prev = {"cycle": 10, "promotions": 0, "total_signals": 0}
    rewritten = {"cycle": 20, "promotions": 0, "total_signals": 500}
    v = evaluate_progress_window(prev, rewritten, 2, False)
    assert v.progressing is True
    assert "+500 signals" in v.detail


def test_unknown_signals_are_treated_as_not_negative_but_also_not_progress():
    prev = {"cycle": 10, "promotions": 0, "total_signals": -1}
    curr = {"cycle": 20, "promotions": 0, "total_signals": 7}
    # prev unknown (-1): delta vs 7 must not be counted as +7 progress.
    v = evaluate_progress_window(prev, curr, 0, False)
    # Documented behaviour: sentinel -> signals_known False -> progressing
    # only via promotions. Verify it can NOT be gamed in the other
    # direction either (curr=-1 after known prev).
    v2 = evaluate_progress_window(
        {"cycle": 10, "promotions": 0, "total_signals": 50},
        {"cycle": 20, "promotions": 0, "total_signals": -1}, 0, False)
    assert v2.progressing is False
