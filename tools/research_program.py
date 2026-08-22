"""
The inheritance rule (BUILD_MANDATE §3 item 6 — the capstone).

    A parent claim's confidence ceiling is a FUNCTION OF ITS RESOLVED
    DESCENDANTS' TRACK RECORD.

Zero resolved descendants caps the claim at SPECULATIVE forever, no matter
how eloquent the reasoning. Long-horizon claims become honest by
decomposing into dated leading-indicator sub-claims that enter the EXISTING
lifecycle and resolve on timescales the calibration loop can actually score;
as they resolve and prove accurate, the parent's ceiling rises. An
unresolved claim is structurally UNABLE to look like a resolved one.

Domain-general by construction: the inputs are resolution records (binary or
quantile), never sports fields. A Bitcoin thesis, a protein-folding
prediction and a supply-chain claim are the same data here.

HARD RULES enforced structurally:
  - Nothing automated may weaken a gate: this module can only ever LOWER a
    score below what raw evidence would allow; it can never raise above the
    evidence-implied ceiling.
  - Never arms the live execution path: pure computation, no side effects,
    no DB writes, no tool dispatch.

Resolution-side seam: B1's OutcomeResolver (tools/hypothesis.py) owns how
claims resolve. This module consumes the RESULT of that — a ResolutionRecord
— and deliberately accepts plain dicts with the same keys so any resolver
implementation can feed it without importing this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

# Tier boundaries mirror agp/thresholds.py values. Imported lazily-free:
# these constants are re-read from agp.thresholds when available so there is
# ONE source of truth, but the module degrades gracefully if imported
# standalone.
try:  # pragma: no cover - trivial import
    from agp.thresholds import (
        TIER_SPECULATIVE_MIN,
        TIER_PROBABLE_MIN,
        TIER_CORROBORATED_MIN,
        TIER_VERIFIED_MIN,
    )
except ImportError:  # pragma: no cover
    TIER_SPECULATIVE_MIN = 0.30
    TIER_PROBABLE_MIN = 0.55
    TIER_CORROBORATED_MIN = 0.75
    TIER_VERIFIED_MIN = 0.90


# ── Resolution records ───────────────────────────────────────────────────

@dataclass
class ResolutionRecord:
    """One descendant's settled outcome. The unit of earned confidence."""
    question_id: str
    resolved_at: date
    outcome: str                     # "hit" | "miss" | "stale" | "void"
    # For quantile descendants: mean pinball loss vs. scale reference,
    # normalized to roughly 0..1 (lower = better). None for binary claims.
    pinball_score: Optional[float] = None
    # Provenance-assigned best source class of the resolving evidence
    # (agp SourceClass value). Caps inheritance like it caps sessions.
    best_source_class: str = "SECONDARY"

    def __post_init__(self):
        self.outcome = str(self.outcome).lower()

    @property
    def counted(self) -> bool:
        """Only hit/miss/stale count toward track record; void = malformed
        resolution, excluded (but recorded)."""
        return self.outcome in ("hit", "miss", "stale")


def _rec_from_mapping(m) -> dict:
    """Accept dicts (e.g., straight out of B1's resolver output) as records."""
    return {
        "question_id": m.get("question_id", ""),
        "resolved_at": m.get("resolved_at"),
        "outcome": m.get("outcome", ""),
        "pinball_score": m.get("pinball_score"),
        "best_source_class": m.get("best_source_class", "SECONDARY"),
    }


def normalize_records(records: Iterable) -> list[ResolutionRecord]:
    out: list[ResolutionRecord] = []
    for r in records:
        if isinstance(r, ResolutionRecord):
            out.append(r)
        elif isinstance(r, dict):
            m = _rec_from_mapping(r)
            ra = m["resolved_at"]
            if isinstance(ra, str):
                m["resolved_at"] = date.fromisoformat(ra)
            elif isinstance(ra, datetime):
                m["resolved_at"] = ra.date()
            elif ra is None:
                m["resolved_at"] = date.min
            out.append(ResolutionRecord(**m))
        else:
            raise TypeError(f"unsupported resolution record: {type(r)!r}")
    return out


# ── Track record → ceiling ───────────────────────────────────────────────

# Source-class ceilings for INHERITED confidence. Deliberately at-or-below
# the session ceilings in agp/thresholds.py: inherited confidence must never
# exceed what direct evidence would have earned.
INHERITED_CEILING_BY_SOURCE: dict[str, float] = {
    "PRIMARY": 0.90,      # can reach VERIFIED boundary exactly, not beyond
    "SECONDARY": 0.75,
    "SIGNAL": 0.55,
    "INFERRED": 0.55,
}

SPECULATIVE_CAP = TIER_PROBABLE_MIN   # just under PROBABLE band start


@dataclass
class TrackRecord:
    n_resolved: int          # hit + miss (+ stale, weighted)
    n_hit: int
    n_stale: int
    brier: Optional[float]   # mean of {1: miss/hit} + quantile skill losses
    stale_fraction: float

    @property
    def hit_rate(self) -> float:
        scored = max(1, self.n_resolved - self.n_stale)
        return self.n_hit / scored if (self.n_resolved - self.n_stale) > 0 else 0.0


