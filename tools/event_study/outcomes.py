"""Outcome measurement: forward returns after events vs matched random dates.

t=0 at each proven event date. Forward log-returns on a FRED series
(CBBTCUSD, SP500, DGS10, ...) measured close-of t0 (or first available
observation at/after) to +1/+4/+12 weeks (first observation on/before the
horizon date). Controls: N random dates per event, matched to lie in the
same calendar span as the event set so bull/bear regimes are shared —
without this, "price rose after the event" is meaningless.

The output is a distribution report: n, median, IQR, and a sign-flip
permutation test of event medians against controls (same exchangeability
argument as tools/retrodiction/scoring.paired_significance, applied to
return differences because that module's API is Brier-specific). No
confidence score is raised anywhere; no verdict beyond "distinguishable
from noise" language.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field

from tools.sources.base import RestSource
from tools.sources.fred import FredAdapter, SPEC as SPEC_FRED

HORIZONS = {"w1": 7, "w4": 28, "w12": 84}   # days


@dataclass
class OutcomeSeries:
    """One FRED series parsed into {date: float} with gaps tolerated."""
    series_id: str
    obs: dict = field(default_factory=dict)     # date -> value

    @classmethod
    def load(cls, series_id: str, start: str, end: str,
             fred: FredAdapter = None) -> "OutcomeSeries":
        fr = fred or FredAdapter(RestSource(SPEC_FRED))
        data = fr.series_observations(series_id, start=start, end=end)
        obs = {}
        for o in data.get("observations", []):
            try:
                v = float(o["value"])
            except (TypeError, ValueError):
                continue    # '.' missing-value marker
            obs[dt.date.fromisoformat(o["date"])] = v
        return cls(series_id=series_id, obs=obs)

    def _value_at_or_after(self, d: dt.date, max_fwd_days: int = 7):
        for i in range(max_fwd_days + 1):
            v = self.obs.get(d + dt.timedelta(days=i))
            if v is not None and v > 0:
                return v
        return None

    def _value_at_or_before(self, d: dt.date, max_back_days: int = 7):
        for i in range(max_back_days + 1):
            v = self.obs.get(d - dt.timedelta(days=i))
            if v is not None and v > 0:
                return v
        return None

    def forward_return(self, t0: dt.date, days: int) -> float | None:
        """log(P(t0+days) / P(t0)); None when either endpoint is unavailable."""
        p0 = self._value_at_or_after(t0)
        if p0 is None:
            return None
        p1 = self._value_at_or_before(t0 + dt.timedelta(days=days))
        if p1 is None or p1 <= 0:
            return None
        import math
        return math.log(p1 / p0)


def measure_forward_returns(events, series: OutcomeSeries,
                            horizons: dict = HORIZONS) -> list:
    """[{label, event_date, w1, w4, w12}] — None where data missing."""
    rows = []
    for ev in events:
        row = {"label": ev.label, "event_date": ev.event_date}
        for hname, hd in horizons.items():
            row[hname] = series.forward_return(ev.event_date, hd)
        rows.append(row)
    return rows


def random_control_returns(events, series: OutcomeSeries,
                           n_per_event: int = 5,
                           seed: int = 7,
                           horizons: dict = HORIZONS) -> list:
    """Forward returns at random dates drawn from the same calendar span as
    the event set (regime-matched), excluding windows overlapping an event."""
    if not events:
        return []
    lo = min(e.event_date for e in events)
    hi = max(e.event_date for e in events)
    rng = random.Random(seed)
    event_dates = {e.event_date for e in events}
    out = []
    n_drawn = 0
    attempts = 0
    while n_drawn < n_per_event * len(events) and attempts < 200 * len(events):
        attempts += 1
        d = lo + dt.timedelta(days=rng.randrange((hi - lo).days + 1))
        # keep controls out of any event's ±min_gap neighbourhood
        if any(abs((d - ed).days) < 21 for ed in event_dates):
            continue
        row = {"label": f"control-{d.isoformat()}", "event_date": d}
        ok = False
        for hname, hd in horizons.items():
            r = series.forward_return(d, hd)
            row[hname] = r
            ok = ok or r is not None
        if not ok:
            continue
        out.append(row)
        n_drawn += 1
    return out


# ── distribution reporting ──────────────────────────────────────────────

def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    mid = n // 2
    return xs[mid] if n % 2 else 0.5 * (xs[mid - 1] + xs[mid])


def _quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    idx = q * (len(xs) - 1)
    lo_i, hi_i = int(idx), min(int(idx) + 1, len(xs) - 1)
    frac = idx - lo_i
    return xs[lo_i] * (1 - frac) + xs[hi_i] * frac


def dist_stats(values) -> dict:
    vals = [v for v in values if v is not None]
    n = len(vals)
    if not n:
        return {"n": 0, "median": None, "q25": None, "q75": None,
                "mean": None, "min": None, "max": None}
    return {
        "n": n,
        "median": _median(vals),
        "q25": _quantile(vals, 0.25),
        "q75": _quantile(vals, 0.75),
        "mean": sum(vals) / n,
        "min": min(vals),
        "max": max(vals),
    }


def sign_flip_test(event_vals, control_vals,
                   n_permutations: int = 10_000, seed: int = 0) -> dict:
    """Two-sided permutation test on the difference of medians.

    Under H0 (event-window returns and control-window returns are drawn from
    the same distribution) the group labels of the pooled differences are
    exchangeable; p = fraction of random regroupings whose |median gap|
    matches or beats the observed one. Same logic family as
    tools/retrodiction/scoring.paired_significance — reused philosophy, not
    new statistics.
    """
    ev = [v for v in event_vals if v is not None]
    ct = [v for v in control_vals if v is not None]
    if len(ev) < 3 or len(ct) < 3:
        return {"p_value": None, "reason":
                f"insufficient data (events n={len(ev)}, controls n={len(ct)})"}
    pooled = ev + ct
    ne = len(ev)
    observed = abs(_median(ev) - _median(ct))
    rng = random.Random(seed)
    extremes = 0
    for _ in range(n_permutations):
        rng.shuffle(pooled)
        g1, g2 = pooled[:ne], pooled[ne:]
        if abs(_median(g1) - _median(g2)) >= observed - 1e-12:
            extremes += 1
    return {"p_value": extremes / n_permutations,
            "delta_median": _median(ev) - _median(ct),
            "n_event": len(ev), "n_control": len(ct)}


def report(event_rows, control_rows, series_id: str) -> dict:
    """The product: distributions + noise comparison. NO verdict, NO signal,
    NO confidence score — just counts and spreads."""
    out = {"series": series_id, "horizons": {}}
    for h in HORIZONS:
        ev = [r[h] for r in event_rows]
        ct = [r[h] for r in control_rows]
        es = dist_stats(ev)
        cs = dist_stats(ct)
        test = sign_flip_test(ev, ct)
        out["horizons"][h] = {
            "events": es, "controls": cs, "sign_flip": test,
            # plain-language honest reading, computed not asserted:
            "reading": (
                f"n={es['n']}, median {_fmt(es['median'])}, "
                f"IQR [{_fmt(es['q25'])}, {_fmt(es['q75'])}] vs controls "
                f"median {_fmt(cs['median'])}; "
                + ("indistinguishable from random"
                   if test.get("p_value") is None or test["p_value"] >= 0.05
                   else f"distinguishable from random (p={test['p_value']:.3f})"))
        }
    out["n_events_requested"] = len(event_rows)
    return out


def _fmt(x):
    if x is None:
        return "n/a"
    return f"{100 * x:+.1f}%"
