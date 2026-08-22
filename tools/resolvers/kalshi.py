"""Kalshi resolver — a settled event contract IS a resolved claim.

Maps Kalshi settlement onto the domain-general OutcomeResolver surface
(tools/resolvers/base.py). A contract resolving YES is OUTCOME_POSITIVE,
NO is OUTCOME_NEGATIVE; anything else (unresolved, void) is left
unresolved/indeterminate. The predicted probability is supplied by the
caller at claim time (the calibrated probability that met the market's
implied one in assess_edge); the market's own implied probability at
claim time rides along as book_implied_prob so CLV-style scoring works
outside sports.

Read-only: queries the public market-data endpoints through the same
KalshiAdapter everything else uses. No orders, no credentials.
"""

from __future__ import annotations

from typing import AsyncIterator, Optional

from tools.resolvers.base import (
    OUTCOME_NEGATIVE,
    OUTCOME_POSITIVE,
    EvidenceRecord,
    OutcomeResolver,
)


class KalshiOutcomeResolver(OutcomeResolver):
    name = "kalshi"

    def __init__(self, adapter, *, source: str = "kalshi"):
        """adapter: tools.domains.kalshi.market.KalshiAdapter (or anything
        exposing get_market(ticker)). Injected, so tests pass a fixture
        adapter and never touch the network."""
        self._adapter = adapter
        self._source = source

    async def iter_evidence(self, hypothesis_id: str) -> AsyncIterator[EvidenceRecord]:
        """hypothesis_id is a Kalshi market ticker (one claim per contract,
        the natural granularity for event markets). Yields one EvidenceRecord
        when the contract has settled; nothing while it is still open."""
        import asyncio

        loop = asyncio.get_event_loop()

        def _fetch():
            return self._adapter.get_market(hypothesis_id)

        m = await loop.run_in_executor(None, _fetch)
        outcome = self._map_result(m.result)
        if outcome is None:
            return  # still open or voided: no evidence yet
        yield EvidenceRecord(
            event_id=m.ticker,
            predicted_prob=None,   # caller attaches the claim-time probability
            resolved_outcome=outcome,
            resolved_at=m.expected_expiration_time or m.close_time,
            payoff=1.0 if outcome == OUTCOME_POSITIVE else -1.0,
            book_implied_prob=m.mid,
            context_key=m.event_ticker,
            source=self._source,
        )

    @staticmethod
    def _map_result(result: str) -> Optional[str]:
        t = (result or "").strip().lower()
        if t == "yes":
            return OUTCOME_POSITIVE
        if t == "no":
            return OUTCOME_NEGATIVE
        return None
