"""ESTIMATE vs CEILING — the separation prototype. Design, not a refactor.

THE PROBLEM (measured, data/retro_batch/report_smoke5.json): the pipeline
predicted 0.33 where reality was 0.60 (n=5). One number is doing two jobs:

    ESTIMATE — how likely is this claim to be true?
    CEILING  — how much certainty are we ENTITLED to claim, given the
               provenance, the critique, and what has actually resolved?

Every downward mechanism in this architecture operates on the single carried
number, so every ceiling reduction also destroys estimate information
(engine.py:443-466 clamps; engine.py:656-681 clamps again and subtracts;
retro.py:97-99 maps the survivor onto the binary). Estimates are only ever
pulled down, never centred.

THE PROTOTYPE: carry both numbers through one explicit type.

    @dataclass
    class EstimateCeiling:
        estimate: float   # P(outcome | evidence) — the model's actual belief
        ceiling: float    # max certainty provenance/review permits claiming

Invariants enforced here:
  I1  0 <= estimate <= 1 and 0 <= ceiling <= 1.
  I2  sealable() == min(estimate, ceiling) — the reported/sealed/stored/bet
      number is bounded by BOTH. Nothing about sealing changes.
  I3  NO method on this type can increase either field above its value at
      entry except `with_estimate`, which takes the new estimate from an
      EXPLICIT caller argument and still clamps to the unchanged ceiling.
      The anti-inflation guarantee is structural: ceilings only fall.
  I4  floor_conf-style quantisation rounds DOWN only (agp.thresholds).

Consumers:
  - the SEAL covers the conclusion + sealable(); the estimate rides beside it
    as metadata (recorded, never authoritative for action).
  - CALIBRATION scores the estimate (see findings/design_estimate_ceiling.md §3).
  - POSITION SIZING uses the ceiling-adjusted number (§4): edge/Kelly read
    sealable(), because a bet is an entitlement claim against money.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def floor_conf(x: float, places: int = 2) -> float:
    """Quantise DOWNWARD only. Local copy of agp.thresholds.floor_conf so
    this module stays dependency-light for prototype adoption; values and
    semantics identical."""
    f = 10 ** places
    return math.floor(float(x) * f) / f


@dataclass(frozen=True)
class EstimateCeiling:
    """A prediction that separates what we believe from what we may claim."""

    estimate: float
    ceiling: float

    def __post_init__(self):
        e = float(self.estimate)
        c = float(self.ceiling)
        if not (0.0 <= e <= 1.0):
            raise ValueError(f"estimate must be in [0,1], got {e}")
        if not (0.0 <= c <= 1.0):
            raise ValueError(f"ceiling must be in [0,1], got {c}")
        object.__setattr__(self, "estimate", e)
        object.__setattr__(self, "ceiling", c)

    # ── the collapse point, made explicit ────────────────────────────────

    def sealable(self) -> float:
        """The number that may be sealed, stored, and acted on today.

        This IS the current pipeline's behaviour (every clamp is a min()),
        preserved exactly. The split changes nothing here by construction.
        """
        return floor_conf(min(self.estimate, self.ceiling))

    def to_binary_probability(self, leans_yes: bool = True) -> float:
        """Map onto a question's binary exactly as tools/pipeline/retro.py:99
        does: P(True) = 0.5 ± conf/2. Used on sealable(), never on the raw
        estimate, when producing an actionable probability."""
        p = 0.5 + self.sealable() / 2.0
        return p if leans_yes else 1.0 - p

    # ── the separation ───────────────────────────────────────────────────

    def with_ceiling(self, ceiling: float) -> "EstimateCeiling":
        """Apply one downward mechanism. The ceiling may only FALL or stay;
        raising it raises ValueError. The estimate passes THROUGH untouched —
        that is the entire point of the split: a provenance clamp no longer
        destroys the belief it bounds."""
        c = float(ceiling)
        if c > self.ceiling * (1 + 1e-9) + 1e-9:   # tolerate fp drift only
            raise ValueError(
                f"ceiling may not rise: {self.ceiling} -> {c}")
        return EstimateCeiling(estimate=self.estimate, ceiling=max(0.0, c))

    def with_estimate(self, estimate: float) -> "EstimateCeiling":
        """Revise the estimate from NEW EVIDENCE or a NEW model call — an
        explicit human/caller decision, never an internal mechanism. Still
        clamped by the (unchanged) ceiling at seal time, so even this path
        cannot raise the REPORTED confidence above entitlement."""
        e = float(estimate)
        if not (0.0 <= e <= 1.0):
            raise ValueError(f"estimate must be in [0,1], got {e}")
        return EstimateCeiling(estimate=e, ceiling=self.ceiling)

    def apply_adversary_penalty(self, penalty: float) -> "EstimateCeiling":
        """Adversary verdicts (agp.adversary.apply_verdict semantics): lower
        the ceiling, never touch the estimate. There is no bonus path."""
        p = float(penalty)
        if p < 0:
            raise ValueError("adversary penalty must be non-negative: no bonus path")
        return self.with_ceiling(self.ceiling - p)

    # ── serialisation for the DB / seal payload ──────────────────────────

    def to_dict(self) -> dict:
        return {"estimate": round(self.estimate, 4),
                "ceiling": round(self.ceiling, 4),
                "sealable": self.sealable()}

    @classmethod
    def from_dict(cls, d: dict) -> "EstimateCeiling":
        return cls(estimate=float(d["estimate"]),
                   ceiling=float(d["ceiling"]))


# ── scoring ──────────────────────────────────────────────────────────────

def brier(p: float, outcome: bool) -> float:
    y = 1.0 if outcome else 0.0
    return (float(p) - y) ** 2


def rescore(records: list[dict]) -> dict:
    """Counterfactual comparison of calibration under collapse vs separation.

    records: [{question_id, estimate, ceiling, leans_yes, outcome}] — the
    ESTIMATE form, i.e. instrumented runs that kept the raw estimate
    (tools/calibration/instrument.py exists precisely because collapsed runs
    cannot be rescored honestly; see diagnose.py's honesty note).

    Reports Brier and mean bias (realised − predicted; positive =
    underconfident) for both readings of the SAME runs:
      collapsed  — P from sealable() via the retro mapping   [what happened]
      separated  — P from estimate via the same mapping      [counterfactual]

    Position sizing is NOT rescored here: by design it keeps reading
    sealable() either way (findings §4), so realised edge is identical in
    both columns. Separation is a measurement change, not a betting change.
    """
    if not records:
        raise ValueError("no records")

    col_b, col_bias, sep_b, sep_bias = [], [], [], []
    for r in records:
        ec = EstimateCeiling(estimate=float(r["estimate"]),
                             ceiling=float(r["ceiling"]))
        yes = bool(r.get("leans_yes", True))
        y = bool(r["outcome"])
        p_col = ec.to_binary_probability(yes)
        p_sep = 0.5 + ec.estimate / 2.0
        p_sep = p_sep if yes else 1.0 - p_sep
        col_b.append(brier(p_col, y))
        sep_b.append(brier(p_sep, y))
        col_bias.append((1.0 if y else 0.0) - p_col)
        sep_bias.append((1.0 if y else 0.0) - p_sep)

    n = len(records)
    mean = lambda xs: round(sum(xs) / len(xs), 6)  # noqa: E731

    return {
        "n": n,
        "collapsed": {"brier": mean(col_b),
                      "mean_underconfidence_bias": mean(col_bias)},
        "separated": {"brier": mean(sep_b),
                      "mean_underconfidence_bias": mean(sep_bias)},
        "brier_improvement": round(mean(col_b) - mean(sep_b), 6),
        "note": ("counterfactual rescore of recorded numbers; sealable() "
                 "unchanged; nothing was raised"),
    }
