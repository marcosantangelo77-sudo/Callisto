"""Evidence age measurement — the honest first step toward live research.

There is no concept of evidence age within a run. A question takes ~43
minutes; evidence fetched at minute 2 is 41 minutes stale by the time the
conclusion is sealed, and nothing records or reacts to that.

This module MEASURES ONLY. It computes the age spread of a run's evidence —
oldest, newest, median, relative to the seal moment — so every conclusion can
state how old its evidence was when it was sealed.

Deliberately NOT here: staleness penalties, decay functions, confidence
adjustments of any kind. A penalty invented before the distribution is known
is a tuned constant pretending to be a model. Measure first; decide later
from what the spread actually looks like.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Iterable, Optional


@dataclass(frozen=True)
class EvidenceAgeSpread:
    """Age of a run's evidence at seal time. All ages in seconds."""
    oldest_s: float          # most-stale record: now - earliest fetched_at
    newest_s: float          # freshest record: now - latest fetched_at
    median_s: float          # median per-record age
    n_records: int           # records the spread is computed over
    span_s: float            # newest - oldest fetch time (the acquisition window)

    def to_dict(self) -> dict:
        return {
            "oldest_evidence_age_s": round(self.oldest_s, 3),
            "newest_evidence_age_s": round(self.newest_s, 3),
            "median_evidence_age_s": round(self.median_s, 3),
            "n_evidence_records": self.n_records,
            "acquisition_window_s": round(self.span_s, 3),
        }


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_spread(fetched_at_values: Iterable[datetime],
                   *, sealed_at: Optional[datetime] = None) -> Optional[EvidenceAgeSpread]:
    """Age spread of evidence at `sealed_at` (default: now).

    Naive datetimes are treated as UTC. Returns None for an empty set —
    a run with no evidence has no age to report; callers surface that as
    "no evidence" rather than a fabricated zero-age spread.
    """
    stamps = [_as_utc(v) for v in fetched_at_values]
    if not stamps:
        return None
    ref = _as_utc(sealed_at) if sealed_at is not None else datetime.now(timezone.utc)
    ages = [(ref - s).total_seconds() for s in stamps]
    return EvidenceAgeSpread(
        oldest_s=max(ages),        # max age = least recent fetch
        newest_s=min(ages),        # min age = most recent fetch
        median_s=median(ages),
        n_records=len(stamps),
        span_s=(max(stamps) - min(stamps)).total_seconds(),
    )
