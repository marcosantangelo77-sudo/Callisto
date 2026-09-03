"""AutonomousLoop market-psychology helpers extracted from tools.auto.loop.

``AutonomousLoop._run_market_psychology``, ``_get_psychology_for_edge``, and
``get_psychology_report`` stay defined on the class as thin delegates so
slice2 ``hasattr`` pins keep passing. The bodies live here so
tools/auto/loop.py can keep shrinking without changing behaviour.

``_loop``, ``_cleanup_dedup``, and ``get_status`` stay on AutonomousLoop.
Do not import the autonomous facade (no cycles).
Do not arm live betting. Do not add live to paper-signal.
"""
from __future__ import annotations

import logging
import time

from tools.market_psychology import (
    full_market_psychology,
)

from tools.auto.loop import ANALYSIS_COOLDOWN

logger = logging.getLogger("callisto.autonomous")


def run_market_psychology(loop) -> None:
    """Run market psychology analysis on latest snapshots.

    Produces per-sport psychology signals (number shading, attention
    arbitrage) that are cached and merged into edge candidates during
    scoring.  Runs at most once per ANALYSIS_COOLDOWN to avoid waste.
    """
    self = loop
    now = time.time()
    all_reports = self.line_monitor.get_edge_report()
    if not isinstance(all_reports, dict):
        return

    for sport, report in all_reports.items():
        if not isinstance(report, dict):
            continue
        # Throttle: skip if we ran psychology for this sport recently
        last_ts = self._psychology_ts.get(sport, 0)
        if now - last_ts < ANALYSIS_COOLDOWN:
            continue

        # Get the latest snapshot games for this sport
        snapshot = self.line_monitor._snapshots.get(sport)
        if not snapshot or not snapshot.get("games"):
            continue

        try:
            psych = full_market_psychology(
                games=snapshot["games"],
                sport=sport,
            )
            self._psychology_cache[sport] = psych
            self._psychology_ts[sport] = now

            shading_count = len(psych.get("number_shading", []))
            if shading_count > 0:
                logger.info(
                    f"Psychology {sport}: {shading_count} shaded lines detected"
                )
        except Exception as e:
            logger.warning(f"Market psychology failed for {sport}: {e}")


def get_psychology_for_edge(loop, sport: str, game: str, team: str, market: str) -> dict:
    """Extract psychology signals relevant to a specific edge.

    Returns a dict with keys:
        number_shading_detected: bool
        shading_value_side: str or None
        shading_magnitude: int
        attention_opportunity: float (0-1, higher = thinner market)
    """
    self = loop
    result = {
        "number_shading_detected": False,
        "shading_value_side": None,
        "shading_magnitude": 0,
        "attention_opportunity": 0.0,
    }
    psych = self._psychology_cache.get(sport)
    if not psych:
        return result

    # Match number shading signals for this game/team/market
    for shade in psych.get("number_shading", []):
        shade_game = shade.get("game", "")
        shade_team = shade.get("team", "")
        shade_market = shade.get("market", "")
        if (shade_game == game and
                shade_team == team and
                shade_market == market):
            result["number_shading_detected"] = True
            result["shading_value_side"] = shade.get("value_side")
            result["shading_magnitude"] = shade.get("shade_magnitude_cents", 0)
            break

    # Attention arbitrage — sport-level signal
    attn = psych.get("attention_arbitrage", {})
    for thin in attn.get("thin_markets", []):
        if thin.get("sport") == sport:
            result["attention_opportunity"] = thin.get("opportunity_score", 0.0)
            break

    return result


def get_psychology_report(loop) -> dict:
    """Return the latest market psychology signals for all sports."""
    self = loop
    return {
        sport: psych for sport, psych in self._psychology_cache.items()
    }
