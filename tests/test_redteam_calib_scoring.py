"""RED TEAM — CALIBRATION SCORING ITSELF (surface not previously covered).

Prior passes attacked confidence inflation *inside* the pipeline. This pass
attacks the layer that JUDGES the pipeline: retrodiction scoring, batch
reporting, the routing score store, the Thompson policy that consumes them,
and the inheritance rule those scores feed. If the referee can be flattered,
every downstream number (routing, ceilings, verdicts) inherits the flattery.

Convention (matches tests/test_redteam_retr_*): each test is a deterministic
reproduction of a CONFIRMED defect — it PASSES today and FAILS the moment the
defect is fixed, which is the canary signal to update findings/.

Findings (full write-up: findings/redteam_calibration.md):
  K1  _implied_outcome fabricates ground truth from the prediction itself ->
      build_report's calibration table is self-confirming whenever
      answer_binary is missing (resumed/legacy rows).
  K2  The routing store has no question-level identity: duplicates inflate,
      cherry-picked coverage wins 500/500, and decide() ignores task_class so
      one class's scores route another's calls.
  K3  Recency re-weighting lets 15 appended rows outweigh 60 measured ones —
      history is never rewritten but its meaning is.
  K4  Coin-flip descendants (Brier ~= 0.25, direction-only hits) lift a
      parent ceiling to ~0.66 — past the SPECULATIVE cap the rule promises.
  K5  The batch verdict ignores coverage: 50% nulls still reads "strongly
      better than chance", and sealed_rate reports the researcher's own flag.
"""
import random
import statistics
from datetime import date

import pytest

from tools.retrodiction.batch import (
    BatchResult,
    _implied_outcome,
    build_report,
    magnitude_score,
)
from tools.retrodiction.questions import QuestionType, RetrodictionQuestion
from tools.retrodiction.scoring import (
    Prediction,
    resolved_claim_record,
)
from tools.research_program import inherited_ceiling
from tools.routing.policy import CandidateModel, ThompsonRoutingPolicy
from tools.routing.scores import ModelScoreStore


def q(qid, answer):
    return RetrodictionQuestion(
        text=f"question {qid}", domain="FINANCIAL",
        question_type=QuestionType.BEAT_OR_MISS,
        claim_date=date(2024, 1, 1), resolution_date=date(2024, 3, 1),
        answer_binary=answer, question_id=qid)


# ── K1. Ground truth invented from the prediction ─────────────────────────

def test_K1_missing_truth_implied_outcome_agrees_with_prediction():
    """With no answer_binary recorded (legacy/resumed row), _implied_outcome
    'recovers' y from brier — and whenever the recorded brier admits both
    readings or none, the fallback is sign(p): the model agrees with itself.
    Random sweep: attacker-controlled brier values yield agreement with the
    prediction's own direction in effectively every case."""
    rng = random.Random(2024)
    agree = 0
    for _ in range(2000):
        p = rng.random()
        r = BatchResult(question_id="x", status="scored",
                        predicted_probability=p, answer_binary=None,
                        brier=0.0001)
        if _implied_outcome(r) == (1.0 if p >= 0.5 else 0.0):
            agree += 1
    assert agree == 2000  # perfect self-agreement = zero information


def test_K1_forged_brier_flips_reported_outcome():
    """Truth was y=1 and the model said p=0.05 (honest brier 0.9025). A row
    stored without answer_binary but with brier=0.0025 (consistent only with
    y=0) is reported as a correct low-probability call. Nothing cross-checks
    the pair."""
    r = BatchResult(question_id="x", status="scored",
                    predicted_probability=0.05, answer_binary=None,
                    brier=0.0025)
    assert _implied_outcome(r) == 0.0


def test_K1_calibration_table_self_confirms_without_truth():
    """build_report renders a perfect calibration table from rows that carry
    no ground truth at all: realised frequency mirrors mean_p by
    construction. This is the exact table MORNING_REPORT used to diagnose
    underconfidence — with answer_binary absent it measures nothing."""
    rows = [BatchResult(question_id=f"q{i}", status="scored",
                        predicted_probability=0.85 if i % 2 else 0.15,
                        brier=0.02, answer_binary=None)
            for i in range(10)]
    rep = build_report({r.question_id: r for r in rows})
    live = [(b["mean_p"], b["realised"]) for b in
            rep["calibration_overall"] if b["n"]]
    assert live == [(0.15, 0.0), (0.85, 1.0)]  # textbook-perfect, fabricated
    assert rep["verdict"].startswith("strongly better than chance")


