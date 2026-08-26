"""Decomposed TCI signal generators (experience ratio, stability score).

Backtest evidence: composite TCI is flat (51.9%), but sub-components have
predictive power when isolated and filtered by differential magnitude.
"""

from tools.tciscrape.constants import (
    EXP_RATIO_MIN_DIFF,
    EXP_RATIO_STRONG_DIFF,
    STAB_SCORE_MIN_DIFF,
)


def get_experience_signal(
    home_data: dict, away_data: dict,
    min_diff: float = EXP_RATIO_MIN_DIFF,
) -> dict:
    """
    Generate experience ratio signal for a matchup.

    Backtest: 59.6% win rate, +13.8% ROI, p=0.17 (strongest TCI sub-signal).
    Only fires when |differential| >= min_diff (default 10).

    Experience ratio = upperclassmen % (juniors + seniors + grad students).
    Higher experience -> better tournament ATS performance.
    """
    home_exp = home_data.get("experience_ratio", 0)
    away_exp = away_data.get("experience_ratio", 0)

    # Scale to 0-100 for meaningful differential
    diff = round((home_exp - away_exp) * 100, 1)
    abs_diff = abs(diff)

    if abs_diff < min_diff:
        return {
            "fires": False,
            "reason": f"|diff|={abs_diff:.1f} < threshold {min_diff}",
            "differential": diff,
            "home_experience_ratio": round(home_exp, 3),
            "away_experience_ratio": round(away_exp, 3),
        }

    side = "home" if diff > 0 else "away"
    # Confidence tiers based on differential magnitude
    if abs_diff >= EXP_RATIO_STRONG_DIFF:
        confidence = "high"
        backtest_win_rate = 0.667  # 66.7% at |diff| >= 15
    else:
        confidence = "medium"
        backtest_win_rate = 0.571  # 57.1% at |diff| >= 10

    return {
        "fires": True,
        "side": side,
        "differential": diff,
        "abs_differential": abs_diff,
        "confidence": confidence,
        "backtest_win_rate": backtest_win_rate,
        "home_experience_ratio": round(home_exp, 3),
        "away_experience_ratio": round(away_exp, 3),
        "home_upperclassmen": home_data.get("upperclassmen", 0),
        "home_underclassmen": home_data.get("underclassmen", 0),
        "away_upperclassmen": away_data.get("upperclassmen", 0),
        "away_underclassmen": away_data.get("underclassmen", 0),
        "signal_type": "ncaaw_experience_ratio_ats",
    }


def get_stability_signal(
    home_data: dict, away_data: dict,
    min_diff: float = STAB_SCORE_MIN_DIFF,
) -> dict:
    """
    Generate stability score signal for a matchup.

    Backtest: 57.7% win rate, +10.1% ROI, p=0.27 (second-strongest TCI sub-signal).
    Stability = coaching tenure + roster continuity proxy + institutional factor.
    Only fires when |differential| >= min_diff.
    """
    home_stab = home_data.get("stability_score", 0)
    away_stab = away_data.get("stability_score", 0)

    diff = round(home_stab - away_stab, 1)
    abs_diff = abs(diff)

    if abs_diff < min_diff:
        return {
            "fires": False,
            "reason": f"|diff|={abs_diff:.1f} < threshold {min_diff}",
            "differential": diff,
            "home_stability_score": home_stab,
            "away_stability_score": away_stab,
        }

    side = "home" if diff > 0 else "away"

    return {
        "fires": True,
        "side": side,
        "differential": diff,
        "abs_differential": abs_diff,
        "confidence": "medium",
        "backtest_win_rate": 0.577,  # 57.7%
        "home_stability_score": home_stab,
        "away_stability_score": away_stab,
        "home_coaching_tenure": home_data.get("coaching_tenure_years", 0),
        "away_coaching_tenure": away_data.get("coaching_tenure_years", 0),
        "home_continuity_proxy": home_data.get("continuity_proxy", 0),
        "away_continuity_proxy": away_data.get("continuity_proxy", 0),
        "signal_type": "ncaaw_stability_score_ats",
    }
