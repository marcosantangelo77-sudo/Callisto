"""Polymarket source-registry shim.

Same pattern as tools/sources/kalshi.py: the implementation lives in
tools/domains/polymarket/ because it is a full DomainPlugin (market data +
an OutcomeResolver + edge wiring), but tools/sources/adapters.py::
register_all imports every source as tools.sources.<name>, so without
this module the ("polymarket", "PolymarketAdapter") entry fails to import
and is skipped — silently, by design.

Re-export rather than duplicate, so there is exactly one implementation.
"""
from __future__ import annotations

from tools.domains.polymarket.market import SPEC, PolymarketAdapter

__all__ = ["SPEC", "PolymarketAdapter"]
