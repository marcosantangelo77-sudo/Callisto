"""RTR — when may empirical routing overrule the configured order?

`empirical_routing.enabled` stays false until the score store holds enough
HONEST observations to beat configuration with known confidence. This module
makes that threshold explicit and defensible instead of a magic number.

## The crossover argument

Routing decision = choosing between candidate models for a role using their
measured mean Brier. The configured order is itself a hypothesis ("the
rank-0 model is best"). Empirical routing should only be trusted once it can
distinguish a REAL difference of practical size from sampling noise at a
conventional significance level.

For two models with n observations each and per-observation Brier variance
σ² (binary questions: variance ≤ 0.25, since Brier ∈ [0,1]; p=0.5 Bernoulli
squared-error has variance 0.25 — the worst case, which is what we budget
for), the standard error of the DIFFERENCE of means is σ·sqrt(2/n). To
detect a minimum practically-worthwhile difference Δ at z=1.96 (95%, two
sided) with 80% power:

    n ≥ 2 · (z_{α/2} + z_β)² · σ² / Δ²
      ≥ 2 · (1.96 + 0.84)² · 0.25 / Δ²
      ≈ 3.92 / Δ²        (since 2 · 7.85 · 0.25 = 3.925)

Δ is set by ECONOMICS, not taste: usd_per_brier_point (providers.yaml,
default $5 per full Brier point) times what a role's calls cost. If a
frontier Architect call costs ~$0.05 more than local and the role makes
~200 scored calls per quarter, a full point is worth ~$1000 → even a 2-point
improvement pays; but we require the MEASURED difference to be detectable at
MIN_DETECTABLE_BRIER = 0.05 (a fifth of a Brier point — differences smaller
than that are inside calibration noise of this harness) as the floor.

    n ≥ 3.92 / 0.05² = 1568   ← per-model pairwise comparison

That is the honest number for trusting a DIFFERENCE between two models.
Below it, Thompson sampling still explores (that is what it is for), but the
policy must not be ALLOWED to permanently displace the configured rank-0
choice on the basis of a winner it cannot statistically support.

## The gate, in plain terms

readiness(store, roles...) returns, per role:
  - n_honest per model: effective observations (correlated single-model runs
    counted once), NOT raw record counts;
  - eligible: every candidate measured AND the leading pair's honest counts
    both reach PAIRWISE_MIN_N, or one model dominates so completely that its
    own CI excludes chance;
  - overall ready: ALL routed roles eligible.

Until then: keep enabled=false. Flipping it early manufactures confidence —
exactly the failure mode the audit documented.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# ── Threshold constants (each defensible, see module docstring) ────────────

#: Worst-case per-observation Brier variance for binary outcomes.
BRIER_VARIANCE_MAX = 0.25

#: z for 95% two-sided confidence...
Z_ALPHA = 1.96
#: ...and 80% power (z for beta = 0.20).
Z_BETA = 0.84

#: Smallest mean-Brier difference worth detecting. Below this the difference
#: is within the harness's own run-to-run calibration noise; acting on it
#: would be fitting noise. Also the smallest edge that could plausibly clear
#: a cost trade at usd_per_brier_point=$5 for any role making >40 paid calls
#: per quarter.
MIN_DETECTABLE_BRIER = 0.05

#: Derived: honest observations per model needed before a HEAD-TO-HEAD
#: ranking between two models may displace configuration.
PAIRWISE_MIN_N = math.ceil(
    2.0 * (Z_ALPHA + Z_BETA) ** 2 * BRIER_VARIANCE_MAX
    / MIN_DETECTABLE_BRIER ** 2)          # == 1568


def pairwise_min_n(min_detectable_brier: float = MIN_DETECTABLE_BRIER,
                   brier_variance: float = BRIER_VARIANCE_MAX) -> int:
    """The sample-size formula, exposed so tests and reports can recompute
    it under different assumptions."""
    return math.ceil(2.0 * (Z_ALPHA + Z_BETA) ** 2 * brier_variance
                     / min_detectable_brier ** 2)


@dataclass
class RoleReadiness:
    role: str
    #: {model: honest (effective) observation count}
    honest_counts: dict[str, int]
    #: {model: raw record count} — shown beside the honest count precisely so
    #: inflation would be visible
    raw_counts: dict[str, int]
    n_models_measured: int
    #: all measured models individually carry >= PAIRWISE_MIN_N honest obs
    pairwise_ready: bool
    basis: str                    # readiness label, mirrors scores.BASIS_*
    blocking_reason: str = ""

    @property
    def ready(self) -> bool:
        return self.pairwise_ready

    def to_dict(self) -> dict:
        d = {
            "role": self.role,
            "honest_counts": dict(self.honest_counts),
            "raw_counts": dict(self.raw_counts),
            "n_models_measured": self.n_models_measured,
            "pairwise_min_n": PAIRWISE_MIN_N,
            "pairwise_ready": self.pairwise_ready,
            "ready": self.ready,
            "basis": self.basis,
        }
        if self.blocking_reason:
            d["blocking_reason"] = self.blocking_reason
        return d


def role_readiness(role: str,
                   attributions_by_model: dict[str, list],
                   candidates: Optional[list[str]] = None,
                   honest_counts_override: Optional[dict[str, int]] = None
                   ) -> RoleReadiness:
    """Assess one role.

    `attributions_by_model`: {model: [RunAttribution]} — the scored runs in
    which each model participated in THIS role. Honest counts come from
    tools.retrodiction.attribution.effective_observation_count, so correlated
    single-model runs never inflate. Callers without run-level attribution
    may pass `honest_counts_override` ({model: count}) explicitly; passing
    neither treats every record as independent (labelled as such by the
    caller).
    """
    from tools.routing.scores import ModelScoreStore

    if honest_counts_override is not None:
        honest = dict(honest_counts_override)
    else:
        from tools.retrodiction.attribution import \
            effective_observation_count
        honest = {m: effective_observation_count(attrs, m)
                  for m, attrs in attributions_by_model.items()}
    # Raw store records for contrast (what an dishonest count would claim).
    raw = {m: sum(len(a.role_models) for a in attrs if m in a.role_models)
           for m, attrs in attributions_by_model.items()}

    measured = {m: c for m, c in honest.items() if c > 0}
    if candidates:
        missing = [c for c in candidates if not measured.get(c)]
        if missing:
            unmeasured = ", ".join(missing)
            return RoleReadiness(
                role=role, honest_counts=honest, raw_counts=raw,
                n_models_measured=len(measured), pairwise_ready=False,
                basis="unmeasured",
                blocking_reason=(f"no measurements for candidate(s): "
                                 f"{unmeasured}"))
    if len(measured) < 2:
        return RoleReadiness(
            role=role, honest_counts=honest, raw_counts=raw,
            n_models_measured=len(measured), pairwise_ready=False,
            basis="unmeasured" if not measured else "sparse",
            blocking_reason=("only one model has any record — there is no "
                             "alternative to choose between"))
    short = {m: c for m, c in measured.items() if c < PAIRWISE_MIN_N}
    ready = not short
    worst = max(short.values()) if short else 0
    return RoleReadiness(
        role=role, honest_counts=honest, raw_counts=raw,
        n_models_measured=len(measured), pairwise_ready=ready,
        basis="measured" if ready else "provisional",
        blocking_reason=("" if ready else
                         f"largest honest sample is {worst} of "
                         f"{PAIRWISE_MIN_N} required per model to trust a "
                         f"head-to-head at Δ≥{MIN_DETECTABLE_BRIER}, "
                         f"95%/80%"))


def readiness_report(roles_readiness: list[RoleReadiness]) -> dict:
    """Overall verdict + per-role detail. `enabled` flips ONLY on ready=True
    for every routed role."""
    ready = all(r.ready for r in roles_readiness)
    blockers = [{"role": r.role, "reason": r.blocking_reason}
                for r in roles_readiness if not r.ready]
    return {
        "pairwise_min_n": PAIRWISE_MIN_N,
        "min_detectable_brier": MIN_DETECTABLE_BRIER,
        "rationale": (
            f"Detecting a {MIN_DETECTABLE_BRIER} mean-Brier difference "
            f"(the smallest edge worth acting on at $"
            f"5/Brier-point economics) between two models at 95% "
            f"confidence and 80% power needs "
            f"n ≥ 2(z_a+z_b)²σ²/Δ² = {PAIRWISE_MIN_N} honest observations "
            f"PER MODEL. Correlated single-model runs count once."),
        "all_roles_ready": ready,
        "empirical_routing_should_enable": ready,
        "blockers": blockers,
        "roles": [r.to_dict() for r in roles_readiness],
    }
