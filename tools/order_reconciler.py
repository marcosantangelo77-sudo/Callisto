"""Automated settlement reconciler — facade over :mod:`tools.orderrec`.

The implementation was split into the ``tools.orderrec`` package:

- ``tools.orderrec.constants``   tunables + SUPPORTED_MARKETS
- ``tools.orderrec.odds``        American-odds PnL / payout / implied math
- ``tools.orderrec.markets``     market normalisation + notes parsing
- ``tools.orderrec.results``     async DB lookups (results/contexts/stats)
- ``tools.orderrec.resolution``  per-market win/loss/push resolution + SGP
- ``tools.orderrec.effects``     bankroll / clv_log / hypothesis_stats / Telegram
- ``tools.orderrec.stuck``       stuck-pending flagging + void detection
- ``tools.orderrec.reconcile``   ReconciliationReport + reconcile_filled_orders

Every public and private name is re-exported here so existing importers
(``tools.order_manager`` shims, ``tests/test_order_reconciler.py``, cron
wiring in ``api.py``) keep working unchanged. Paper/recon only — this
module never enables OrderManager or BetExecutor live paths.
"""

from __future__ import annotations

import json  # noqa: F401  (kept for backward-compat importers)
import logging

# --- Tunables --------------------------------------------------------------
from tools.orderrec.constants import (  # noqa: F401
    STUCK_GAME_HOURS,
    STUCK_PROP_HOURS,
    SUPPORTED_MARKETS,
)

# --- Odds helpers -----------------------------------------------------------
from tools.orderrec.odds import (  # noqa: F401
    _american_payout,
    _american_pnl,
    _american_to_implied,
    _team_matches,
)

# --- Markets / notes parsing -------------------------------------------------
from tools.orderrec.markets import (  # noqa: F401
    _extract_line,
    _extract_player_meta,
    _normalise_market,
    _parse_legs,
    _parse_side_for_total,
)

# --- Game-result lookups -----------------------------------------------------
from tools.orderrec.results import (  # noqa: F401
    _lookup_game_context,
    _lookup_game_result,
    _lookup_player_stat,
)

# --- Per-market resolution ---------------------------------------------------
from tools.orderrec.resolution import (  # noqa: F401
    _resolve_moneyline,
    _resolve_player_prop,
    _resolve_spread,
    _resolve_sgp,
    _resolve_total,
)

# --- Side-effect helpers -----------------------------------------------------
from tools.orderrec.effects import (  # noqa: F401
    _apply_bankroll,
    _emit_settle_telegram,
    _record_clv,
    _refresh_hypothesis_stats,
)

# --- Stuck / void detection --------------------------------------------------
from tools.orderrec.stuck import (  # noqa: F401
    _maybe_mark_stuck,
    detect_voided_orders,
)

# --- Main entry -------------------------------------------------------------
from tools.orderrec.reconcile import (  # noqa: F401
    ReconciliationReport,
    _reconcile_one,
    reconcile_filled_orders,
)

logger = logging.getLogger("callisto.order_reconciler")
