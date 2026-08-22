"""Kalshi source-registry shim.

The Kalshi implementation lives in tools/domains/kalshi/ because it is a full
DomainPlugin (market data + an OutcomeResolver + edge wiring), not only a source
adapter. But tools/sources/adapters.py::register_all imports every source as
tools.sources.<name>, so without this module the entry ("kalshi",
"KalshiAdapter") fails to import and is skipped — silently, by design, which is
how a registered-but-broken source became indistinguishable from an absent one.

Re-export rather than duplicate, so there is exactly one implementation.
"""
from __future__ import annotations

from tools.domains.kalshi.market import SPEC, KalshiAdapter

__all__ = ["SPEC", "KalshiAdapter"]
