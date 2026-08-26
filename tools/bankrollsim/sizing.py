"""Slate sizing + bet resolution mirroring the live executor's sizing logic."""

from __future__ import annotations

from typing import Optional

from tools.bankrollsim.config import (
    SIM_MAX_BET_PCT,
    SIM_MAX_GAME_EXPOSURE_PCT,
    SIM_MAX_SPORT_EXPOSURE_PCT,
    SIM_MIN_BET_AMOUNT,
)


def _size_slate(
    signals: list[dict],
    bankroll: float,
    kelly_fraction: float,
    hyp_signal_counts: dict[str, int],
    correlation_matrix: Optional[dict[tuple[str, str], float]] = None,
) -> list[dict]:
    """Size a list of signals using portfolio-Kelly + per-game/sport caps.

    Mirrors ``BetExecutor.compute_portfolio_stakes`` without the DB writes.
    Returns a list of {stake, edge, odds, actual_result, sport, event_id,
    hypothesis_id} dicts. Stakes below SIM_MIN_BET_AMOUNT are zeroed.
    """
    if not signals:
        return []

    # Import lazily so the sim can be unit-tested without pulling in aiosqlite
    from tools.kelly import kelly_portfolio, kelly_fractional, _confidence_tier_from_score, AGP_TIER_MULTIPLIERS

    # Default confidence: mirror the executor's 0.6 (PROBABLE tier) so sim
    # sizing matches what the live path would produce.
    default_conf = 0.6
    # Corr overrides from matrix
    corr_overrides: dict[int, float] = {}
    if correlation_matrix:
        for i, si in enumerate(signals):
            hi = si["hypothesis_id"]
            pair_corrs = []
            for j, sj in enumerate(signals):
                if i == j:
                    continue
                hj = sj["hypothesis_id"]
                key = (hi, hj) if (hi, hj) in correlation_matrix else (hj, hi)
                if key in correlation_matrix:
                    pair_corrs.append(correlation_matrix[key])
            if pair_corrs:
                corr_overrides[i] = sum(pair_corrs) / len(pair_corrs)

    if len(signals) == 1:
        # Single bet: simple fractional Kelly + tier adjustment
        s = signals[0]
        frac = kelly_fractional(s["edge"], s["odds"], fraction=kelly_fraction)
        tier = _confidence_tier_from_score(default_conf)
        frac *= AGP_TIER_MULTIPLIERS.get(tier, 0.0)
        frac = min(frac, SIM_MAX_BET_PCT)
        stake = round(bankroll * frac, 2)
        if stake < SIM_MIN_BET_AMOUNT:
            stake = 0.0
        return [{**s, "stake": stake, "fraction": frac}]

    portfolio_bets = []
    for i, s in enumerate(signals):
        rho = corr_overrides.get(i, 0.1)
        portfolio_bets.append({
            "edge": s["edge"],
            "odds": s["odds"],
            "confidence_score": default_conf,
            "variance_estimate": abs(s["edge"]) * 0.5,
            "correlation_with_others": rho,
            "description": s["hypothesis_id"],
        })
    sized = kelly_portfolio(portfolio_bets)

    # Scale by kelly_fraction relative to default quarter-Kelly (0.25)
    # so sensitivity analysis actually moves the sim.
    scale = kelly_fraction / 0.25 if kelly_fraction != 0.25 else 1.0

    results = []
    for i, item in enumerate(sized):
        frac = float(item.get("final_fraction", 0.0)) * scale
        stake = round(bankroll * frac, 2)
        results.append({
            **signals[i],
            "stake": stake,
            "fraction": frac,
        })

    # Per-game cap
    game_cap = bankroll * SIM_MAX_GAME_EXPOSURE_PCT
    by_game: dict[str, list[int]] = {}
    for idx, r in enumerate(results):
        by_game.setdefault(r["event_id"], []).append(idx)
    for eid, idxs in by_game.items():
        total = sum(results[i]["stake"] for i in idxs)
        if total > game_cap and total > 0:
            ratio = game_cap / total
            for i in idxs:
                results[i]["stake"] = round(results[i]["stake"] * ratio, 2)

    # Per-sport cap
    sport_cap = bankroll * SIM_MAX_SPORT_EXPOSURE_PCT
    by_sport: dict[str, list[int]] = {}
    for idx, r in enumerate(results):
        sp = r.get("sport") or ""
        if not sp:
            continue
        by_sport.setdefault(sp, []).append(idx)
    for sp, idxs in by_sport.items():
        total = sum(results[i]["stake"] for i in idxs)
        if total > sport_cap and total > 0:
            ratio = sport_cap / total
            for i in idxs:
                results[i]["stake"] = round(results[i]["stake"] * ratio, 2)

    # Floor
    for r in results:
        if r["stake"] < SIM_MIN_BET_AMOUNT:
            r["stake"] = 0.0

    return results


def _resolve_bets(bets: list[dict]) -> float:
    """Given a list of sized bets with actual_result, return net P&L."""
    pnl = 0.0
    for b in bets:
        stake = b["stake"]
        if stake <= 0:
            continue
        odds = b["odds"]
        if b["actual_result"] == "won":
            if odds > 0:
                pnl += stake * (odds / 100.0)
            else:
                pnl += stake * (100.0 / abs(odds))
        elif b["actual_result"] == "lost":
            pnl -= stake
        # push: 0
    return pnl