def test_K1_sealed_rate_is_the_researchers_own_flag():
    """sealed_rate in the headline counts r.sealed, which _run_one copies
    from the researcher's self-reported run trace. No seal object is ever
    inspected."""
    r = BatchResult(question_id="q", status="scored",
                    predicted_probability=0.7, brier=0.09,
                    answer_binary=None, sealed=True)
    assert build_report({"q": r})["sealed_rate"] == 1.0


# ── K2. The routing store: no identity, no coverage, no class ─────────────

def test_K2_duplicate_question_records_both_count(tmp_path):
    """The same question_id can be recorded N times and all copies enter the
    aggregate: one easy question repeated 100x looks like 100 measurements.
    Nothing dedups on question_id, so volume substitutes for breadth."""
    s = ModelScoreStore(path=tmp_path / "s.jsonl")
    for i in range(100):
        s.record(role="r", model="spammer", task_class="tc",
                 question_id="same_q", brier=0.0)
    agg = ModelScoreStore.aggregate(s.records_for("r", "spammer"))
    assert agg["n"] == 100          # one question, counted as one hundred
    assert agg["mean_brier"] < 0.05  # shrunk toward heroic by pure duplication
    assert ModelScoreStore.basis_label(agg["n"]) == "measured"


def test_K2_cherry_picked_model_wins_routing(tmp_path):
    """H7.3 made real: model B answers only its 10 easy questions (brier
    .0025) and nulls the hard ones; model A answers all 20 honestly (.25).
    write_routing_scores drops B's nulls silently, so the store compares
    B's easy subset against A's full set — and Thompson sampling then picks
    B essentially always."""
    s = ModelScoreStore(path=tmp_path / "s.jsonl")
    for i in range(10):
        s.record(role="r", model="cherry", task_class="tc",
                 question_id=f"c{i}", brier=0.01)
    for i in range(30):
        s.record(role="r", model="honest", task_class="tc",
                 question_id=f"h{i}", brier=0.24)
    pol = ThompsonRoutingPolicy(store=s, rng=random.Random(1))
    cands = [CandidateModel(name="cherry", tier="t1", config_rank=1),
             CandidateModel(name="honest", tier="t2", config_rank=0)]
    wins = sum(pol.decide("r", cands).model == "cherry" for _ in range(500))
    assert wins >= 480  # the unrepresentative sample owns the role


def test_K2_decide_ignores_task_class(tmp_path):
    """Scores are keyed (role, model) — task_class is stored but never read
    by decide(). Measurements from one task class route every other class's
    calls under the same role, with basis='measured'."""
    s = ModelScoreStore(path=tmp_path / "s.jsonl")
    # mB measured ONLY on research_synthesis, where it is artificially good;
    # mA is the configured choice for decomposition, where mB was never tried.
    for i in range(30):
        s.record(role="pipeline", model="mB", task_class="research_synthesis",
                 question_id=f"s{i}", brier=0.01)
        s.record(role="pipeline", model="mA", task_class="research_synthesis",
                 question_id=f"s{i}'", brier=0.40)
    pol = ThompsonRoutingPolicy(store=s)
    cands = [CandidateModel(name="mA", tier="a", config_rank=0),
             CandidateModel(name="mB", tier="b", config_rank=1)]
    d = pol.decide("pipeline", cands)
    assert d.model == "mB"
    assert d.basis == "measured"   # cross-class extrapolation dressed as science


def test_K2_recency_rewrite_outweighs_measured_history(tmp_path):
    """The store is append-only ('history is never rewritten') — yet the
    policy's read-time exponential decay lets 15 fresh rows outvote 60
    measured ones: a model that collapsed can launder its record by behaving
    briefly. The aggregate shown for honesty still carries the lifetime
    mean, but the DECISION samples the rewritten posterior."""
    s = ModelScoreStore(path=tmp_path / "s.jsonl")
    for i in range(60):
        s.record(role="r", model="M", task_class="tc",
                 question_id=f"q{i}", brier=0.45)     # terrible era
    for i in range(15):
        s.record(role="r", model="M", task_class="tc",
                 question_id=f"g{i}", brier=0.02)     # brief "reform"
    pol = ThompsonRoutingPolicy(store=s)
    draws = [pol._sample_loss(s.records_for("r", "M")) for _ in range(3000)]
    # posterior centres BELOW the lifetime raw mean (~0.38) — recent spin won
    assert statistics.mean(draws) < 0.36


