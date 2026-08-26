"""Kelly sizing helpers extracted from ``tools/bet_executor``.

Pure arithmetic — no DB, no browser. The facade (``tools.bet_executor``)
delegates to these and keeps the public method surface unchanged.
"""

import logging
from typing import Optional

from tools.betexec.config import (
    FULL_QUARTER_KELLY_FRACTION,
    HALF_KELLY_FRACTION,
    KELLY_FRACTION,
    MAX_BET_PCT,
    MAX_GAME_EXPOSURE_PCT,
    MAX_SPORT_EXPOSURE_PCT,
    MIN_BET_AMOUNT,
    VAR_DAMPENER_HIGH_N,
    VAR_DAMPENER_LOW_N,
)

logger = logging.getLogger("callisto.executor")


def signals_n_to_kelly_fraction(signals_n: int) -> float:
    """Map observed-signals count to Kelly base fraction.

    feat/portfolio-kelly-live-loop (audit 2026-04-22): half-Kelly for
    hypotheses with fewer than VAR_DAMPENER_LOW_N signals, full quarter-
    Kelly once they cross VAR_DAMPENER_HIGH_N. Linear interp between.
    """
    if signals_n <= VAR_DAMPENER_LOW_N:
        return HALF_KELLY_FRACTION  # half-Kelly relative to quarter-Kelly floor
    if signals_n >= VAR_DAMPENER_HIGH_N:
        return FULL_QUARTER_KELLY_FRACTION  # full quarter-Kelly
    # Linear interpolation between 0.125 and 0.25
    span = max(1, VAR_DAMPENER_HIGH_N - VAR_DAMPENER_LOW_N)
    t = (signals_n - VAR_DAMPENER_LOW_N) / span
    return HALF_KELLY_FRACTION + t * (
        FULL_QUARTER_KELLY_FRACTION - HALF_KELLY_FRACTION
    )


def compute_stake(
    edge: float,
    odds: int,
    bankroll: float,
    confidence: float = 0.6,
    p_push: float = 0.0,
    variance_estimate: Optional[float] = None,
) -> float:
    """
    Compute bet stake using dynamic Kelly with AGP confidence tiers,
    uncertainty adjustment, and push-aware sizing.

    Uses kelly_dynamic (confidence + variance aware) as the primary sizer.
    Falls back to kelly_with_push for spread bets where push is possible.
    Applies uncertainty_adjusted_kelly when confidence is below VERIFIED tier.

    Returns dollar amount to wager (0 if bet should be skipped).
    """
    # Canonical Kelly module is tools.kelly; tools.sizing only provides
    # push-aware helpers with no canonical equivalent.
    from tools.kelly import kelly_dynamic
    from tools.sizing import kelly_with_push, uncertainty_adjusted_kelly

    # Default variance_estimate: half the edge magnitude
    if variance_estimate is None:
        variance_estimate = abs(edge) * 0.5

    # For spread bets with push probability, use push-aware Kelly
    if p_push > 0:
        from tools.math_utils import american_to_decimal
        decimal_odds = american_to_decimal(odds)
        from tools.odds_api import calculate_implied_probability
        implied = calculate_implied_probability(odds)
        fair_prob = implied + edge

        fk = kelly_with_push(fair_prob, p_push, decimal_odds)

        # Map confidence score to string tier for uncertainty adjustment
        if confidence >= 0.90:
            conf_str = "high"
        elif confidence >= 0.55:
            conf_str = "medium"
        else:
            conf_str = "low"

        # Apply uncertainty adjustment for non-verified edges
        adjusted = uncertainty_adjusted_kelly(fk, edge, conf_str)
        stake_fraction = min(adjusted, MAX_BET_PCT)
        stake = round(bankroll * stake_fraction, 2)

        if stake < MIN_BET_AMOUNT:
            return 0.0
        return stake

    # Primary path: kelly_dynamic integrates AGP confidence tiers,
    # variance dampening, and hard caps in one call
    result = kelly_dynamic(
        edge=edge,
        odds=odds,
        confidence_score=confidence,
        variance_estimate=variance_estimate,
        bankroll=bankroll,
        kelly_base_fraction=KELLY_FRACTION,
    )

    stake = result["stake"]

    # Additional cap at max bet percentage of bankroll
    max_stake = bankroll * MAX_BET_PCT
    if stake > max_stake:
        stake = round(max_stake, 2)

    # Floor
    if stake < MIN_BET_AMOUNT:
        return 0.0

    return stake


