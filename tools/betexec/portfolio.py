"""Portfolio Kelly sizing orchestration (slice 3 split).

Extracted from ``BetExecutor.compute_portfolio_stakes`` in
``tools/bet_executor.py``: correlation-aware multi-bet sizing with
signals_n base-fraction dampening, per-sport regime multipliers, and the
per-game / per-sport / min-bet exposure-cap passes.

Pure orchestration over injected callables — no executor state, no DB,
no browser, no arming.
"""

import logging
from typing import Callable, Optional

from tools.betexec.config import KELLY_FRACTION, MIN_BET_AMOUNT
from tools.betexec.sizing import apply_exposure_caps, build_portfolio_requests

logger = logging.getLogger("callisto.executor")


def size_single_bet(
    b: dict,
    bankroll: float,
    *,
    stake_fn: Callable,
    kelly_fraction_fn: Callable[[int], float],
    regime_multiplier: float = 1.0,
) -> dict:
    """Size one bet with signals_n dampening + regime multiplier applied.

    ``stake_fn(edge, odds, bankroll, confidence)`` is the caller's canonical
    Kelly sizing entry point (executor.compute_stake). Returns the result
    row dict shaped exactly like the legacy single-bet branch of
    ``compute_portfolio_stakes``.
    """
    signals_n = int(b.get("signals_n", 0) or 0)
    kelly_frac = kelly_fraction_fn(signals_n)
    stake = stake_fn(
        b.get("edge", 0.0),
        b.get("odds", -110),
        bankroll,
        b.get("confidence", 0.6),
    )
    # Scale by signals_n-aware base fraction (cap-at-quarter Kelly).
    stake = (
        round(stake * (kelly_frac / KELLY_FRACTION), 2)
        if KELLY_FRACTION > 0
        else stake
    )
    pre_regime_stake = stake
    stake = round(stake * regime_multiplier, 2)
    return {
        "description": b.get("description", "Bet 1"),
        "stake": stake if stake >= MIN_BET_AMOUNT else 0.0,
        "fraction": round(stake / bankroll, 6) if bankroll > 0 else 0,
        "event_id": b.get("event_id", ""),
        "sport": b.get("sport", ""),
        "hypothesis_id": b.get("hypothesis_id", ""),
        "method": "individual_kelly_n_adjusted",
        "kelly_base_fraction": kelly_frac,
        "signals_n": signals_n,
        "regime_multiplier": regime_multiplier,
        "stake_before_regime": pre_regime_stake,
    }


def size_portfolio_bets(
    bets: list[dict],
    bankroll: float,
    correlation_matrix: Optional[dict],
    *,
    regime_multiplier_fn: Callable[[str], float],
    kelly_fraction_fn: Callable[[int], float],
    stake_fn: Callable,
) -> list[dict]:
    """Correlation-aware portfolio Kelly with caps — full multi-bet path.

    Injected callables:
      - ``regime_multiplier_fn(sport) -> float``
      - ``kelly_fraction_fn(signals_n) -> float``
      - ``stake_fn(edge, odds, bankroll, confidence) -> float``

    Returns sized result rows with per-game / per-sport caps and min-bet
    floors already applied (via tools.betexec.sizing.apply_exposure_caps).
    """
    if not bets:
        return []

    # --- Regime multipliers per sport in the batch (cached for this call) ---
    sports_in_batch = {b.get("sport", "") for b in bets if b.get("sport")}
    regime_mults: dict[str, float] = {
        sp: regime_multiplier_fn(sp) for sp in sports_in_batch
    }
    if regime_mults:
        logger.info(
            "regime_sizing: applying multipliers %s",
            {k: round(v, 3) for k, v in regime_mults.items()},
        )

    portfolio_bets, sized = build_portfolio_requests(bets, correlation_matrix)

    results: list[dict] = []
    for i, item in enumerate(sized):
        b = bets[i]
        frac = float(item.get("final_fraction", 0.0))
        signals_n = int(b.get("signals_n", 0) or 0)
        kelly_frac = kelly_fraction_fn(signals_n)
        scale = (kelly_frac / KELLY_FRACTION) if KELLY_FRACTION > 0 else 1.0
        frac = frac * scale
        stake_before_regime = round(bankroll * frac, 2)
        sport = b.get("sport", "")
        reg_mult = regime_mults.get(sport, 1.0)
        frac = frac * reg_mult
        stake = round(bankroll * frac, 2)
        if reg_mult != 1.0 and stake_before_regime > 0:
            logger.info(
                "regime_sizing: %s stake $%.2f → $%.2f (mult=%.3f sport=%s)",
                b.get("hypothesis_id", "?"), stake_before_regime, stake,
                reg_mult, sport,
            )
        results.append({
            "description": item.get("description", ""),
            "stake": stake,
            "fraction": frac,
            "correlation": item.get("correlation", 0.0),
            "tier": item.get("tier", ""),
            "event_id": b.get("event_id", ""),
            "sport": sport,
            "hypothesis_id": b.get("hypothesis_id", ""),
            "market_type": b.get("market_type", ""),
            "method": "portfolio_kelly_n_adjusted",
            "kelly_base_fraction": kelly_frac,
            "signals_n": signals_n,
            "regime_multiplier": reg_mult,
            "stake_before_regime": stake_before_regime,
            "portfolio_summary": item.get("portfolio_summary", {}),
        })

    # Second/third passes: per-game + per-sport caps, then min-bet floor.
    return apply_exposure_caps(results, bankroll)


def compute_portfolio_stakes(
    bets: list[dict],
    bankroll: float,
    correlation_matrix: Optional[dict] = None,
    *,
    regime_multiplier_fn: Callable[[str], float],
    kelly_fraction_fn: Callable[[int], float],
    stake_fn: Callable,
) -> list[dict]:
    """Dispatch single vs multi-bet sizing (legacy public entry point)."""
    if not bets:
        return []
    if len(bets) == 1:
        sport = bets[0].get("sport", "")
        reg_mult = (
            regime_multiplier_fn(sport) if sport else 1.0
        )
        return [size_single_bet(
            bets[0], bankroll,
            stake_fn=stake_fn,
            kelly_fraction_fn=kelly_fraction_fn,
            regime_multiplier=reg_mult,
        )]
    return size_portfolio_bets(
        bets, bankroll, correlation_matrix,
        regime_multiplier_fn=regime_multiplier_fn,
        kelly_fraction_fn=kelly_fraction_fn,
        stake_fn=stake_fn,
    )