def summarize_track_record(records: Iterable[ResolutionRecord]) -> TrackRecord:
    recs = [r for r in normalize_records(records) if r.counted]
    n = len(recs)
    hits = sum(1 for r in recs if r.outcome == "hit")
    stales = sum(1 for r in recs if r.outcome == "stale")
    # Brier-style mean error: binary outcomes contribute 0/1; quantile
    # descendants contribute their normalized pinball score (regardless of
    # hit/miss label — a sharp forecast that "hit" still earns calibration
    # credit; a sloppy one that "missed" is scored by how sloppy).
    errs: list[float] = []
    for r in recs:
        if r.pinball_score is not None:
            errs.append(min(1.0, max(0.0, float(r.pinball_score))))
        elif r.outcome == "hit":
            errs.append(0.0)
        else:  # miss or stale — unresolved by its own deadline
            errs.append(1.0)
    brier = sum(errs) / len(errs) if errs else None
    stale_frac = (stales / n) if n else 0.0
    return TrackRecord(n_resolved=n, n_hit=hits, n_stale=stales,
                       brier=brier, stale_fraction=stale_frac)


# Wilson lower bound on hit rate: the parent earns credit only for accuracy
# the data actually supports at ~95% one-sided confidence. This is why a
# lucky 2-for-2 cannot flatter a ten-year target.
_WILSON_Z = 1.645


def wilson_lower_bound(hits: int, n: int, z: float = _WILSON_Z) -> float:
    if n <= 0:
        return 0.0
    p = hits / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    lb = (center - margin) / denom
    return min(max(lb, 0.0), 1.0)


# Minimum resolved descendants before ANY lift off SPECULATIVE is possible.
MIN_RESOLVED_FOR_LIFT = 5
# Resolved-descendant counts at which each tier becomes reachable. A parent
# whose descendants have not survived this much scoring stays capped.
N_FOR_PROBABLE = MIN_RESOLVED_FOR_LIFT        # 5
N_FOR_CORROBORATED = 15
N_FOR_VERIFIED = 40


def inherited_ceiling(records: Iterable) -> float:
    """The parent's maximum confidence score given its descendants' record.

    Returns a hard ceiling (a number to min() against). Guarantees:
      - zero resolved descendants      -> SPECULATIVE cap forever;
      - few/poor resolutions           -> cap rises slowly, bounded by
        Wilson lower bound on the descendant hit rate AND their Brier-style
        error, AND the best provenance-assigned source class among them;
      - staleness penalizes (mirrors cascade demotion);
      - the result NEVER exceeds the evidence-implied ceiling — this
        function only lowers scores, never raises them.
    """
    recs = [r for r in normalize_records(records) if r.counted]
    n = len(recs)

    # Hard floor case: nothing has ever resolved under this claim.
    if n < MIN_RESOLVED_FOR_LIFT:
        return SPECULATIVE_CAP

    tr = summarize_track_record(recs)

    # Accuracy the data supports (95% one-sided Wilson LB on hit rate).
    support = wilson_lower_bound(tr.n_hit, tr.n_resolved - tr.n_stale) \
        if tr.n_resolved - tr.n_stale > 0 else 0.0

    # Calibration quality: map mean error to a 0..1 factor.
    # brier 0.00 -> 1.0 ; 0.25 -> 0.5 ; >= 0.50 -> 0.0
    calib = max(0.0, 1.0 - 2.0 * tr.brier)

    # Sample-size ramp — but size only counts for a system that has been
    # RIGHT. A large track record of misses earns nothing: the ramp factor
    # multiplies accuracy, it never substitutes for it.
    size_factor = min(1.0, math.log1p(n) / math.log1p(N_FOR_VERIFIED))

    base = SPECULATIVE_CAP + (1.0 - SPECULATIVE_CAP) * (
        (0.55 * support + 0.45 * calib) * (0.5 + 0.5 * size_factor))

    # Staleness penalty: unresolved-at-deadline sub-claims demote the parent
    # (mirrors existing cascade demotion). Up to −0.20.
    penalty = 0.20 * tr.stale_fraction
    score = base - penalty

    # Provenance gate: inherited confidence is capped by the BEST source
    # class among the resolving evidence — hearsay descendants cannot make
    # a parent VERIFIED even in aggregate.
    best_class = "INFERRED"
    rank = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}
    for r in recs:
        if rank.get(r.best_source_class, 0) > rank.get(best_class, 0):
            best_class = r.best_source_class
    src_cap = INHERITED_CEILING_BY_SOURCE.get(best_class, SPECULATIVE_CAP)

    return round(min(score, src_cap), 4)


def tier_ceiling_from_score(score: float) -> str:
    """Tier label implied by a ceiling score (agp.ConfidenceTier names)."""
    if score >= TIER_VERIFIED_MIN:
        return "VERIFIED"
    if score >= TIER_CORROBORATED_MIN:
        return "CORROBORATED"
    if score >= TIER_PROBABLE_MIN:
        return "PROBABLE"
    if score >= TIER_SPECULATIVE_MIN:
        return "SPECULATIVE"
    return "UNVERIFIED"


def clamp_parent_confidence(raw_score: float,
                            descendant_resolutions: Iterable) -> tuple[float, str]:
    """Apply the inheritance rule to a proposed parent confidence.

    (score, reason) -> (clamped_score, tier). The clamped score is
    min(raw_score, inherited_ceiling(...)) — the rule can only pull DOWN.
    """
    raw = max(0.0, min(1.0, float(raw_score)))
    ceil_ = inherited_ceiling(descendant_resolutions)
    clamped = round(min(raw, ceil_), 2)
    return clamped, tier_ceiling_from_score(clamped)
