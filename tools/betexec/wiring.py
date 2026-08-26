"""tools.betexec.wiring — dependency binding for the execution pipeline.

Slice-5 split (2026-08): the large keyword-argument block that
``BetExecutor.execute_bet`` used to build inline moved here as
``bind_execution_pipeline``. It returns exactly the kwargs that
``tools.betexec.execution.run_execute_bet`` consumes, wiring the executor's
live state (db handle, bankroll lock, enabled flag) and its browser /
recording / notification hooks.

Pure binding — no DB access, no browser work, no arming. The legacy
short-circuit lives in :func:`ensure_logged_in_shortcircuit`: an
already-known-good session never touches the browser (matches the pre-split
facade check).
"""

from __future__ import annotations

from typing import Awaitable, Callable

from tools.betexec.notify import build_bet_placed_message


def ensure_logged_in_shortcircuit(
    executor,
    ensure_fn: Callable[[object], Awaitable[bool]],
) -> Callable[[], Awaitable[bool]]:
    """Wrap the executor's ensure_logged_in with the legacy short-circuit."""

    async def _wrapped() -> bool:
        if executor._logged_in:
            return True
        return await ensure_fn(executor)

    return _wrapped


def bind_execution_pipeline(
    executor,
    *,
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
    point=None,
    stake_override=None,
) -> dict:
    """Assemble the full ``run_execute_bet`` kwargs for this executor."""
    from tools.betexec.session import ensure_logged_in as session_ensure_logged_in

    return dict(
        db=executor._db,
        bankroll_lock=executor._bankroll_lock,
        enabled=executor._enabled,
        sport=sport,
        team=team,
        market=market,
        side=side,
        odds=odds,
        fair_prob=fair_prob,
        edge=edge,
        hypothesis_id=hypothesis_id,
        event_id=event_id,
        game_description=game_description,
        confidence=confidence,
        point=point,
        stake_override=stake_override,
        compute_stake_fn=executor.compute_stake,
        preflight_fn=executor.preflight_check,
        ensure_logged_in_fn=ensure_logged_in_shortcircuit(
            executor, session_ensure_logged_in
        ),
        navigate_fn=lambda s, t, e="": _navigate(executor, s, t),
        place_fn=lambda sel, stake: _place(executor, sel, stake),
        record_bet_fn=_record_bet_binding(executor),
        log_action_fn=_log_action_binding(executor),
        notify_fn=getattr(executor, "_notify", None),
        build_message_fn=build_bet_placed_message,
    )


async def _navigate(executor, sport: str, team: str) -> bool:
    from tools.betexec.session import navigate_to_game

    return await navigate_to_game(executor, sport, team)


async def _place(executor, selection_text: str, stake: float) -> dict:
    from tools.betexec.session import place_bet_on_slip

    return await place_bet_on_slip(executor, selection_text, stake)


def _record_bet_binding(executor):
    async def _record(**kwargs):
        return await executor._record_bet(**kwargs)

    return _record


def _log_action_binding(executor):
    async def _log(*args, **kwargs):
        await executor._log_action(*args, **kwargs)

    return _log
