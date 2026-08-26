"""tools.betexec.execution — the full bet-execution pipeline.

Slice-4 split (2026-08): ``BetExecutor.execute_bet`` moved here as
``run_execute_bet``. The function receives every dependency it needs as a
parameter (db, bankroll-lock, sizing fn, preflight fn, browser hooks,
record/log fns), so the facade method is now a thin adapter that binds the
executor's live attributes.

Safety invariants carried over verbatim:
  - The read-bankroll → size → exposure-check sequence runs under the
    caller-supplied ``bankroll_lock`` (audit H-1/H-4) so concurrent
    placements cannot both decide they have full bankroll available.
  - Preflight (including enablement) still gates before any browser work;
    callers must keep ``_enabled`` False unless explicitly armed via
    ``enable()`` — which refuses under CALLISTO_LOCAL_ONLY.

No Playwright import happens in this module: navigation and slip placement
are callables supplied by the caller (the real executor wires them to
``tools.betexec.browser`` / ``tools.betexec.slip``; tests wire fakes).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from tools.betexec.config import MAX_OPEN_EXPOSURE_PCT, MIN_BET_AMOUNT

logger = logging.getLogger("callisto.executor")


def build_selection_or_none(market: str, team: str, side: str, point=None):
    """Build the DraftKings selection text for a market (delegating helper).

    Kept here so the execution pipeline has a single seam for selection-text
    construction; delegates to tools.betexec.slip.build_selection_text.
    """
    from tools.betexec.slip import build_selection_text  # local: avoids cycle at import time

    return build_selection_text(market, team, side, point)


async def resolve_stake(
    stake_override: Optional[float],
    edge: float,
    odds: int,
    bankroll: float,
    confidence: float,
    compute_stake_fn: Callable[..., float],
) -> float:
    """Pick the effective stake for a placement attempt.

    Respects a pre-computed portfolio stake when provided (feat/
    portfolio-kelly-live-loop audit 2026-04-22): recomputing Kelly would undo
    the correlation/exposure-cap work already applied by the portfolio pass.
    """
    if stake_override is not None and stake_override > 0:
        return round(float(stake_override), 2)
    return compute_stake_fn(edge, odds, bankroll, confidence)


async def _peek_bankroll(db) -> float:
    from tools.betexec.db_state import get_bankroll

    return await get_bankroll(db)


async def run_execute_bet(
    *,
    db,
    bankroll_lock,
    enabled: bool,
    sport: str,
    team: str,
    market: str,
    side: str,
    odds: int,
    fair_prob: float,
    edge: float,
    hypothesis_id: str = "",
    event_id: str = "",
    game_description: str = "",
    confidence: float = 0.6,
    point: Optional[float] = None,
    stake_override: Optional[float] = None,
    # --- dependency seams ---
    compute_stake_fn: Callable[..., float],
    preflight_fn: Callable[..., Awaitable[tuple[bool, str]]],
    ensure_logged_in_fn: Callable[[], Awaitable[bool]],
    navigate_fn: Callable[..., Awaitable[bool]],
    place_fn: Callable[[str, float], Awaitable[dict]],
    record_bet_fn: Callable[..., Awaitable[int]],
    log_action_fn: Callable[..., Awaitable],
    notify_fn: Optional[Callable[[str], None]] = None,
    build_message_fn: Optional[Callable[..., str]] = None,
) -> dict:
    """Full bet execution pipeline: size → cap → preflight → navigate → place → record.

    Returns an execution result dict with success/reason/stake/screenshot.
    Never raises for business failures; only infrastructure errors propagate.
    """
    async with bankroll_lock:
        from tools.betexec.db_state import get_bankroll, get_open_exposure

        bankroll = await get_bankroll(db)

        stake = await resolve_stake(
            stake_override, edge, odds, bankroll, confidence, compute_stake_fn
        )
        if stake <= 0:
            return {"success": False, "reason": "Stake too small after Kelly sizing"}

        # Portfolio-level cap: refuse or shrink to remaining headroom.
        open_exposure = await get_open_exposure(db)
        exposure_cap = bankroll * MAX_OPEN_EXPOSURE_PCT
        if open_exposure + stake > exposure_cap:
            room = max(0.0, exposure_cap - open_exposure)
            if room < MIN_BET_AMOUNT:
                await log_action_fn(
                    "EXPOSURE_CAP", sport, team, market, side, odds, stake, edge,
                    hypothesis_id,
                    reason=(
                        f"Open exposure ${open_exposure:.2f} + stake ${stake:.2f} "
                        f"> cap ${exposure_cap:.2f}"
                    ),
                )
                return {
                    "success": False,
                    "reason": (
                        f"Portfolio exposure cap hit: ${open_exposure:.2f} pending + "
                        f"${stake:.2f} would exceed {MAX_OPEN_EXPOSURE_PCT:.0%} of bankroll"
                    ),
                }
            stake = round(room, 2)

    # Preflight checks (enablement gate lives inside the caller's preflight).
    ok, reason = await preflight_fn(sport=sport, odds=odds, edge=edge, stake=stake)
    if not ok:
        await log_action_fn(
            "PREFLIGHT_FAIL", sport, team, market, side, odds, stake, edge,
            hypothesis_id, reason=reason,
        )
        return {"success": False, "reason": reason}

    # Ensure browser session exists + logged in.
    if not await ensure_logged_in_fn():
        return {
            "success": False,
            "reason": "Not logged into DraftKings — manual login required",
        }

    # Navigate to game.
    found = await navigate_fn(sport, team, event_id)
    if not found:
        await log_action_fn(
            "NAV_FAIL", sport, team, market, side, odds, stake, edge,
            hypothesis_id, reason="Game not found on DK",
        )
        return {"success": False, "reason": f"Could not find {team} game on DraftKings"}

    # Build selection text based on market type.
    selection_text = build_selection_or_none(market, team, side, point)

    # Place the bet.
    placement = await place_fn(selection_text, stake)

    if placement.get("success"):
        bet_id = await record_bet_fn(
            sport=sport,
            event_id=event_id,
            game_description=game_description,
            team=team,
            market=market,
            bookmaker="DraftKings",
            odds=odds,
            point=point,
            stake=stake,
            edge=edge,
            fair_prob=fair_prob,
            hypothesis_id=hypothesis_id,
        )

        await log_action_fn(
            "BET_PLACED", sport, team, market, side, odds, stake, edge,
            hypothesis_id, bet_id=bet_id,
            screenshot=placement.get("screenshot"),
        )

        # Best-effort Telegram notification.
        try:
            if notify_fn is not None and build_message_fn is not None:
                msg = build_message_fn(
                    game_description=game_description,
                    team=team,
                    side=side,
                    odds=odds,
                    stake=stake,
                    edge=edge,
                    bankroll=bankroll,
                )
                notify_fn(msg)
        except Exception as e:  # noqa: BLE001 — notification is best-effort
            logger.warning(f"Telegram bet notification failed: {e}")

        return {
            "success": True,
            "bet_id": bet_id,
            "stake": stake,
            "odds": odds,
            "edge": edge,
            "screenshot": placement.get("screenshot"),
        }

    await log_action_fn(
        "BET_FAILED", sport, team, market, side, odds, stake, edge,
        hypothesis_id, reason=placement.get("error"),
        screenshot=placement.get("screenshot"),
    )
    return {
        "success": False,
        "reason": placement.get("error"),
        "screenshot": placement.get("screenshot"),
    }
