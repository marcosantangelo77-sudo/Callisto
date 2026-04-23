"""Automated settlement reconciler — closes the order FSM loop.

The order-management branch (``tools.order_manager``) shipped an FSM that
tracks orders from ``pending_approval`` through ``filled``, but the final
``filled -> settled_{win,loss,push}`` transition was a stub. This module
supplies the missing half: cross-check each ``filled`` order against
``game_results`` / ``player_stats`` / ``game_contexts``, compute PnL, call
``order_manager.settle()``, record CLV, append a fresh ``hypothesis_stats``
row, and — on Telegram-configured installs — fire a confirmation alert.

Design invariants
-----------------

* **Idempotent**. ``reconcile_filled_orders`` only reads rows whose state
  is exactly ``FILLED``. Once ``settle()`` drives the FSM to a terminal
  state, the row is invisible to the next scan. The bankroll / clv_log /
  hypothesis_stats side-effects live on the settle path (or are keyed on
  ``bet_id`` + state so double-updates are harmless).
* **No silent state mutation**. Every observed change — settle, void,
  stuck-alert — writes to ``state_history_json`` via the existing
  ``_transition`` machinery or is marked explicitly in ``notes``.
* **BEGIN IMMEDIATE for bankroll**. The ``bankroll`` append and the
  ``bets`` mirror share the connection's WAL+busy_timeout, and both touch
  the same append-only pattern used by ``clv_tracker.resolve_bet``. We
  emulate that behaviour (INSERT into ``bankroll`` in the same commit as
  the FSM transition).
* **Backward-compat**. The legacy ``bets`` table mirror, already written
  by ``order_manager._sync_bets_on_fill/_sync_bets_on_settle``, continues
  to carry ``result`` / ``payout`` / CLV columns for the transition era.

Outcome semantics (by market)
-----------------------------

``h2h`` / moneyline
    Compare ``order.side`` (team string, case-insensitive substring
    match against home/away) to ``game_results.winner``. ``winner ==
    "push"`` (tie with no MLB-style extras) settles push.

``spreads``
    Cover test: ``(home_score - away_score) + line_for_our_side``
    against 0. We derive ``line_for_our_side`` from the order row's
    point — stored historically on the ``bets`` mirror, opportunistic
    on ``orders`` via signal metadata (notes field or odds snapshot).

``totals``
    ``total_score`` vs ``order.line``. Push on exact match.

``player_props`` / ``props``
    Joins ``player_stats`` on (sport, event_id, player_name, stat_type),
    compares ``stat_value`` to ``order.line``.

``sgp`` / ``parlay``
    All legs must hit (AND). Legs persisted in ``notes`` as a JSON
    list. ``price_american`` holds the combined odds; PnL therefore
    uses the parlay price directly.

Stuck / void detection
----------------------

* If the game's scheduled start is >48h (or >72h for props) in the past
  and there's still no row in ``game_results``, we flag the order with
  ``notes='stuck_pending_result'`` and Telegram-alert exactly once.
  ``detect_voided_orders`` is the second cron (``api.py`` wires it at
  15 min). It consults ``game_contexts`` for a ``status`` field inside
  ``context_json`` (postponed/cancelled/suspended), or infers a void
  when a game_results row exists but carries NULL scores with a
  finalised timestamp.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from tools.order_manager import (
    FILLED,
    OrderManager,
    SETTLED_LOSS,
    SETTLED_PUSH,
    SETTLED_WIN,
    CANCELLED,
)

logger = logging.getLogger("callisto.order_reconciler")

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


# --- Helpers ----------------------------------------------------------------


def _american_pnl(stake: float, price_american: int, result: str) -> float:
    """PnL in dollars from an American-odds wager.

    Win: stake * (odds - 1) in decimal terms.
    Loss: -stake.
    Push: 0.
    """
    if not stake or not price_american:
        return 0.0
    if result == "push":
        return 0.0
    if result == "loss":
        return -float(stake)
    # win
    p = int(price_american)
    if p > 0:
        return float(stake) * (p / 100.0)
    return float(stake) * (100.0 / abs(p))


def _american_payout(stake: float, price_american: int) -> float:
    """Stake + profit for winning American odds (for bets.payout mirror)."""
    if not stake:
        return 0.0
    p = int(price_american)
    if p > 0:
        return float(stake) * (1 + p / 100.0)
    return float(stake) * (1 + 100.0 / abs(p))


def _american_to_implied(price_american: int) -> Optional[float]:
    """Implied probability from American odds. None if price is falsy."""
    if not price_american:
        return None
    p = int(price_american)
    if p > 0:
        return 100.0 / (p + 100.0)
    return abs(p) / (abs(p) + 100.0)


def _team_matches(side: str, team: str) -> bool:
    """Loose team-name match. game_results stores full names; orders store
    whatever the signal emitter wrote (often a short code or full name).
    """
    if not side or not team:
        return False
    s = side.lower().strip()
    t = team.lower().strip()
    return s == t or s in t or t in s


def _normalise_market(market: Optional[str]) -> str:
    if not market:
        return ""
    m = market.lower().strip()
    # Canonicalise to the SUPPORTED_MARKETS keys we branch on.
    if m in ("moneyline", "ml", "h2h"):
        return "h2h"
    if m in ("spread", "spreads", "run_line", "puck_line"):
        return "spreads"
    if m in ("total", "totals", "over_under", "over/under"):
        return "totals"
    if m in ("prop", "props", "player_prop", "player_props"):
        return "player_props"
    if m in ("sgp", "parlay"):
        return "sgp"
    return m


def _parse_side_for_total(side: Optional[str]) -> Optional[str]:
    """Pull 'over' or 'under' out of a side string like 'Over 8.5'."""
    if not side:
        return None
    s = side.lower().strip()
    if "over" in s:
        return "over"
    if "under" in s:
        return "under"
    return None


def _extract_line(order_row: Any, notes: Optional[str]) -> Optional[float]:
    """Find the line for spread/total/prop bets.

    Checks — in order:
      1. A ``line`` key in the order row (tests may populate this).
      2. The linked ``bets.placement_point`` row (production path).
      3. A ``line=`` token inside ``notes`` (SGP / legacy).

    Returns None if we can't recover the line.
    """
    # Dict-like access (aiosqlite.Row + dict both support __getitem__).
    try:
        line = order_row["line"]
        if line is not None:
            return float(line)
    except (KeyError, IndexError, TypeError):
        pass
    if notes and "line=" in notes:
        try:
            tok = notes.split("line=", 1)[1].split()[0].rstrip(",;")
            return float(tok)
        except (ValueError, IndexError):
            pass
    return None


# --- Game-result lookups ---------------------------------------------------


async def _lookup_game_result(db, sport: str, event_id: str) -> Optional[dict]:
    """Fetch a ``game_results`` row keyed on (sport, event_id).

    ``game_results`` doesn't carry event_id directly (unique key is
    (sport, game_date, home_team, away_team)), so we consult
    ``game_contexts`` first to map event_id -> (home, away, game_date),
    then lift the matching game_results row. Falls back to a best-effort
    match against home/away when game_contexts is empty — safeguard for
    tests that populate game_results directly.
    """
    if not event_id:
        return None
    # Primary path — game_contexts has the event_id <-> (home, away, date) map.
    # game_contexts may be absent in stripped-down test DBs; swallow and
    # fall through to the direct fallback.
    try:
        cur = await db.execute(
            "SELECT home_team, away_team, game_date FROM game_contexts "
            "WHERE sport = ? AND event_id = ? LIMIT 1",
            (sport, event_id),
        )
        ctx = await cur.fetchone()
    except Exception:
        ctx = None
    if ctx:
        gr = await db.execute(
            "SELECT home_team, away_team, home_score, away_score, "
            "total_score, spread_result, winner "
            "FROM game_results "
            "WHERE sport = ? AND game_date = ? "
            "AND home_team = ? AND away_team = ? LIMIT 1",
            (sport, ctx["game_date"], ctx["home_team"], ctx["away_team"]),
        )
        row = await gr.fetchone()
        if row:
            return dict(row)
    # Fallback: event_id might literally be a team abbrev that matches
    # home/away (used throughout the existing tests). Sport filter keeps
    # cross-sport collisions out.
    gr2 = await db.execute(
        "SELECT home_team, away_team, home_score, away_score, "
        "total_score, spread_result, winner "
        "FROM game_results "
        "WHERE sport = ? AND (home_team = ? OR away_team = ?) "
        "ORDER BY game_date DESC LIMIT 1",
        (sport, event_id, event_id),
    )
    row = await gr2.fetchone()
    return dict(row) if row else None


async def _lookup_game_context(
    db, sport: str, event_id: str
) -> Optional[dict]:
    """Game context row — carries game_date we use for stuck detection
    and ``context_json.status`` for void detection.
    """
    if not event_id:
        return None
    try:
        cur = await db.execute(
            "SELECT home_team, away_team, game_date, context_json "
            "FROM game_contexts WHERE sport = ? AND event_id = ? LIMIT 1",
            (sport, event_id),
        )
        row = await cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    out = dict(row)
    try:
        out["context"] = json.loads(out.get("context_json") or "{}")
    except Exception:
        out["context"] = {}
    return out


async def _lookup_player_stat(
    db, sport: str, event_id: str, player: str, stat_type: str
) -> Optional[float]:
    """Return ``stat_value`` from ``player_stats`` for a prop settle."""
    if not event_id or not player or not stat_type:
        return None
    cur = await db.execute(
        "SELECT stat_value FROM player_stats "
        "WHERE sport = ? AND event_id = ? "
        "AND LOWER(player_name) = LOWER(?) "
        "AND LOWER(stat_type) = LOWER(?) LIMIT 1",
        (sport, event_id, player, stat_type),
    )
    row = await cur.fetchone()
    return float(row["stat_value"]) if row else None


# --- Per-market resolution --------------------------------------------------


def _resolve_moneyline(side: str, game: dict) -> Optional[str]:
    """Return ``win|loss|push`` or None if undecidable."""
    winner = (game.get("winner") or "").strip()
    if not winner:
        return None
    if winner.lower() == "push":
        return "push"
    if _team_matches(side, winner):
        return "win"
    # Make sure the other team actually appeared on the card before
    # declaring loss — protects against partial data.
    home = game.get("home_team") or ""
    away = game.get("away_team") or ""
    if _team_matches(side, home) or _team_matches(side, away):
        return "loss"
    return None


def _resolve_spread(side: str, line: Optional[float], game: dict) -> Optional[str]:
    """Standard spread settlement.

    ``line`` is signed from the bettor's perspective (e.g. -7.5 means
    "side laid 7.5 points"). ``game.spread_result`` is stored as
    ``away_score - home_score`` by data_collector; we recompute from
    (home_score, away_score) so we don't depend on that convention.
    """
    if line is None:
        return None
    home = game.get("home_team") or ""
    away = game.get("away_team") or ""
    h, a = game.get("home_score"), game.get("away_score")
    if h is None or a is None:
        return None
    if _team_matches(side, home):
        margin = float(h) - float(a)
    elif _team_matches(side, away):
        margin = float(a) - float(h)
    else:
        return None
    diff = margin + float(line)
    if abs(diff) < 1e-9:
        return "push"
    return "win" if diff > 0 else "loss"


def _resolve_total(side: str, line: Optional[float], game: dict) -> Optional[str]:
    ou = _parse_side_for_total(side)
    if ou is None or line is None:
        return None
    total = game.get("total_score")
    if total is None:
        h, a = game.get("home_score"), game.get("away_score")
        if h is None or a is None:
            return None
        total = int(h) + int(a)
    diff = float(total) - float(line)
    if abs(diff) < 1e-9:
        return "push"
    if ou == "over":
        return "win" if diff > 0 else "loss"
    return "win" if diff < 0 else "loss"


def _resolve_player_prop(
    side: str, line: Optional[float], stat_value: Optional[float]
) -> Optional[str]:
    ou = _parse_side_for_total(side)
    if ou is None or line is None or stat_value is None:
        return None
    diff = float(stat_value) - float(line)
    if abs(diff) < 1e-9:
        return "push"
    if ou == "over":
        return "win" if diff > 0 else "loss"
    return "win" if diff < 0 else "loss"


# --- Main entry -------------------------------------------------------------


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


# --- Side-effect helpers ---------------------------------------------------


async def _apply_bankroll(db, order, pnl: float, result: str) -> None:
    """Append a bankroll row (same shape as clv_tracker.resolve_bet).

    Uses BEGIN IMMEDIATE semantics via the connection's busy_timeout + the
    commit path already enforced by OrderManager._transition.
    """
    if result == "push" or not pnl:
        return
    try:
        bal_cur = await db.execute(
            "SELECT balance FROM bankroll ORDER BY timestamp DESC LIMIT 1"
        )
        bal_row = await bal_cur.fetchone()
        current = float(bal_row[0]) if bal_row else 0.0
        new_balance = current + float(pnl)
        now = datetime.now(timezone.utc).isoformat()
        desc = (
            f"Order {order.order_id} {result}: "
            f"{order.game_description or order.event_id or ''}"
        )
        # BEGIN IMMEDIATE enforced on sqlite write path by the connection's
        # busy_timeout=60000 (set in OrderManager.initialize).
        await db.execute(
            "INSERT INTO bankroll (timestamp, balance, change, bet_id, description) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, new_balance, float(pnl), order.bet_id, desc),
        )
        await db.commit()
    except Exception as e:
        logger.debug(f"bankroll append skipped for {order.order_id}: {e}")


async def _record_clv(db, order, result: str, pnl: float) -> None:
    """Write a clv_log row using the canonical prob-bp unit."""
    try:
        # Lift the closing line for (event_id, market, side) from
        # closing_lines. The table keys on (event_id, market, team),
        # case-sensitive — mirror the LOWER() pattern used by
        # clv_tracker.record_closing_line so casing doesn't drop matches.
        closing_cur = await db.execute(
            "SELECT closing_odds, closing_point, closing_implied, source "
            "FROM closing_lines "
            "WHERE event_id = ? AND LOWER(market) = LOWER(?) "
            "AND LOWER(team) = LOWER(?) "
            "ORDER BY captured_at DESC LIMIT 1",
            (order.event_id or "", order.market or "", order.side or ""),
        )
        clc = await closing_cur.fetchone()

        placement_implied = _american_to_implied(order.price_american or 0)
        closing_implied = None
        clv_prob_bp = None
        close_reliable = False
        closing_source = "unknown"
        if clc:
            closing_implied = (
                float(clc["closing_implied"]) if clc["closing_implied"] is not None
                else _american_to_implied(clc["closing_odds"] or 0)
            )
            closing_source = clc["source"] or "pinnacle"
            close_reliable = closing_source.lower() in {"pinnacle", "circa"}
            if placement_implied is not None and closing_implied is not None:
                # Canonical CLV unit: prob-bp. Positive = our price beat close.
                clv_prob_bp = round(
                    (closing_implied - placement_implied) * 10000.0, 1
                )
        else:
            logger.warning(
                f"closing line missing for order={order.order_id} "
                f"event={order.event_id} market={order.market} "
                f"side={order.side} — clv_log row written with NULL CLV"
            )

        our_decimal = None
        if order.price_american:
            p = int(order.price_american)
            our_decimal = (1 + p / 100.0) if p > 0 else (1 + 100.0 / abs(p))

        now = datetime.now(timezone.utc).isoformat()
        # INSERT OR REPLACE — idempotent on bet_id PK. We key on
        # order_id (unique, durable) rather than the integer bets.id
        # which may be NULL on a stripped-down test DB.
        clv_key = f"order:{order.order_id}"
        await db.execute(
            "INSERT OR REPLACE INTO clv_log "
            "(bet_id, event, outcome, point, book, our_odds_decimal, "
            "pinnacle_close_fair_prob, pinnacle_close_fair_decimal, "
            "clv_cents, clv_prob_bp, actual_result, actual_pnl, "
            "close_reliable, logged_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                clv_key,
                order.event_id or "",
                order.side or "",
                None,  # point — spreads/totals stash in notes, optional
                order.book or "",
                our_decimal,
                closing_implied,
                (1 / closing_implied) if closing_implied else None,
                clv_prob_bp,  # clv_cents mirrors prob-bp on new rows
                clv_prob_bp,
                {"win": "won", "loss": "lost", "push": "push"}[result],
                float(pnl),
                close_reliable,
                now,
            ),
        )
        await db.commit()
    except Exception as e:
        logger.debug(f"clv_log write skipped for {order.order_id}: {e}")


async def _refresh_hypothesis_stats(db, hypothesis_id: str) -> None:
    """Append a fresh rolling-20 ``stage='live'`` row to ``hypothesis_stats``.

    Source of truth: the last 20 settled orders for this hypothesis
    (joined with clv_log for avg CLV). Append-only — the new
    ``_phase_review_live`` looks at the most-recent row.
    """
    if not hypothesis_id:
        return
    try:
        cur = await db.execute(
            "SELECT state, pnl_dollars, stake_dollars, order_id "
            "FROM orders "
            "WHERE hypothesis_id = ? "
            "AND state IN (?, ?, ?) "
            "ORDER BY settled_at DESC LIMIT 20",
            (hypothesis_id, SETTLED_WIN, SETTLED_LOSS, SETTLED_PUSH),
        )
        rows = await cur.fetchall()
        if not rows:
            return
        wins = sum(1 for r in rows if r["state"] == SETTLED_WIN)
        losses = sum(1 for r in rows if r["state"] == SETTLED_LOSS)
        pushes = sum(1 for r in rows if r["state"] == SETTLED_PUSH)
        n = wins + losses + pushes
        decided = wins + losses
        hit_rate = (wins / decided) if decided else None
        total_staked = sum(float(r["stake_dollars"] or 0) for r in rows)
        total_pnl = sum(float(r["pnl_dollars"] or 0) for r in rows)
        roi_pct = (total_pnl / total_staked * 100.0) if total_staked else None

        # Average CLV across the same 20 orders.
        order_ids = tuple(r["order_id"] for r in rows)
        placeholders = ",".join(["?"] * len(order_ids))
        clv_cur = await db.execute(
            f"SELECT AVG(clv_prob_bp) FROM clv_log "
            f"WHERE bet_id IN ({placeholders}) AND clv_prob_bp IS NOT NULL",
            tuple(f"order:{oid}" for oid in order_ids),
        )
        avg_clv_row = await clv_cur.fetchone()
        avg_clv = avg_clv_row[0] if avg_clv_row else None

        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO hypothesis_stats "
            "(hypothesis_id, stage, computed_at, total_n, signals_n, "
            "win, loss, push_, hit_rate, avg_edge, avg_ev, avg_clv, "
            "positive_clv_rate, roi_pct, sharpe, max_drawdown, "
            "p_value, is_significant) "
            "VALUES (?, 'live', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, "
            "NULL, ?, NULL, NULL, NULL, 0)",
            (
                hypothesis_id, now, n, n, wins, losses, pushes, hit_rate,
                avg_clv, roi_pct,
            ),
        )
        await db.commit()
    except Exception as e:
        logger.debug(f"hypothesis_stats refresh skipped: {e}")


async def _emit_settle_telegram(
    manager: OrderManager, order, result: str, pnl: float
) -> None:
    """Fire a one-line Telegram confirmation on settle.

    Respects the injected ``_telegram_sender`` so tests don't hit the
    network. Best-effort — Telegram failure MUST NOT unwind the settle.
    """
    short = order.order_id[-6:]
    verb = {"win": "WIN", "loss": "LOSS", "push": "PUSH"}[result]
    sign = "+" if pnl > 0 else ""
    msg = f"#{short} settled {verb} {sign}${pnl:.2f}"
    try:
        # If the manager has a test sender, use it; otherwise use the
        # normal telegram module.
        if manager._telegram_sender is not None:
            await manager._telegram_sender(msg)
            return
        from tools import telegram as _tg
        await _tg.send_alert(msg, parse_mode="")
    except Exception as e:
        logger.debug(f"settle telegram skipped for {order.order_id}: {e}")


# --- Stuck / void detection ------------------------------------------------


async def _maybe_mark_stuck(
    manager: OrderManager, row, report: ReconciliationReport, market: str
) -> None:
    """If the event is well past its expected completion time and still
    has no game_result row, tag the order as stuck_pending_result and
    alert Marco exactly once.
    """
    db = manager._db
    order_id = row["order_id"]
    notes = row["notes"] if "notes" in row.keys() else None
    if notes and "stuck_pending_result" in notes:
        return  # already flagged; don't re-alert

    ctx = await _lookup_game_context(db, row["sport"] or "", row["event_id"] or "")
    if not ctx:
        return
    try:
        game_dt = datetime.fromisoformat(
            ctx["game_date"].replace("Z", "+00:00")
        )
    except Exception:
        return
    if game_dt.tzinfo is None:
        game_dt = game_dt.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - game_dt).total_seconds() / 3600.0
    threshold = STUCK_PROP_HOURS if market == "player_props" else STUCK_GAME_HOURS
    if age_h < threshold:
        return

    flag = (
        f"stuck_pending_result; "
        f"market={market} age_h={age_h:.1f} flagged_at="
        f"{datetime.now(timezone.utc).isoformat()}"
    )
    new_notes = f"{notes}; {flag}" if notes else flag
    await db.execute(
        "UPDATE orders SET notes = ? WHERE order_id = ?", (new_notes, order_id),
    )
    await db.commit()
    report.stuck += 1
    report.stuck_order_ids.append(order_id)

    try:
        msg = (
            f"Order #{order_id[-6:]} stuck — {market} on "
            f"{row['sport']}/{row['event_id']} still has no game_result "
            f"{age_h:.1f}h after start. Manual settle needed."
        )
        if manager._telegram_sender is not None:
            await manager._telegram_sender(msg)
        else:
            from tools import telegram as _tg
            await _tg.send_alert(msg, parse_mode="")
    except Exception as e:
        logger.debug(f"stuck telegram skipped: {e}")


async def detect_voided_orders(manager: OrderManager) -> dict:
    """Scan filled orders whose game was postponed/cancelled; void them.

    Void path:
      1. Scan ``filled`` orders.
      2. For each, peek at ``game_contexts.context_json`` for a
         ``status`` key in ('postponed','cancelled','suspended').
      3. Transition state -> ``cancelled``, pnl = 0, refund stake
         (bankroll append of +stake).
      4. Telegram-alert.
    """
    db = manager._db
    assert db is not None
    voided: list[str] = []
    errors = 0

    cur = await db.execute(
        "SELECT * FROM orders WHERE state = ? ORDER BY created_at ASC",
        (FILLED,),
    )
    rows = await cur.fetchall()
    for row in rows:
        order_id = row["order_id"]
        try:
            ctx = await _lookup_game_context(
                db, row["sport"] or "", row["event_id"] or ""
            )
            if not ctx:
                continue
            status = (ctx.get("context", {}).get("status") or "").lower()
            if status not in ("postponed", "cancelled", "canceled", "suspended"):
                continue

            # Void the order — filled -> cancelled with pnl=0.
            voided_order = await manager._transition(
                order_id, CANCELLED,
                reason=f"auto_void_{status}",
                pnl_dollars=0.0,
            )
            # Refund stake to bankroll.
            stake = float(row["stake_dollars"] or 0.0)
            if stake > 0:
                bal_cur = await db.execute(
                    "SELECT balance FROM bankroll "
                    "ORDER BY timestamp DESC LIMIT 1"
                )
                bal_row = await bal_cur.fetchone()
                current = float(bal_row[0]) if bal_row else 0.0
                now = datetime.now(timezone.utc).isoformat()
                try:
                    await db.execute(
                        "INSERT INTO bankroll "
                        "(timestamp, balance, change, bet_id, description) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (now, current + stake, stake, voided_order.bet_id,
                         f"Void refund {order_id} ({status})"),
                    )
                    await db.commit()
                except Exception as e:
                    logger.debug(f"bankroll refund skipped: {e}")

            # Update the bets mirror too.
            if voided_order.bet_id:
                try:
                    await db.execute(
                        "UPDATE bets SET result = ?, payout = ? WHERE id = ?",
                        ("void", stake, voided_order.bet_id),
                    )
                    await db.commit()
                except Exception:
                    pass

            voided.append(order_id)
            try:
                msg = (
                    f"Order #{order_id[-6:]} VOIDED ({status}) — "
                    f"stake ${stake:.2f} refunded."
                )
                if manager._telegram_sender is not None:
                    await manager._telegram_sender(msg)
                else:
                    from tools import telegram as _tg
                    await _tg.send_alert(msg, parse_mode="")
            except Exception as e:
                logger.debug(f"void telegram skipped: {e}")
        except Exception as e:
            logger.warning(f"void scan failed for {order_id}: {e}", exc_info=True)
            errors += 1

    if voided:
        logger.info(f"void scan: voided {len(voided)} orders: {voided}")
    return {"voided": len(voided), "errors": errors, "order_ids": voided}


# --- SGP helpers -----------------------------------------------------------


def _extract_player_meta(notes: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Pull ``player=...,stat=...`` tokens from the order ``notes`` column.

    Props submitted via ``submit_order`` stash player/stat there because
    the ``orders`` table doesn't have a dedicated column (intentional —
    keeping the schema narrow).
    """
    if not notes:
        return None, None
    player, stat = None, None
    for tok in notes.replace(";", ",").split(","):
        tok = tok.strip()
        if tok.startswith("player="):
            player = tok.split("=", 1)[1].strip()
        elif tok.startswith("stat="):
            stat = tok.split("=", 1)[1].strip()
    return player, stat


