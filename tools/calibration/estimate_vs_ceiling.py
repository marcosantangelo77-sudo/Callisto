"""ESTIMATE vs CEILING — the separation prototype.

THE FINDING THIS ENCODES: a ceiling answers "how much certainty may I claim";
an estimate answers "how likely is this". The pipeline collapses them. A model
estimate of 0.8 under a 0.55 ceiling reports 0.55; an estimate of 0.3 under
the same ceiling reports 0.3. Estimates are only ever pulled DOWN, never
centred, so across many claims the reported number drifts low — measured as a
27-point gap (predicted 0.33, realised 0.60, n=5).

THE PROTOTYPE: carry both numbers.

  @dataclass
  class TwoNumberPrediction:
      estimate: float   # P(outcome) — what calibration should be scored on
      ceiling: float    # how much certainty provenance permits claiming

  def sealable_confidence(two) -> float:
      return min(two.estimate, two.ceiling)     # UNCHANGED guard

The invariant stands: min() still bounds anything stored/sealed/bet on. What
changes is only which number feeds the CALIBRATION SCORE and the routing
loop's learning signal. Nothing anywhere raises a confidence.

rescore(): given recorded (raw_estimate, ceiling, outcome) triples, compare
Brier + calibration bias scored on the collapsed reported probability versus
the separated estimate.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TwoNumberPrediction:
    question_id: str
    estimate: float          # the model's actual probability estimate
    ceiling: float           # max certainty provenance/review permits
    leans_yes: bool = True

    def __post_init__(self):
        for name in ("estimate", "ceiling"):
            v = getattr(self, name)
            if not (0.0 <= float(v) <= 1.0):
                raise ValueError(f"{name} must be in [0,1]")

    @property
    def reported(self) -> float:
        """What today's pipeline reports — the collapse. UNCHANGED."""
        p = 0.5 + (min(self.estimate, self.ceiling) / 2.0)
        return p if self.leans_yes else 1.0 - p

    @property
    def estimate_probability(self) -> float:
        p = 0.5 + (self.estimate / 2.0)
        return p if self.leans_yes else 1.0 - p


def _brier(p: float, y: bool) -> float:
    return (p - (1.0 if y else 0.0)) ** 2


def rescore(records: list[dict]) -> dict:
    """records: [{question_id, raw_estimate, ceiling, leans_yes, outcome}]

    Returns Brier and mean bias (realised − predicted) under both readings.
    Positive bias = underconfident. No record is modified; this is a
    counterfactual scoring of already-recorded numbers.
    """
    if not records:
        raise ValueError("no records")
    rep_b, est_b, rep_bias, est_bias = [], [], [], []
    for r in records:
        t = TwoNumberPrediction(
            question_id=r["question_id"],
            estimate=float(r["raw_estimate"]),
            ceiling=float(r.get("ceiling", 1.0)),
            leans_yes=bool(r.get("leans_yes", True)))
        y = bool(r["outcome"])
        rep_b.append(_brier(t.reported, y))
        est_b.append(_brier(t.estimate_probability, y))
        rep_bias.append((1.0 if y else 0.0) - t.reported)
        est_bias.append((1.0 if y else 0.0) - t.estimate_probability)
    n = len(records)

    def _mean(xs):
        return round(sum(xs) / len(xs), 6)

    return {
        "n": n,
        "reported": {"brier": _mean(rep_b),
                     "mean_underconfidence_bias": _mean(rep_bias)},
        "estimate_separated": {"brier": _mean(est_b),
                               "mean_underconfidence_bias": _mean(est_bias)},
        "brier_improvement": round(_mean(rep_b) - _mean(est_b), 6),
        "note": ("counterfactual rescore of recorded numbers; no stored "
                 "confidence was changed and no mechanism raised anything"),
    }


def decompose_observed(observed_p: float, ceiling: float,
                       leans_yes: bool = True) -> TwoNumberPrediction:
    """Invert one collapsed report back into (minimum consistent estimate,
    ceiling). The observed reported probability is a LOWER BOUND on the true
    estimate whenever the ceiling bound; this recovers that floor."""
    conf = (2.0 * (observed_p if leans_yes else 1.0 - observed_p)) - 1.0
    reported_conf = min(conf, ceiling)
    # estimate consistent with the observation: exactly `conf` if unbound,
    # otherwise unknown but >= ceiling — we carry the floor honestly.
    return TwoNumberPrediction(question_id="", estimate=max(conf, 0.0),
                               ceiling=ceiling, leans_yes=leans_yes)
