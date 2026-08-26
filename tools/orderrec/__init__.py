"""tools.orderrec — split modules for the settlement reconciler.

Extracted from the former monolithic ``tools/order_reconciler.py``:

- ``constants``   tunables + SUPPORTED_MARKETS
- ``odds``        American-odds PnL / payout / implied-prob math
- ``markets``     market normalisation + order-notes parsing
- ``results``     async DB lookups (game_results / game_contexts / player_stats)
- ``resolution``  per-market win/loss/push resolution incl. SGP legs
- ``effects``     bankroll, clv_log, hypothesis_stats, Telegram side-effects
- ``stuck``       stuck-pending flagging and postponed/cancelled voiding
- ``reconcile``   ReconciliationReport + reconcile_filled_orders entry point

Paper/recon only — this package never enables OrderManager or BetExecutor
live paths.
"""

from tools.orderrec.constants import (
    STUCK_GAME_HOURS,
    STUCK_PROP_HOURS,
    SUPPORTED_MARKETS,
)
from tools.orderrec.odds import (
    _american_pnl,
    _american_payout,
    _american_to_implied,
    _team_matches,
)
from tools.orderrec.markets import (
    _extract_line,
    _extract_player_meta,
    _normalise_market,
    _parse_legs,
    _parse_side_for_total,
)
from tools.orderrec.results import (
    _lookup_game_context,
    _lookup_game_result,
    _lookup_player_stat,
)
from tools.orderrec.resolution import (
    _resolve_moneyline,
    _resolve_player_prop,
    _resolve_spread,
    _resolve_sgp,
    _resolve_total,
)
from tools.orderrec.effects import (
    _apply_bankroll,
    _emit_settle_telegram,
    _record_clv,
    _refresh_hypothesis_stats,
)
from tools.orderrec.stuck import (
    _maybe_mark_stuck,
    detect_voided_orders,
)
from tools.orderrec.reconcile import (
    ReconciliationReport,
    _reconcile_one,
    reconcile_filled_orders,
)

__all__ = [
    "STUCK_GAME_HOURS",
    "STUCK_PROP_HOURS",
    "SUPPORTED_MARKETS",
    "ReconciliationReport",
    "reconcile_filled_orders",
    "detect_voided_orders",
]
