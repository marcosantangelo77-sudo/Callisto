#!/usr/bin/env python3
"""One sanctioned LIVE read of Polymarket's public market data.

The only network call the polymarket build is allowed outside fixtures.
Read-only: list a page of markets and pull top-of-book for the most
liquid one. No wallet, no keys, no orders — public GETs through
RestSource so provenance is recorded like every other fetch.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agp.provenance import ProvenanceLedger          # noqa: E402
from tools.domains.polymarket.market import (        # noqa: E402
    SPEC, PolymarketAdapter)
from tools.sources.base import RestSource            # noqa: E402


def main() -> int:
    ad = PolymarketAdapter(RestSource(SPEC, ledger=ProvenanceLedger()))
    page = ad.list_markets(limit=25)
    print(f"fetched {len(page['markets'])} markets  "
          f"(sha256={page['_fetch']['sha256'][:16]}...)")
    for m in page["markets"][:5]:
        print(f"  {m.id:>8}  {str(m.outcome_prices[0]):>7}  {m.question[:70]}")
    liquid = max(page["markets"],
                 key=lambda m: m.volume_num or 0.0)
    if not liquid.yes_token_id:
        print("most-liquid market has no CLOB tokens; skipping book")
        return 0
    quote, meta = ad.market_quote(liquid.slug or liquid.id)
    fair, audit = quote.fair_probability()
    print(f"book: yes_bid={meta['yes_bid']} yes_ask={meta['yes_ask']} "
          f"-> devigged fair={fair:.4f} overround={audit.get('overround')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
