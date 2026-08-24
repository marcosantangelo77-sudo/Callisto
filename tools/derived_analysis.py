"""Derived-analysis loop — structured extraction → expected relationship →
deviation → QUESTION.

The mechanism is domain-generic. A domain supplies Relationships; this module
compares each relationship's observed values against a normal range DERIVED
FROM THE ENTITY'S OWN HISTORY (median ± k·MAD of that entity's own series,
never a textbook constant), flags deviations, and renders each deviation as a
question string for the research pipeline.

HARD RULES (enforced structurally, not by convention):

1. An Anomaly is a QUESTION, never a finding. `Anomaly` carries no confidence
   field and cannot be promoted to any conclusion type — its only outputs are
   `question()` text and evidence dicts.
2. No confidence score is computed, raised, or lowered anywhere here.
3. The loop cannot run away: `MAX_QUESTIONS_PER_EXTRACTION` bounds how many
   questions one extraction may generate. `select_for_emission` truncates
   deterministically (largest |deviation| first, then oldest period label),
   and every emitted batch reports what was dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

# Hard bound: one extraction may raise at most this many questions.
MAX_QUESTIONS_PER_EXTRACTION = 5

# Normal range width in robust sigma units (scaled MAD). Wide on purpose: an
# anomaly must be a genuine deviation from THIS entity's own behaviour, not noise.
MAD_SIGMA = 3.0

# Minimum observed periods before a range is defensible. With fewer, the
# entity's own history says nothing and we stay silent.
MIN_PERIODS_FOR_RANGE = 3


@dataclass(frozen=True)
class Relationship:
    """One expected relationship between extracted quantities.

    `compute(series)` receives {label: {period: value}} and returns
    {period: float} — one observation per period where inputs exist.
    """

    key: str                       # stable id, e.g. "cash_conversion"
    description: str               # human-readable expectation
    unit: str                      # "ratio", "percent", ...
    compute: Callable[[dict], dict[str, float]]

    def observe(self, series: dict[str, dict[str, Optional[float]]]) -> dict[str, float]:
        try:
            return {
                p: float(v) for p, v in self.compute(series).items()
                if v is not None and v == v  # drop None and NaN
            }
        except Exception:
            return {}  # a broken relationship yields silence, not a fake range


@dataclass(frozen=True)
class Anomaly:
    """A deviation rendered ONLY as a question. No confidence field exists."""

    relationship_key: str
    entity: str
    period: str
    observed: float
    expected_low: float
    expected_high: float
    history_periods: tuple[str, ...]
    unit: str
    description: str

    @property
    def magnitude(self) -> float:
        """How far outside the range, in units of the range's own scale."""
        span = self.expected_high - self.expected_low
        if span <= 0:
            return abs(self.observed - self.expected_high)
        if self.observed > self.expected_high:
            return (self.observed - self.expected_high) / span
        return (self.expected_low - self.observed) / span

    def evidence(self) -> dict:
        """Provenance-grade evidence for the researcher. Facts only."""
        return {
            "relationship": self.relationship_key,
            "expectation": self.description,
            "entity": self.entity,
            "period": self.period,
            "observed": round(self.observed, 6),
            "normal_range": [round(self.expected_low, 6),
                             round(self.expected_high, 6)],
            "range_basis_periods": list(self.history_periods),
            "magnitude_in_range_units": round(self.magnitude, 3),
            "unit": self.unit,
        }

    def question(self) -> str:
        """The anomaly AS a question. This is its entire interface to the
        research pipeline — it seeds inquiry, never a conclusion."""
        lo, hi = self.expected_low, self.expected_high
        basis = ", ".join(self.history_periods)
        return (
            f"Investigate: why is {self.entity}'s {self.relationship_key} "
            f"({self.description}) {self.observed:.4g} {self.unit} in "
            f"{self.period}, outside the range [{lo:.4g}, {hi:.4g}] its own "
            f"history ({basis}) implies? Magnitude ~{self.magnitude:.1f} "
            f"range-widths. Evidence: {self.evidence()}"
        )


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _mad_sigma_range(observations: dict[str, float]) -> Optional[tuple[float, float, tuple[str, ...]]]:
    """Normal range from the entity's OWN history: median ± 3·(1.4826·MAD).

    Returns (low, high, periods_used) or None when history is too thin or too
    degenerate (zero dispersion makes any range meaningless).
    """
    periods = sorted(observations)
    if len(periods) < MIN_PERIODS_FOR_RANGE:
        return None
    vals = [observations[p] for p in periods]
    med = _median(vals)
    mad = _median([abs(v - med) for v in vals])
    sigma = 1.4826 * mad
    if sigma <= 0:
        # Zero dispersion: fall back to a tiny epsilon band around the median
        # so an actual move still trips the flag instead of dividing by zero.
        scale = max(abs(med) * 1e-6, 1e-9)
        sigma = scale
    return med - MAD_SIGMA * sigma, med + MAD_SIGMA * sigma, tuple(periods)


def detect_anomalies(
    relationships: Sequence[Relationship],
    series: dict[str, dict[str, Optional[float]]],
    *,
    entity: str,
    focus: Optional[Sequence[str]] = None,
) -> list[Anomaly]:
    """Flag deviations of the FOCUS period(s) from the entity's own norm.

    Only periods named in `focus` are flagged (the extraction's newest data);
    older periods contribute to the baseline. When `focus` is omitted, the
    lexicographically-last period of each relationship is the focus.
    """
    anomalies: list[Anomaly] = []
    for rel in relationships:
        obs = rel.observe(series)
        if not obs:
            continue
        focus_periods = sorted(set(focus)) if focus else [sorted(obs)[-1]]
        baseline = {p: v for p, v in obs.items() if p not in set(focus_periods)}
        rng = _mad_sigma_range(baseline)
        if rng is None:
            continue
        low, high, used = rng
        for p in focus_periods:
            if p not in obs:
                continue
            v = obs[p]
            if v < low or v > high:
                anomalies.append(Anomaly(
                    relationship_key=rel.key,
                    entity=entity,
                    period=p,
                    observed=v,
                    expected_low=low,
                    expected_high=high,
                    history_periods=used,
                    unit=rel.unit,
                    description=rel.description,
                ))
    return anomalies


def select_for_emission(
    anomalies: Sequence[Anomaly],
    *,
    limit: int = MAX_QUESTIONS_PER_EXTRACTION,
) -> tuple[list[Anomaly], int]:
    """Deterministically pick which anomalies become questions.

    Returns (selected, dropped_count). Largest magnitude first; ties broken by
    (relationship key, period) for reproducibility. The bound is absolute:
    one extraction can never flood the pipeline.
    """
    ordered = sorted(anomalies, key=lambda a: (-a.magnitude, a.relationship_key, a.period))
    return list(ordered[:limit]), max(0, len(ordered) - limit)


async def emit_questions(
    anomalies: Sequence[Anomaly],
    queue: Any,
    *,
    limit: int = MAX_QUESTIONS_PER_EXTRACTION,
    priority: int = 3,
) -> dict:
    """Submit selected anomalies as questions via the existing pipeline.

    `queue` needs only `submit_task(query, priority)`. Questions enter the
    normal research loop with normal confidence/provenance machinery — the
    anomaly itself contributes nothing but the question text.
    """
    selected, dropped = select_for_emission(anomalies, limit=limit)
    ids = []
    for a in selected:
        tid = await queue.submit_task(a.question(), priority=priority)
        ids.append({"task_id": tid, "relationship": a.relationship_key,
                    "period": a.period})
    return {"submitted": len(ids), "dropped_over_bound": dropped,
            "bound": limit, "tasks": ids}
