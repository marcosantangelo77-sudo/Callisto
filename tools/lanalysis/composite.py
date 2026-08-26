"""Composite line analysis — run all components and return a unified report."""

from typing import Optional

from tools.lanalysis.decomposition import decompose_movement
from tools.lanalysis.priority import ev_of_analysis
from tools.lanalysis.public import (
    contrarian_value,
    estimate_public_side,
)
from tools.lanalysis.rlm import detect_rlm
from tools.lanalysis.steam import detect_steam
from tools.lanalysis.timing import optimal_bet_timing


def full_line_analysis(
    line_history: list[dict],
    sport: str,
    line_open: float,
    line_current: float,
    public_ticket_pct: Optional[float] = None,
    public_money_pct: Optional[float] = None,
    is_primetime: bool = False,
    is_rivalry: bool = False,
    team_a: str = "",
    team_b: str = "",
    hours_to_game: float = 24.0,
    day_of_week: str = "sunday",
    game_data: Optional[dict] = None,
) -> dict:
    """
    Run all line analysis components and return a unified report.

    This is the main entry point — call with whatever data is available.
    Components that lack required data will be skipped gracefully.
    """
    report: dict = {
        "sport": sport,
        "team_a": team_a,
        "team_b": team_b,
        "line_open": line_open,
        "line_current": line_current,
    }

    # 1. Decomposition
    if line_history and len(line_history) >= 3:
        report["decomposition"] = decompose_movement(line_history, sport)
    else:
        report["decomposition"] = {"note": "Insufficient line history for decomposition (need 3+ points)"}

    # 2. RLM detection
    line_movement_direction = line_current - line_open
    if public_ticket_pct is not None and public_money_pct is not None:
        report["rlm"] = detect_rlm(line_movement_direction, public_ticket_pct, public_money_pct)
    else:
        # Use estimated public side
        est = estimate_public_side(
            line_open, line_current, sport, is_primetime, is_rivalry,
            team_a, team_b,
        )
        report["public_estimate"] = est
        # Synthesize an RLM check with estimated data
        est_ticket = est["estimated_public_pct_a"]
        est_money = est_ticket * 0.85  # Public money % is typically less skewed than tickets
        report["rlm"] = detect_rlm(line_movement_direction, est_ticket, est_money)
        report["rlm"]["note"] = "Based on estimated (not actual) public percentages"

    # 3. Steam moves (requires multi-book snapshots — just check if history qualifies)
    if line_history and len(line_history) >= 4:
        books_in_history = set(e.get("book", "") for e in line_history)
        if len(books_in_history) >= 2:
            report["steam_moves"] = detect_steam(line_history)
        else:
            report["steam_moves"] = {"note": "Need multi-book snapshots for steam detection"}
    else:
        report["steam_moves"] = {"note": "Insufficient data for steam detection"}

    # 4. Timing
    report["timing"] = optimal_bet_timing(sport, "spreads", day_of_week, hours_to_game)

    # 5. Public estimate (if not already computed)
    if "public_estimate" not in report:
        report["public_estimate"] = estimate_public_side(
            line_open, line_current, sport, is_primetime, is_rivalry,
            team_a, team_b,
        )

    # 6. Contrarian value
    public_popular_pct = max(
        report["public_estimate"]["estimated_public_pct_a"],
        report["public_estimate"]["estimated_public_pct_b"],
    )
    report["contrarian"] = contrarian_value(public_popular_pct, sport, line_current)

    # 7. EV of further analysis
    if game_data:
        report["analysis_priority"] = ev_of_analysis(game_data)
    else:
        # Build minimal game_data from what we have
        synthetic_game_data = {
            "line_movement": abs(line_movement_direction),
            "hours_to_game": hours_to_game,
            "is_primetime": is_primetime,
            "sport": sport,
            "estimated_public_pct": public_popular_pct,
        }
        report["analysis_priority"] = ev_of_analysis(synthetic_game_data)

    # Summary
    signals = []
    if report.get("rlm", {}).get("is_rlm"):
        signals.append(f"RLM ({report['rlm']['strength']})")
    if isinstance(report.get("steam_moves"), list) and report["steam_moves"]:
        signals.append(f"Steam moves ({len(report['steam_moves'])})")
    decomp = report.get("decomposition", {})
    if isinstance(decomp, dict) and decomp.get("signal_to_noise", 0) > 3:
        signals.append(f"Clean sharp trend (SNR={decomp['signal_to_noise']:.1f})")
    if report.get("contrarian", {}).get("adjusted_roi", 0) > 2:
        signals.append(f"Contrarian value ({report['contrarian']['adjusted_roi']:+.1f}% ROI)")

    report["summary"] = {
        "signals_detected": signals,
        "signal_count": len(signals),
        "overall_assessment": (
            "STRONG — multiple confirming signals" if len(signals) >= 3
            else "MODERATE — some signals present" if len(signals) >= 1
            else "WEAK — no strong signals detected"
        ),
    }

    return report
