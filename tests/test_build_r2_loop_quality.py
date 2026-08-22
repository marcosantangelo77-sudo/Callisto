"""R2 build tests — loop quality machinery (tools/loop_quality.py).

Covers the five R2 jobs:
  1. Information-gain termination (StopDecision / InformationGainTerminator)
  2. Loop-level calibration trace (scoreable per-iteration record shape)
  3. Explicit, disconfirming-biased state compaction
  4. Anti-thrash: pure progress-window evaluator fixing the diagnosis-refire
     and DB-sentinel flaws found in characterization
  5. Per-phase task_class allocation matching ProviderRouter's declared set
"""

from __future__ import annotations

import json

import pytest

from tools.loop_quality import (
    LOOP_PHASE_TASK_CLASSES,
    InformationGainTerminator,
    LoopCalibrationTrace,
    ProgressVerdict,
    StopDecision,
    compact_state,
    evaluate_progress_window,
    phase_sequence,
    task_class_for_iteration,
    task_class_for_phase,
)


# ══════════════════════════════════════════════════════════════════════
# 1. Termination by information gain
# ══════════════════════════════════════════════════════════════════════


class TestInformationGainTerminator:

    def test_never_stops_before_min_iterations_even_if_flat(self):
        t = InformationGainTerminator(min_iterations=3, stagnant_iterations_needed=2)
        assert not t.record(0.50).stop
        assert not t.record(0.50).stop  # flat, but min_iterations guards

    def test_stops_after_stagnant_plateau(self):
        t = InformationGainTerminator(
            min_iterations=3, stagnant_iterations_needed=2,
            confidence_delta_threshold=0.05,
        )
        seq = (0.30, 0.62, 0.60, 0.60, 0.60)  # big moves then flat plateau
        for c in seq:
            dec = t.record(c)
        assert dec.stop
        assert dec.code == "info_gain_stalled"
        # The reason must diagnose WHY — a premature stop is diagnosable.
        assert "stalled" in dec.reason
        assert f"{t.confidence_delta_threshold:.3f}" in dec.reason

    def test_keeps_going_while_information_is_alive(self):
        t = InformationGainTerminator(min_iterations=3, max_iterations=20)
        for i, c in enumerate((0.30, 0.45, 0.58, 0.70, 0.82, 0.90)):
            dec = t.record(c)
            assert not dec.stop, f"stopped early at iter {i}: {dec.reason}"

    def test_hard_ceiling_always_stops(self):
        t = InformationGainTerminator(min_iterations=2, max_iterations=5)
        for c in (0.10, 0.35, 0.55, 0.75, 0.92):
            dec = t.record(c)  # every move is huge; ceiling wins at 5
        assert dec.stop
        assert dec.code == "max_iterations"

    def test_single_small_move_resets_not_stops(self):
        t = InformationGainTerminator(
            min_iterations=3, confidence_delta_threshold=0.02,
            stagnant_iterations_needed=2,
        )
        seq = (0.30, 0.31, 0.35, 0.36, 0.40)  # alternating small/large moves
        last = None
        for c in seq:
            last = t.record(c)
        # deltas of 0.01, 0.04, 0.01, 0.04 → never two consecutive stagnant
        assert not last.stop

    def test_decision_is_fully_diagnosed(self):
        t = InformationGainTerminator()
        dec = t.evaluate_and_log(0.5)
        assert isinstance(dec, StopDecision)
        assert dec.iteration == 1
        assert dec.marginal_confidence_delta == float("inf")
        assert dec.reason

    def test_config_validation_rejects_nonsense(self):
        with pytest.raises(ValueError):
            InformationGainTerminator(min_iterations=0)
        with pytest.raises(ValueError):
            InformationGainTerminator(min_iterations=5, max_iterations=3)
        with pytest.raises(ValueError):
            InformationGainTerminator(confidence_delta_threshold=0)

    def test_confidence_range_enforced(self):
        t = InformationGainTerminator()
        with pytest.raises(ValueError):
            t.record(1.5)

    def test_decisions_history_preserved(self):
        t = InformationGainTerminator()
        t.record(0.5)
        t.record(0.7)
        ds = t.decisions()
        assert len(ds) == 2 and all(isinstance(d, StopDecision) for d in ds)


# ══════════════════════════════════════════════════════════════════════
# 2. Calibration hooks
# ══════════════════════════════════════════════════════════════════════


