"""R5 — Fermi decomposition with uncertainty propagation.

Break a quantity into estimable factors, attach a distribution to each,
propagate uncertainty to the answer. A point estimate with false precision is
worse than a range that admits what it does not know.

Design constraints (BUILD_MANDATE properties 3 and 4):

- **Auditable factor by factor.** Every factor carries its name, distribution,
  parameters, source, and note; the result reports mean, median, std, and the
  p05-p95 band of the propagated answer plus per-factor sensitivity ranked by
  contribution to variance. The 'verifiable evidence' requirement is
  satisfied structurally — the output IS a full inspectable trace — and emits
  into the artifact layer as a live-formula workbook with every assumption
  labelled.

- **Domain-general.** Factors multiply or add; distributions are named
  parametric forms. Nothing knows whether the question was about revenue,
  protein-folding yield, or container ships.

- **Correlations matter.** Factors can be correlated via a Gaussian copula on
  rank-normalised draws, so "revenue and costs both scale with GDP"
  propagates honestly instead of understating joint variance. Marginals are
  preserved exactly (permutation-based).

Determinism: seeded numpy Generator — same seed, same answer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np


def _sample_pert(rng: np.random.Generator, p: dict) -> np.ndarray:
    """Modified PERT: low, mode, high with optional shape lambda."""
    lo, mode, hi = p["low"], p["mode"], p["high"]
    lam = p.get("lambda", 4.0)
    if not lo < mode < hi:
        raise ValueError("PERT requires low < mode < high")
    alpha = 1 + lam * (mode - lo) / (hi - lo)
    beta = 1 + lam * (hi - mode) / (hi - lo)
    return rng.beta(alpha, beta, size=p["_n"]) * (hi - lo) + lo


# name -> sampler(rng, params_with__n) -> ndarray
DISTRIBUTIONS = {
    "lognormal": lambda rng, p: rng.lognormal(
        mean=math.log(p["median"]), sigma=p["sigma"], size=p["_n"]
    ),
    "normal": lambda rng, p: rng.normal(loc=p["mean"], scale=p["std"], size=p["_n"]),
    "uniform": lambda rng, p: rng.uniform(low=p["low"], high=p["high"], size=p["_n"]),
    "triangular": lambda rng, p: rng.triangular(
        left=p["low"], mode=p["mode"], right=p["high"], size=p["_n"]
    ),
    "bernoulli": lambda rng, p: rng.binomial(n=1, p=p["p"], size=p["_n"]).astype(float),
    "pert": _sample_pert,
}


@dataclass
class Factor:
    """One estimable factor in the decomposition."""

    name: str
    distribution: str                 # key into DISTRIBUTIONS
    params: dict                      # e.g. {"median": 5e9, "sigma": 0.6}
    combine: str = "multiply"         # how this factor joins the running total
    source: str = ""                  # where the estimate comes from
    note: str = ""

    def validate(self) -> None:
        if self.distribution not in DISTRIBUTIONS:
            raise ValueError(
                f"unknown distribution {self.distribution!r}; "
                f"known: {sorted(DISTRIBUTIONS)}"
            )
        if self.combine not in ("multiply", "add"):
            raise ValueError(f"combine must be 'multiply' or 'add', got {self.combine!r}")


@dataclass
class Correlation:
    """Pairwise correlation between two factors (by name). rho in [-0.99, 0.99]."""
    a: str
    b: str
    rho: float


@dataclass
class FermiResult:
    """The propagated answer plus the complete audit trail."""

    quantity: str
    unit: str
    n_samples: int
    seed: int
    factors: list[dict]                       # per-factor audit rows
    mean: float
    median: float
    std: float
    p05: float
    p50: float
    p95: float
    sensitivity: list[dict]                   # ranked contribution to variance
    correlations_applied: list[dict]
    samples: Optional[np.ndarray] = field(default=None, repr=False)

    def summary(self) -> dict:
        return {
            "quantity": self.quantity,
            "unit": self.unit,
            "mean": self.mean,
            "median": self.median,
            "std": self.std,
            "p05_p95": [self.p05, self.p95],
            "n_samples": self.n_samples,
            "seed": self.seed,
            "factors": self.factors,
            "sensitivity": self.sensitivity,
            "correlations": self.correlations_applied,
        }


# ---------------------------------------------------------------------------
# Core propagation
# ---------------------------------------------------------------------------

def propagate(
    quantity: str,
    factors: list[Factor],
    *,
    unit: str = "",
    correlations: Optional[list[Correlation]] = None,
    n_samples: int = 20_000,
    seed: int = 42,
    transform: Optional[Callable[[np.ndarray], np.ndarray]] = None,
) -> FermiResult:
    """Monte-Carlo propagate the factors to the answer.

    combine='multiply' factors accumulate multiplicatively (Fermi's classic
    chain); combine='add' factors are summed into the total after the
    multiplicative chain. `transform` optionally post-processes the array
    (e.g. subtract a fixed cost).
    """
    if not factors:
        raise ValueError("a Fermi decomposition needs at least one factor")
    for f in factors:
        f.validate()
    names = [f.name for f in factors]
    if len(set(names)) != len(names):
        raise ValueError("factor names must be unique")

    rng = np.random.default_rng(seed)

    raw_draws: dict[str, np.ndarray] = {}
    for f in factors:
        p = {**f.params, "_n": n_samples}
        raw_draws[f.name] = np.asarray(DISTRIBUTIONS[f.distribution](rng, p), dtype=float)

    # Gaussian-copula-style correlation on rank-normalised draws. Mixing is
    # done in normal space, then draws are PERMUTED to match the mixed ranks,
    # so each marginal distribution is preserved exactly.
    normals = _to_normals(raw_draws, n_samples)
    applied_corr: list[dict] = []
    for c in correlations or []:
        if c.a not in normals or c.b not in normals:
            raise ValueError(f"correlation references unknown factor(s): {c.a}, {c.b}")
        if not -0.99 <= c.rho <= 0.99 or abs(c.rho) < 1e-12:
            raise ValueError("rho must be nonzero within [-0.99, 0.99]")
        shared = normals[c.a]
        indep = normals[c.b]
        mixed = math.sqrt(c.rho) * shared + math.sqrt(1.0 - c.rho * c.rho) * indep
        order_target = np.argsort(mixed)
        order_current = np.argsort(normals[c.b])
        new_draws = np.empty_like(raw_draws[c.b])
        new_draws[order_target] = raw_draws[c.b][order_current]
        raw_draws[c.b] = new_draws
        normals[c.b] = mixed[order_target]
        applied_corr.append({"a": c.a, "b": c.b, "rho": round(c.rho, 4)})

    total: Optional[np.ndarray] = None
    for f in factors:
        d = raw_draws[f.name]
        if total is None:
            total = d.copy()
        elif f.combine == "multiply":
            total = total * d
        else:
            total = total + d
    assert total is not None
    if transform is not None:
        total = transform(total)

    # Sensitivity: correlation of each factor with the result, in log space
    # when both are strictly positive (multiplicative chains compare
    # elasticities honestly there).
    log_total = np.log(total) if np.all(total > 0) else total
    sens = []
    for f in factors:
        d = raw_draws[f.name]
        x = np.log(d) if np.all(d > 0) else d
        r = float(np.corrcoef(x, log_total)[0, 1])
        sens.append({"factor": f.name, "corr_with_result": round(r, 4)})
    sens.sort(key=lambda s: -abs(s["corr_with_result"]))

    factor_rows = [
        {
            "name": f.name,
            "distribution": f.distribution,
            "params": dict(f.params),
            "combine": f.combine,
            "source": f.source,
            "note": f.note,
            "sample_median": float(np.median(raw_draws[f.name])),
        }
        for f in factors
    ]

    return FermiResult(
        quantity=quantity,
        unit=unit,
        n_samples=n_samples,
        seed=seed,
        factors=factor_rows,
        mean=float(np.mean(total)),
        median=float(np.median(total)),
        std=float(np.std(total)),
        p05=float(np.percentile(total, 5)),
        p50=float(np.percentile(total, 50)),
        p95=float(np.percentile(total, 95)),
        sensitivity=sens,
        correlations_applied=applied_corr,
        samples=total,
    )


def _to_normals(raw_draws: dict, n: int) -> dict:
    out = {}
    try:
        from scipy.stats import norm
        ppf = norm.ppf
    except ImportError:
        ppf = np.vectorize(_bsm_ppf)
    for name, d in raw_draws.items():
        ranks = np.argsort(np.argsort(d))
        u = (ranks + 0.5) / n
        out[name] = ppf(u)
    return out


def _bsm_ppf(u: float) -> float:
    """Beasley-Springer-Moro inverse normal CDF (single value)."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425

    def tail(q):
        qq = math.sqrt(-2 * math.log(q))
        return (((((c[0]*qq+c[1])*qq+c[2])*qq+c[3])*qq+c[4])*qq+c[5]) / \
               ((((d[0]*qq+d[1])*qq+d[2])*qq+d[3])*qq+1)

    def central(q):
        qq = q - 0.5
        r = qq*qq
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*qq / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)

    if u < plow:
        return tail(u)
    if u > phigh:
        return -tail(1 - u)
    return central(u)