# ── K3/K4. The inheritance rule rewards direction-luck ────────────────────

def test_K4_coin_flip_descendants_lift_parent_past_SPECULATIVE():
    """40 descendants predicted at p=0.5+eps resolve hit/miss by pure chance;
    via the REAL path (resolved_claim_record), roughly half count as hits,
    Wilson LB on 20/40 ~= 0.35, and the ceiling lands near 0.63-0.75 — ABOVE
    the SPECULATIVE cap (0.55) the module promises as the floor for
    unproven parents, and into PROBABLE. Direction-only hits with
    chance-level Brier (~0.25 each) are full currency here; the H6 fix
    ('weight hits by extremity') was never applied."""
    rng = random.Random(9)
    recs = []
    for i in range(40):
        y = rng.random() < 0.5
        recs.append(resolved_claim_record(
            q(f"c{i}", y), Prediction(f"c{i}", 0.500001)))
    assert all(abs(r["brier"] - 0.25) < 0.01 for r in recs)  # pure chance
    ceiling = inherited_ceiling(recs)
    assert ceiling > 0.55   # broke the SPECULATIVE cap on coin flips


def test_K4_perfect_pinball_on_misses_inflates_ceiling():
    """pinball_score arrives inside plain dicts (normalize_records accepts
    any mapping). Forged near-zero pinballs on 35 MISSES convert a 12%
    track record into a 0.77 ceiling — the calibration term trusts a number
    the resolving side supplies about itself."""
    forged = [{"question_id": f"m{i}", "resolved_at": "2025-01-01",
               "outcome": "miss", "pinball_score": 0.0001,
               "best_source_class": "PRIMARY"} for i in range(35)]
    forged += [{"question_id": f"d{i}", "resolved_at": "2025-01-01",
                "outcome": "hit"} for i in range(5)]
    honest = [{"question_id": f"m{i}", "resolved_at": "2025-01-01",
               "outcome": "miss", "best_source_class": "PRIMARY"}
              for i in range(35)]
    honest += [{"question_id": f"d{i}", "resolved_at": "2025-01-01",
                "outcome": "hit"} for i in range(5)]
    assert inherited_ceiling(forged) - inherited_ceiling(honest) > 0.19


def test_K4_hits_with_terrible_calibration_still_full_support():
    """Every descendant 'hit' with pinball 0.49 (near-chance sharpness) still
    contributes full Wilson support AND its pinball only halves the calib
    term once. 40 such records reach the SECONDARY cap (0.75): the rule
    caps provenance but never asks whether the hits were informative."""
    recs = [{"question_id": f"h{i}", "resolved_at": "2025-01-01",
             "outcome": "hit", "pinball_score": 0.49} for i in range(40)]
    assert inherited_ceiling(recs) >= 0.75


# ── K5. Verdict and headline ignore coverage / trust self-reports ─────────

def test_K5_verdict_blesses_half_null_batch():
    """At exactly 50% nulls the majority-null guard (>0.5) does not fire and
    the headline reads 'strongly better than chance' from ONE scored row."""
    rows = {"ok": BatchResult(question_id="ok", status="scored",
                              predicted_probability=0.95,
                              answer_binary=True, brier=0.0025)}
    rows["n0"] = BatchResult(question_id="n0", status="null",
                             refusal_reason="x")
    rep = build_report(rows)
    assert rep["null_rate"] == 0.5
    assert rep["verdict"].startswith("strongly better than chance")


def test_K5_magnitude_zero_edge_counted_as_wrong():
    """Agreeing exactly with the market (p == market_implied) yields
    directional_edge = -0.0: the tie is scored as a WRONG directional bet
    even though edge_taken is zero and nothing was risked. Systematically
    deferring to the market is penalised in beat_market_rate while any
    epsilon of disagreement in the right direction is rewarded — the metric
    pushes the system off the market line even when the market is right."""
    m = magnitude_score(0.6, True, 0.6)
    assert m["edge_taken"] == 0.0
    assert m["directional_edge"] <= 0   # penalised for having no opinion
