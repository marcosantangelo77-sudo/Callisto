"""Event-study harness: COUNT forward returns after dated narrative events.

The owner's question shape: "how many times has a major speech come out and
Bitcoin gone up 15%, and what happened in the next three months?" This module
answers by counting, never by narrating. It produces a DISTRIBUTION (n,
median, IQR, distinguishable-from-noise verdict) — never a conclusion, never
a confidence score, never a trading signal.

Components:
  events.py   — event set construction with PROVABLE publication timestamps.
                An event without an IMMUTABLE_SNAPSHOT proof at-or-before the
                event date is EXCLUDED (fail-closed). A statement mis-dated
                after the move it "predicted" is fabricated alpha; this is
                the failure mode that would make the whole line worthless.
  outcomes.py — FRED outcome series aligned to t=0 at each event; forward
                log-returns at +1/+4/+12 weeks; matched random-date controls.

Statistics come from tools/retrodiction/scoring.py machinery where it fits
(permutation-style sign-flip test reimplemented here ONLY because scoring.py's
API is Brier-specific); the report itself carries no verdict language beyond
"indistinguishable from noise" / "distinguishable at p<...".

HARD RULES enforced in code:
  - no confidence scores raised anywhere in this package
  - output is data + distribution summary only
"""

from tools.event_study.events import Event, EventSet, build_event_set
from tools.event_study.outcomes import (
    OutcomeSeries, measure_forward_returns, random_control_returns, report)

__all__ = [
    "Event", "EventSet", "build_event_set",
    "OutcomeSeries", "measure_forward_returns",
    "random_control_returns", "report",
]
