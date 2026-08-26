"""Main reconciliation entry point (split from ``tools/order_reconciler``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from tools.order_manager import FILLED, OrderManager
from tools.orderrec.constants import SUPPORTED_MARKETS
from tools.orderrec.markets import (
    _extract_line,
    _extract_player_meta,
    _normalise_market,
)
from tools.orderrec.odds import _american_pnl
from tools.orderrec.results import (
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
from tools.orderrec.stuck import _maybe_mark_stuck

logger = logging.getLogger("callisto.order_reconciler")


@dataclass
class ReconciliationReport:
    """Structured summary returned by :func:`reconcile_filled_orders`.

    Kept JSON-serialisable so the /orders/reconcile endpoint can hand it
    back to callers (and logs can tail it).
    """

    settled: int = 0
    skipped_no_result: int = 0
    skipped_unsupported: int = 0
    errors: int = 0
    stuck: int = 0
    voided: int = 0
    by_result: dict[str, int] = field(
        default_factory=lambda: {"win": 0, "loss": 0, "push": 0}
    )
    settled_order_ids: list[str] = field(default_factory=list)
    stuck_order_ids: list[str] = field(default_factory=list)
    voided_order_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "settled": self.settled,
            "skipped_no_result": self.skipped_no_result,
            "skipped_unsupported": self.skipped_unsupported,
            "errors": self.errors,
            "stuck": self.stuck,
            "voided": self.voided,
            "by_result": dict(self.by_result),
            "settled_order_ids": list(self.settled_order_ids),
            "stuck_order_ids": list(self.stuck_order_ids),
            "voided_order_ids": list(self.voided_order_ids),
        }


async def reconcile_filled_orders(
    manager: OrderManager,
    *,
    limit: int = 100,
) -> dict:
    """Scan ``filled`` orders, settle those that have resolved.

    Returns a JSON-serialisable dict from
    :meth:`ReconciliationReport.to_dict`. Stable shape — the top-level
    ``settled``/``skipped_no_result``/``errors`` keys mirror the v1 stub
    so existing call sites (``api.py:order_cron_loop``,
    ``/orders/reconcile``) keep working.
    """
    db = manager._db
    assert db is not None, "OrderManager not initialised"
    report = ReconciliationReport()

    cur = await db.execute(
        "SELECT * FROM orders WHERE state = ? "
        "ORDER BY created_at ASC LIMIT ?",
        (FILLED, int(limit)),
    )
    rows = await cur.fetchall()

    for row in rows:
        order_id = row["order_id"]
        try:
            outcome = await _reconcile_one(manager, row, report)
            if outcome is None:
                continue  # already recorded in the report
        except Exception as e:
            logger.warning(
                f"reconcile: unhandled error on {order_id}: {e}",
                exc_info=True,
            )
            report.errors += 1

    if report.settled:
        logger.info(
            f"reconcile: settled {report.settled} orders "
            f"({report.by_result}), skipped {report.skipped_no_result} "
            f"no-result, {report.skipped_unsupported} unsupported, "
            f"{report.stuck} stuck, errors {report.errors}"
        )
    return report.to_dict()


async def _reconcile_one(
    manager: OrderManager, row, report: ReconciliationReport
) -> Optional[str]:
    """Drive a single filled order through settlement.

    Returns the final state string on settle, None otherwise (bumps the
    relevant counter on ``report`` either way).
    """
    db = manager._db
    order_id = row["order_id"]
    sport = row["sport"] or ""
    event_id = row["event_id"] or ""
    market = _normalise_market(row["market"])
    side = row["side"] or ""
    stake = float(row["stake_dollars"] or 0.0)
    price = int(row["price_american"] or 0)
    notes = row["notes"] if "notes" in row.keys() else None

    if market not in SUPPORTED_MARKETS and market not in (
        "h2h", "spreads", "totals", "player_props", "sgp"
    ):
        report.skipped_unsupported += 1
        return None

    # --- Per-market resolution ---------------------------------------------
    result: Optional[str] = None

    if market in ("sgp",):
        result = await _resolve_sgp(db, notes, sport)
    elif market in ("player_props",):
        line = _extract_line(row, notes)
        # Player prop expects side like "Over 27.5" plus notes with
        # player=<name>,stat=<type>.
        player, stat_type = _extract_player_meta(notes)
        stat_value = await _lookup_player_stat(
            db, sport, event_id, player or "", stat_type or ""
        )
        if stat_value is None:
            # Stat not posted yet — same stuck/skip treatment as games.
            await _maybe_mark_stuck(manager, row, report, market)
            report.skipped_no_result += 1
            return None
        result = _resolve_player_prop(side, line, stat_value)
    else:
        game = await _lookup_game_result(db, sport, event_id)
        if game is None:
            # Nothing to settle against yet — check stuck / void, then
            # count as skipped for the caller-visible report.
            await _maybe_mark_stuck(manager, row, report, market)
            report.skipped_no_result += 1
            return None
        if market == "h2h":
            result = _resolve_moneyline(side, game)
        elif market == "spreads":
            line = _extract_line(row, notes)
            result = _resolve_spread(side, line, game)
        elif market == "totals":
            line = _extract_line(row, notes)
            result = _resolve_total(side, line, game)

    if result is None:
        report.skipped_no_result += 1
        return None

    pnl = _american_pnl(stake, price, result)
    settled_order = await manager.settle(
        order_id, result=result, pnl_dollars=pnl,
        reason="auto_from_game_results",
    )

    # Bankroll append, CLV log, hypothesis_stats refresh, Telegram.
    await _apply_bankroll(db, settled_order, pnl, result)
    await _record_clv(db, settled_order, result, pnl)
    await _refresh_hypothesis_stats(db, settled_order.hypothesis_id)
    await _emit_settle_telegram(manager, settled_order, result, pnl)

    report.settled += 1
    report.by_result[result] = report.by_result.get(result, 0) + 1
    report.settled_order_ids.append(order_id)
    return settled_order.state