def _scale_group_cap(
    results: list[dict],
    key_field: str,
    cap_amount: float,
    scale_field: str,
) -> None:
    """In-place per-group exposure cap pass.

    Groups ``results`` by ``r[key_field]``; if a group's summed stake exceeds
    ``cap_amount``, every member's stake/fraction is scaled down proportionally
    and the applied factor recorded in ``r[scale_field]``.
    """
    groups: dict[str, list[int]] = {}
    for idx, r in enumerate(results):
        k = r.get(key_field) or ""
        if not k:
            continue
        groups.setdefault(k, []).append(idx)
    for _k, idxs in groups.items():
        total = sum(results[i]["stake"] for i in idxs)
        if total > cap_amount and total > 0:
            scale = cap_amount / total
            for i in idxs:
                results[i]["stake"] = round(results[i]["stake"] * scale, 2)
                results[i]["fraction"] = results[i]["fraction"] * scale
                results[i][scale_field] = round(scale, 4)


def apply_exposure_caps(results: list[dict], bankroll: float) -> list[dict]:
    """Apply per-game then per-sport caps and the min-bet floor (in place).

    Second and third passes of the former
    ``BetExecutor.compute_portfolio_stakes``, plus the final floor pass.
    """
    # Per-game exposure cap.
    game_cap = bankroll * MAX_GAME_EXPOSURE_PCT
    _scale_group_cap(results, "event_id", game_cap, "game_cap_scale")

    # Per-sport exposure cap.
    sport_cap = bankroll * MAX_SPORT_EXPOSURE_PCT
    _scale_group_cap(results, "sport", sport_cap, "sport_cap_scale")

    # Floor below MIN_BET_AMOUNT.
    for r in results:
        if r["stake"] < MIN_BET_AMOUNT:
            r["stake"] = 0.0

    return results


def build_portfolio_requests(bets: list[dict], correlation_matrix: Optional[dict] = None):
    """Translate raw bet dicts into kelly_portfolio request dicts.

    If a correlation matrix was passed, override per-bet
    ``correlation_with_others`` with the average pairwise correlation of each
    bet with every other bet in the batch — correlations derived from
    historical co-firing (per audit). Returns (portfolio_bets, sized_output).
    """
    from tools.kelly import kelly_portfolio

    corr_overrides: dict[int, float] = {}
    if correlation_matrix:
        n = len(bets)
        for i, bi in enumerate(bets):
            hi = bi.get("hypothesis_id", "")
            if not hi:
                continue
            pair_corrs = []
            for j, bj in enumerate(bets):
                if i == j:
                    continue
                hj = bj.get("hypothesis_id", "")
                if not hj:
                    continue
                key = (hi, hj) if (hi, hj) in correlation_matrix else (hj, hi)
                if key in correlation_matrix:
                    pair_corrs.append(correlation_matrix[key])
            if pair_corrs:
                corr_overrides[i] = sum(pair_corrs) / len(pair_corrs)

    portfolio_bets = []
    for i, b in enumerate(bets):
        rho = corr_overrides.get(i, b.get("correlation_with_others", 0.1))
        portfolio_bets.append({
            "edge": b.get("edge", 0.0),
            "odds": b.get("odds", -110),
            "confidence_score": b.get("confidence", 0.6),
            "variance_estimate": abs(b.get("edge", 0.01)) * 0.5,
            "correlation_with_others": rho,
            "description": b.get("description", ""),
        })

    return portfolio_bets, kelly_portfolio(portfolio_bets)