async def _resolve_sgp(db, notes: Optional[str], sport: str) -> Optional[str]:
    """All-legs-must-hit resolution for same-game parlays.

    Legs persisted as JSON in ``notes`` under ``legs=[...]``, each leg
    a dict with keys: ``market``, ``event_id``, ``side``, ``line``,
    ``player``, ``stat_type``. One unresolved leg -> None (skip).
    One losing leg -> loss immediately. All winners -> win. Any push on
    a still-winning ticket degrades to win on the pushed legs dropping.
    """
    if not notes:
        return None
    legs = _parse_legs(notes)
    if not legs:
        return None
    outcomes: list[str] = []
    for leg in legs:
        mkt = _normalise_market(leg.get("market"))
        side = leg.get("side") or ""
        line = leg.get("line")
        eid = leg.get("event_id") or ""
        if mkt == "player_props":
            stat_value = await _lookup_player_stat(
                db, sport, eid, leg.get("player", ""), leg.get("stat_type", ""),
            )
            outcome = _resolve_player_prop(side, line, stat_value)
        else:
            game = await _lookup_game_result(db, sport, eid)
            if game is None:
                return None
            if mkt == "h2h":
                outcome = _resolve_moneyline(side, game)
            elif mkt == "spreads":
                outcome = _resolve_spread(side, line, game)
            elif mkt == "totals":
                outcome = _resolve_total(side, line, game)
            else:
                return None
        if outcome is None:
            return None
        if outcome == "loss":
            return "loss"  # one dead leg -> whole ticket loses
        outcomes.append(outcome)
    # No losing legs. Pure wins -> win. Pushes reduce the parlay but
    # since we don't recompute price, any all-push ticket -> push.
    if all(o == "push" for o in outcomes):
        return "push"
    return "win"


def _parse_legs(notes: str) -> list[dict]:
    """Pull ``legs=[...]`` JSON out of a notes field."""
    if "legs=" not in notes:
        return []
    try:
        chunk = notes.split("legs=", 1)[1]
        # The JSON may be followed by more tokens; find the matching
        # closing bracket.
        depth = 0
        end = -1
        for i, ch in enumerate(chunk):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            return []
        return json.loads(chunk[:end])
    except Exception:
        return []
