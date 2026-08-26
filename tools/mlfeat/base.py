"""Shared plumbing for the ``tools.mlfeat`` feature-store package.

This module holds the pieces every feature builder needs:

  * ``FeatureVector`` — immutable ordered (names, values) container
  * read-only SQLite helpers (``_resolve_db_path`` / ``_open_ro``)
  * asof normalisation and small numeric/statistical helpers
  * static venue metadata (park factors, dome set, altitude table)

Everything here is strictly read-only with respect to any database, and
pure with respect to time: all history filtering is bounded by an
``asof_date`` cutoff so features are reproducible at train and inference
time.
"""
from __future__ import annotations

import os
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, Sequence, Union

import numpy as np


# ──────────────────────────────────────────────────────────────────────────
# FeatureVector container
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FeatureVector:
    """Ordered feature values + their names.

    Immutable by design so downstream batch-training code can't accidentally
    shuffle column order between rows.
    """

    names: tuple[str, ...]
    values: np.ndarray  # shape (len(names),), dtype float64
    target_stat_value: Optional[float] = None
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.values.shape != (len(self.names),):
            raise ValueError(
                f"FeatureVector shape mismatch: {len(self.names)} names, "
                f"{self.values.shape} values"
            )

    def as_dict(self) -> dict[str, float]:
        return {n: float(v) for n, v in zip(self.names, self.values)}


# ──────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────

def _resolve_db_path() -> str:
    return os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


def _open_ro(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a read-only SQLite connection. Callers SHOULD pass an existing
    connection when batching; this is a convenience for one-off calls."""
    path = db_path or _resolve_db_path()
    # URI mode + mode=ro protects us from accidentally mutating anything.
    conn = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


# ──────────────────────────────────────────────────────────────────────────
# Date / asof helpers
# ──────────────────────────────────────────────────────────────────────────

def _asof_date(asof_ts: Union[str, datetime, date]) -> date:
    """Normalise asof to a date — we filter history as ``< asof_date`` (strict)."""
    if isinstance(asof_ts, date) and not isinstance(asof_ts, datetime):
        return asof_ts
    if isinstance(asof_ts, datetime):
        return asof_ts.date()
    if isinstance(asof_ts, str):
        s = asof_ts.strip()
        # Accept ISO datetime or ISO date.
        try:
            if "T" in s or " " in s:
                s2 = s.replace("Z", "+00:00")
                return datetime.fromisoformat(s2).date()
            return date.fromisoformat(s[:10])
        except Exception:
            pass
    raise ValueError(f"Unparseable asof_ts: {asof_ts!r}")


def _safe_stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return float("nan")
    try:
        return float(statistics.stdev(xs))
    except statistics.StatisticsError:
        return float("nan")


def _trend_slope(xs: Sequence[float]) -> float:
    """Least-squares slope of ``xs`` against index 0..N-1. NaN if <2 points."""
    n = len(xs)
    if n < 2:
        return float("nan")
    arr = np.asarray(xs, dtype=float)
    if not np.isfinite(arr).any():
        return float("nan")
    idx = np.arange(n, dtype=float)
    mask = np.isfinite(arr)
    if mask.sum() < 2:
        return float("nan")
    try:
        slope = float(np.polyfit(idx[mask], arr[mask], 1)[0])
    except (np.linalg.LinAlgError, ValueError):
        return float("nan")
    return slope


def _mean(xs: Sequence[float]) -> float:
    if not xs:
        return float("nan")
    arr = np.asarray(xs, dtype=float)
    mask = np.isfinite(arr)
    if not mask.any():
        return float("nan")
    return float(arr[mask].mean())


# ──────────────────────────────────────────────────────────────────────────
# Static venue park factors (inlined mirror of data_collector.VENUE_METADATA)
# ──────────────────────────────────────────────────────────────────────────
#
# We intentionally mirror the park-factor subset of VENUE_METADATA rather
# than importing from tools.data_collector — the data_collector module is in
# the DO-NOT-TOUCH list, importing it pulls in httpx + credentials on every
# feature extraction. These are static numbers that rarely change.
_PARK_FACTORS: dict[str, float] = {
    "Coors Field": 1.35,
    "Great American Ball Park": 1.13,
    "Yankee Stadium": 1.11,
    "Fenway Park": 1.07,
    "American Family Field": 1.05,
    "Wrigley Field": 1.05,
    "Minute Maid Park": 1.04,
    "Chase Field": 1.04,
    "Rogers Centre": 1.00,
    "Globe Life Field": 0.98,
    "Dodger Stadium": 0.96,
    "T-Mobile Park": 0.93,
    "Petco Park": 0.90,
    "Tropicana Field": 0.90,
    "loanDepot park": 0.88,
    "Oracle Park": 0.83,
}

_DOME_VENUES: set[str] = {
    # Subset of VENUE_METADATA["<name>"]["dome"] == True entries.
    "Rogers Centre",
    "Globe Life Field",
    "Chase Field",
    "T-Mobile Park",
    "Tropicana Field",
    "Minute Maid Park",
    "American Family Field",
    "loanDepot park",
    "Allegiant Stadium",
    "AT&T Stadium",
    "Caesars Superdome",
    "Lucas Oil Stadium",
    "Mercedes-Benz Stadium",
    "U.S. Bank Stadium",
    "State Farm Stadium",
    "NRG Stadium",
    "SoFi Stadium",
}


def _park_factor(venue_name: Optional[str]) -> float:
    if not venue_name:
        return 1.0
    v = venue_name.strip()
    if v in _PARK_FACTORS:
        return _PARK_FACTORS[v]
    # Partial / fuzzy match — short-circuit for e.g. "Fenway Park (test)"
    for key, pf in _PARK_FACTORS.items():
        if key.lower() in v.lower() or v.lower() in key.lower():
            return pf
    return 1.0


def _is_dome(venue_name: Optional[str]) -> int:
    if not venue_name:
        return 0
    v = venue_name.strip()
    if v in _DOME_VENUES:
        return 1
    for key in _DOME_VENUES:
        if key.lower() in v.lower():
            return 1
    return 0


_ALTITUDES: dict[str, int] = {
    "Coors Field": 5200,
    "Ball Arena": 5280,
    "Empower Field at Mile High": 5280,
    "Chase Field": 1082,
    "Allegiant Stadium": 2001,
    "State Farm Stadium": 1100,
    "Vivint Arena": 4226,
}


def _altitude_factor(venue: Optional[str]) -> float:
    if not venue:
        return 0.0
    alt = _ALTITUDES.get(venue.strip(), 0)
    return float(alt) / 5280.0