# ---------------------------------------------------------------------------
# Artifact emission — the estimate becomes a live-formula sheet
# ---------------------------------------------------------------------------

def emit_workbook(result: FermiResult, store=None, name: str = "") -> dict:
    """Emit the Fermi estimate into tools/charts.store_workbook.

    Assumptions sheet: every factor with its distribution, parameters,
    source, and note — labelled, visible, torturable. ModelLive: a live
    formula chaining Assumptions cells into the product/sum so changing any
    assumption recomputes inside Excel. Scenarios: p05/p50/p95 bands recorded
    from the Monte-Carlo run.
    """
    from tools.charts import store_workbook

    assumptions = []
    cell_of_factor: dict[str, str] = {}
    for i, fr in enumerate(result.factors):
        cell = f"B{i + 2}"
        cell_of_factor[fr["name"]] = cell
        assumptions.append({
            "name": fr["name"],
            "value": fr["sample_median"],
            "unit": "",
            "source": fr["source"] or fr["distribution"],
            "note": f"{fr['distribution']}({fr['params']}) "
                    f"[{fr['combine']}] {fr['note']}".strip(),
        })

    mult_cells = [cell_of_factor[r["factor"]] for r in []]
    mult_cells = []
    sum_cells = []
    for fr in result.factors:
        c = cell_of_factor[fr["name"]]
        if fr["combine"] == "multiply":
            mult_cells.append(c)
        else:
            sum_cells.append(c)

    formula_parts = []
    if mult_cells:
        formula_parts.append("*".join(mult_cells))
    if sum_cells:
        formula_parts.append("(" + "+".join(sum_cells) + ")")
    formula = "*".join(formula_parts)

    model = [{
        "cell": "B1",
        "formula": formula,
        "label": f"{result.quantity} ({result.unit})",
    }]

    scenarios = [
        {"name": "P05", "overrides": {"_band": result.p05}},
        {"name": "Median", "overrides": {"_band": result.p50}},
        {"name": "P95", "overrides": {"_band": result.p95}},
    ]

    spec = {
        "assumptions": assumptions,
        "data": {},
        "model": model,
        "scenarios": scenarios,
        "code": f"fermi.propagate(quantity={result.quantity!r}, seed={result.seed})",
        "notes": (
            f"Fermi decomposition, {result.n_samples} Monte-Carlo samples, "
            f"seed={result.seed}. Bands: p05={result.p05:.6g}, "
            f"median={result.p50:.6g}, p95={result.p95:.6g} {result.unit}".strip()
            + ". Assumption values are sample medians; the live B1 formula is "
              "the deterministic point model behind the Monte Carlo."
        ),
    }
    wb = store_workbook(spec, store=store, name=name or f"fermi_{result.quantity[:30]}")
    return {**wb, "spec": spec, "summary": result.summary()}