class TestLoopCalibrationTrace:

    def test_record_shape_is_stable_and_serializable(self):
        tr = LoopCalibrationTrace(subject="nvda-q3")
        tr.add_iteration(0.4, {"confirming": 2, "disconfirming": 1, "neutral": 3},
                         task_class="extraction")
        rec = tr.to_records()[0]
        json.dumps(rec)  # must be plain JSON
        assert rec["iteration"] == 1
        assert rec["confidence"] == 0.4
        assert rec["evidence_total"] == 6
        for k in ("timestamp", "confirming", "disconfirming", "neutral",
                  "task_class", "notes"):
            assert k in rec

    def test_iterations_are_monotonically_numbered(self):
        tr = LoopCalibrationTrace()
        for i in range(3):
            r = tr.add_iteration(0.3 + i * 0.1, {"confirming": i})
            assert r.iteration == i + 1

    def test_confidence_rising_without_disconfirmation_flags_overconfidence(self):
        tr = LoopCalibrationTrace()
        tr.add_iteration(0.30, {"confirming": 1})
        for i in range(4):
            tr.add_iteration(0.30 + 0.15 * (i + 1), {"confirming": 2})
        s = tr.summary()
        assert s["final_confidence"] == pytest.approx(0.90)
        assert s["overconfidence_suspected"] is True

    def test_disconfirming_evidence_registered_clears_flag(self):
        tr = LoopCalibrationTrace()
        tr.add_iteration(0.30, {"confirming": 1})
        tr.add_iteration(0.45, {"disconfirming": 2})
        tr.add_iteration(0.80, {"confirming": 3})
        s = tr.summary()
        assert s["overconfidence_suspected"] is False
        assert s["disconfirming_seen"] == 2

    def test_summary_counts_gain_of_both_kinds(self):
        tr = LoopCalibrationTrace()
        tr.add_iteration(0.30, {"confirming": 2, "disconfirming": 1})
        tr.add_iteration(0.60, {"confirming": 4, "disconfirming": 1})
        s = tr.summary()
        assert s["iterations"] == 2
        assert s["confidence_gain"] == pytest.approx(0.30)
        assert s["evidence_gain"] == 2

    def test_empty_trace_summary_is_safe(self):
        assert LoopCalibrationTrace().summary() == {"iterations": 0}

    def test_confidence_range_enforced(self):
        tr = LoopCalibrationTrace()
        with pytest.raises(ValueError):
            tr.add_iteration(-0.1, {})


# ══════════════════════════════════════════════════════════════════════
# 3. State compaction
# ══════════════════════════════════════════════════════════════════════


def _item(id_, stance, tier=4):
    return {"id": id_, "content": f"content-{id_}", "stance": stance, "tier": tier}


class TestCompactState:

    def test_contradicting_items_never_dropped_however_many(self):
        items = [_item(f"c{i}", "contradicting") for i in range(12)]
        kept, dropped = compact_state(items)
        assert sum(1 for k in kept if k["stance"] == "contradicting") == 12
        assert dropped == []

    def test_supporting_capped_tier_one_survives_first(self):
        items = (
            [_item("bad", "supporting", tier=5)]
            + [_item(f"g{i}", "supporting", tier=1) for i in range(9)]
        )
        kept, dropped = compact_state(items, max_supporting=8)
        kept_ids = [k["id"] for k in kept]
        assert "g0" in kept_ids and "bad" not in kept_ids
        d = [x for x in dropped if x["id"] == "bad"][0]
        assert "budget" in d["dropped_reason"]

    def test_the_one_contradicting_source_survives_among_noise(self):
        # The exact failure mode from the mandate: compaction silently drops
        # the one contradicting source among many supporting ones.
        items = [_item(f"s{i}", "supporting", tier=1) for i in range(20)]
        items.append(_item("dissent", "contradicting", tier=3))
        kept, _ = compact_state(items, max_supporting=8)
        assert any(k["id"] == "dissent" for k in kept)

    def test_neutral_capped(self):
        items = [_item(f"n{i}", "neutral") for i in range(6)]
        kept, dropped = compact_state(items, max_neutral=4)
        assert sum(1 for k in kept if k["stance"] == "neutral") == 4
        assert len(dropped) == 2

    def test_unknown_stance_treated_as_neutral(self):
        kept, _ = compact_state([{"id": 1, "stance": "weird"}])
        assert kept[0]["stance"] == "neutral"

    def test_unknown_tier_defaults_to_secondary(self):
        kept, _ = compact_state([{"id": 1, "stance": "neutral"}])
        assert kept[0]["tier"] == 4

    def test_missing_id_is_loud(self):
        with pytest.raises(ValueError):
            compact_state([{"stance": "supporting"}])

    def test_input_not_mutated(self):
        original = {"id": 1, "stance": "supporting", "tier": "3"}
        snapshot = dict(original)
        compact_state([original])
        assert original == snapshot

    def test_drop_reasons_present_on_every_dropped_item(self):
        items = [_item(f"s{i}", "supporting") for i in range(10)]
        _, dropped = compact_state(items, max_supporting=2)
        assert dropped and all("dropped_reason" in d for d in dropped)

    def test_deterministic_ordering(self):
        items = [_item(f"s{i}", "supporting", tier=(i % 3) + 1) for i in range(10)]
        k1, d1 = compact_state(items, max_supporting=3)
        k2, d2 = compact_state(items, max_supporting=3)
        assert [k["id"] for k in k1] == [k["id"] for k in k2]
        assert [d["id"] for d in d1] == [d["id"] for d in d2]


