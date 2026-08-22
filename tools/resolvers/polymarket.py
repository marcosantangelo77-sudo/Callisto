"""Polymarket resolver — a UMA-resolved contract IS a resolved claim.

Maps Polymarket resolution onto the domain-general OutcomeResolver surface
(tools/resolvers/base.py). outcomePrices at ~1/~0 after a "resolved" UMA
status is ground truth: YES is OUTCOME_POSITIVE, NO is OUTCOME_NEGATIVE;
anything else (open, disputed, indeterminate) stays unresolved. The
predicted probability is supplied by the caller at claim time; the
market's own last price rides along as book_implied_prob so CLV-style
scoring works outside sports.

Read-only: queries public market data through the same PolymarketAdapter
everything else uses. No wallet, no keys, no orders.
"""

from __future__ import annotations

from typing import AsyncIterator, Optional

from tools.resolvers.base import (
    OUTCOME_NEGATIVE,
    OUTCOME_POSITIVE,
    _norm_outcome,
    EvidenceRecord,
    OutcomeResolver,
)


class PolymarketOutcomeResolver(OutcomeResolver):
    name = "polymarket"

    def __init__(self, adapter, *, source: str = "polymarket"):
        """adapter: tools.domains.polymarket.market.PolymarketAdapter (or
        anything exposing get_market(ref)). Injected, so tests pass a
        fixture adapter and never touch the network."""
        self._adapter = adapter
        self._source = source

    async def iter_evidence(self, hypothesis_id: str) -> AsyncIterator[EvidenceRecord]:
        """hypothesis_id is a Polymarket market id or slug (one claim per
        contract, the natural granularity for event markets). Yields one
        EvidenceRecord when the contract has settled; nothing while it is
        still open or disputed."""
        import asyncio

        loop = asyncio.get_event_loop()

        def _fetch():
            return self._adapter.get_market(hypothesis_id)

        m = await loop.run_in_executor(None, _fetch)
        outcome = _map_result(m)
        if outcome is None:
            return  # open, disputed, or voided: no evidence yet
        yield EvidenceRecord(
            event_id=m.slug or m.id,
            predicted_prob=None,   # caller attaches the claim-time probability
            resolved_outcome=outcome,
            resolved_at=m.end_date_iso,
            payoff=1.0 if outcome == OUTCOME_POSITIVE else -1.0,
            book_implied_prob=(m.outcome_prices[0]
                               if m.outcome_prices and m.outcome_prices[0] is not None
                               else None),
            context_key=m.event_slug,
            source=self._source,
        )


def _map_result(m) -> Optional[str]:
    """Settled = UMA status 'resolved' AND prices pinned at 1/0. Normalised
    onto the general outcome vocabulary via base._norm_outcome."""
    if not m.closed or m.uma_resolution_status != "resolved":
        return None
    t = m.resolved_outcome()
    return _norm_outcome(t) if t else None
