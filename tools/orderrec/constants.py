"""Tunables and constants for the order reconciler (split from
``tools/order_reconciler``)."""

from __future__ import annotations

import os

# --- Tunables --------------------------------------------------------------

STUCK_GAME_HOURS = float(os.getenv("CALLISTO_STUCK_GAME_HOURS", "48"))
STUCK_PROP_HOURS = float(os.getenv("CALLISTO_STUCK_PROP_HOURS", "72"))

# Markets we know how to settle without Marco clicking a button.
SUPPORTED_MARKETS = frozenset({
    "h2h", "moneyline", "ml",
    "spreads", "spread", "run_line", "puck_line",
    "totals", "total", "over_under", "over/under",
    "player_props", "prop", "props", "player_prop",
    "sgp", "parlay",
})