# ══════════════════════════════════════════════════════════════════════
# 4. Anti-thrash (pure evaluator)
# ══════════════════════════════════════════════════════════════════════


def _snap(cycle, promotions=0, signals=0):
    return {"cycle": cycle, "promotions": promotions, "total_signals": signals}


class TestEvaluateProgressWindow:

    def test_first_snapshot_is_baseline_not_progress_judgment(self):
        v = evaluate_progress_window(None, _snap(10), 0, False)
        assert v.progressing and v.consecutive_no_progress == 0

    def test_promotions_are_progress(self):
        v = evaluate_progress_window(_snap(10), _snap(20, promotions=2), 1, False)
        assert v.progressing and v.consecutive_no_progress == 0

    def test_signals_are_progress(self):
        v = evaluate_progress_window(_snap(10, signals=5), _snap(20, signals=9), 1, False)
        assert v.progressing

    def test_flat_accumulates_to_spinning(self):
        streak = 0
        diag_fired = False
        prev = _snap(10)
        for cycle in (20, 30, 40):
            v = evaluate_progress_window(prev, _snap(cycle), streak, diag_fired)
            prev, streak, diag_fired = _snap(cycle), v.consecutive_no_progress, v.diagnose
        assert streak >= 3 and not v.progressing

    def test_diagnosis_fires_once_per_episode_not_every_check(self):
        # THE FIX: inline version re-fired the Claude escalation on every
        # subsequent no-progress check. Pure version fires once.
        v1 = evaluate_progress_window(_snap(10), _snap(20), 2, False)
        assert v1.diagnose  # first spin detection diagnoses
        v2 = evaluate_progress_window(_snap(20), _snap(30), v1.consecutive_no_progress, True)
        assert v2.spinning and not v2.diagnose  # still spinning, no re-fire

    def test_db_sentinel_negative_means_unknown_not_regression(self):
        v = evaluate_progress_window(
            _snap(10, signals=-1), _snap(20, signals=-1), 0, False)
        assert v.detail  # handled without nonsense negative deltas


# ══════════════════════════════════════════════════════════════════════
# 5. Per-phase task-class allocation
# ══════════════════════════════════════════════════════════════════════

# Declared task_classes in config/providers.yaml routing.task_classes.
DECLARED_TASK_CLASSES = {
    "hypothesis_generation", "research_synthesis", "screening", "extraction",
    "classification", "backtest_interpretation", "promotion_judgment",
    "adversarial_review",
}


class TestPhaseAllocation:

    def test_all_phase_classes_declared_in_providers_yaml_set(self):
        for phase, tc in LOOP_PHASE_TASK_CLASSES.items():
            assert tc in DECLARED_TASK_CLASSES, (
                f"phase {phase} emits undeclared task_class {tc!r}"
            )

    def test_first_iteration_frames_last_adversarial(self):
        total = 8
        assert phase_sequence(0, total) == "framing"
        assert phase_sequence(total - 1, total) == "adversarial_review"

    def test_middle_is_grind_with_synthesis_checkpoints(self):
        phases = [phase_sequence(i, 10) for i in range(10)]
        middle = phases[1:-1]
        assert all(p in ("evidence_grind", "interim_synthesis") for p in middle)
        assert middle.count("interim_synthesis") >= 2

    def test_short_loop_collapses_gracefully(self):
        assert phase_sequence(0, 1) == "framing"
        assert phase_sequence(0, 2) == "framing"
        assert phase_sequence(1, 2) == "adversarial_review"

    def test_capability_ordering_front_ends_get_heavy_classes(self):
        # Framing and adversarial review must route to capability tiers;
        # grind to an extraction-class (local-friendly) tier.
        assert task_class_for_phase("framing") == "promotion_judgment"
        assert task_class_for_phase("adversarial_review") == "adversarial_review"
        assert task_class_for_phase("evidence_grind") == "extraction"

    def test_unknown_phase_raises_loudly(self):
        with pytest.raises(KeyError):
            task_class_for_phase("vibes")

    def test_out_of_range_position_raises(self):
        with pytest.raises(ValueError):
            phase_sequence(5, 5)
        with pytest.raises(ValueError):
            phase_sequence(-1, 5)
        with pytest.raises(ValueError):
            phase_sequence(0, 0)

    def test_task_class_for_iteration_end_to_end(self):
        assert task_class_for_iteration(0, 6) == "promotion_judgment"
        assert task_class_for_iteration(5, 6) == "adversarial_review"
