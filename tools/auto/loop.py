"""
AutonomousLoop — real-time edge detection loop (extracted from autonomous.py).

The facade at tools/autonomous.py re-exports this class so existing
``from tools.autonomous import AutonomousLoop`` callers keep working.
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger("callisto.autonomous")

# Map odds-API sport keys to injury_model sport codes
_SPORT_TO_MODEL = {
    "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL",
    "baseball_mlb": "MLB",
    "basketball_ncaab": "NBA",  # model tables work for college too
    "americanfootball_ncaaf": "NFL",
    "icehockey_nhl": "NHL",
}

# Only analyze edges above these thresholds — don't waste GPU on noise
# Lowered from 4%/3% — with 3-5 scraped books, legitimate edges start at 2%
MIN_IMPLIED_RANGE = 0.02       # 2% cross-book disagreement minimum
MIN_SOFT_EDGE_VS_SHARP = 0.02  # 2% vs sharp consensus minimum
MIN_CONFIDENCE_TO_ALERT = 0.40 # Alert at moderate confidence

# Cooldown between full analysis cycles (seconds)
ANALYSIS_COOLDOWN = 120  # 2 min between analysis runs

# Don't re-analyze the same edge within this window
EDGE_DEDUP_WINDOW = 1800  # 30 minutes


class AutonomousLoop:
    """Proactive reasoning engine — turns raw edges into analyzed recommendations."""

    def __init__(self, orchestrator, line_monitor):
        """
        Args:
            orchestrator: The Orchestrator instance (has run_session())
            line_monitor: The LineMonitor instance (has edge reports)
        """
        self.orchestrator = orchestrator
        self.line_monitor = line_monitor
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._analyzed_edges: dict[str, float] = {}  # edge_key -> timestamp
        self._session_count = 0
        self._alert_count = 0
        self._loop_cycle = 0  # cycle counter for periodic parlay scans
        self._parlay_scan_cache: dict[str, dict] = {}  # sport -> latest parlay scan results (keyed by sport, max ~10 entries)
        self._parlay_scan_ts: dict[str, float] = {}    # sport -> last scan timestamp
        self._psychology_cache: dict[str, dict] = {}  # sport -> latest psychology signals (keyed by sport, max ~10 entries)
        self._psychology_ts: dict[str, float] = {}    # sport -> last run timestamp
        self._injury_cache: dict[str, dict] = {}      # sport -> injury report from ESPN
        self._injury_ts: dict[str, float] = {}         # sport -> last fetch timestamp
        self._injury_analysis_cache: dict[str, dict] = {}  # "sport:game" -> injury analysis results (capped at 50)

    async def start(self) -> None:
        """Start the autonomous reasoning loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Autonomous reasoning loop started")

    async def stop(self) -> None:
        """Stop the loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(
            f"Autonomous loop stopped — {self._session_count} sessions, "
            f"{self._alert_count} alerts sent"
        )

    async def _loop(self) -> None:
        """Main loop — find edges, reason about them, alert if worthy."""
        from tools.auto.loop_run import run_loop
        return await run_loop(self)

    def _run_market_psychology(self) -> None:
        """Run market psychology analysis on latest snapshots.

        Produces per-sport psychology signals (number shading, attention
        arbitrage) that are cached and merged into edge candidates during
        scoring.  Runs at most once per ANALYSIS_COOLDOWN to avoid waste.
        """
        from tools.auto.loop_psych import run_market_psychology
        return run_market_psychology(self)

    def _get_psychology_for_edge(self, sport: str, game: str, team: str, market: str) -> dict:
        """Extract psychology signals relevant to a specific edge.

        Returns a dict with keys:
            number_shading_detected: bool
            shading_value_side: str or None
            shading_magnitude: int
            attention_opportunity: float (0-1, higher = thinner market)
        """
        from tools.auto.loop_psych import get_psychology_for_edge
        return get_psychology_for_edge(self, sport, game, team, market)

    def _get_pace_model_confirmation(self, sport: str, game_name: str, report: dict) -> dict:
        """Check if pace model independently confirms a total edge direction.

        Returns dict with pace_model_confirms (bool), pace_model_direction,
        pace_model_edge_pct, and pace_model_total.
        """
        result = {
            "pace_model_confirms": False,
            "pace_model_direction": None,
            "pace_model_edge_pct": 0.0,
            "pace_model_total": None,
        }
        pace_edges = report.get("pace_model_totals", [])
        for pe in pace_edges:
            if pe.get("game") == game_name:
                result["pace_model_direction"] = pe.get("direction")
                result["pace_model_edge_pct"] = pe.get("edge_pct", 0.0)
                result["pace_model_total"] = pe.get("model_total")
                # Confirms if both cross-book and pace model agree on direction
                # (caller compares this with the cross-book edge direction)
                result["pace_model_confirms"] = True
                break
        return result

    # ---- Injury model integration ----

    async def _refresh_injury_cache(self, sport: str) -> dict:
        """Fetch and cache injury data for a sport. Returns cached injuries."""
        now = time.time()
        if now - self._injury_ts.get(sport, 0) < 300:
            return self._injury_cache.get(sport, {})
        try:
            from tools.contextual_data import get_injuries as _fetch_inj
            data = await _fetch_inj(sport)
            if data and not data.get("error"):
                self._injury_cache[sport] = data
                self._injury_ts[sport] = now
                cnt = data.get("injury_count", 0)
                if cnt:
                    logger.info(f"Injury cache refreshed for {sport}: {cnt} injuries")
            return self._injury_cache.get(sport, {})
        except Exception as e:
            logger.warning(f"Injury cache refresh failed for {sport}: {e}")
            return self._injury_cache.get(sport, {})

    def _get_injuries_for_game(self, sport: str, game_name: str) -> list[dict]:
        """Extract injuries relevant to a specific game from cache."""
        injuries = self._injury_cache.get(sport, {}).get("injuries", [])
        if not injuries or not game_name:
            return []
        game_lower = game_name.lower()
        relevant = []
        for inj in injuries:
            team = inj.get("team", "")
            team_abbr = inj.get("team_abbr", "")
            status = (inj.get("status") or "").lower()
            if status not in ("out", "doubtful", "questionable"):
                continue
            if (team.lower() in game_lower
                    or team_abbr.lower() in game_lower
                    or any(w in game_lower for w in team.lower().split() if len(w) > 3)):
                relevant.append(inj)
        return relevant

    def _run_injury_analysis_for_edge(self, sport: str, game_name: str,
                                       team_name: str) -> dict:
        """Run injury model on injuries relevant to an edge candidate.

        Returns dict with keys: has_injury_edge, injury_analyses,
        market_adjustment_summary, confidence_modifier (-0.10..+0.10),
        is_contrarian, prop_opportunities.
        """
        from tools.auto.loop_edge import run_injury_analysis_for_edge
        return run_injury_analysis_for_edge(self, sport, game_name, team_name)

    # ---- Line analysis signal computation ----

    def _compute_line_analysis_signals(
        self, sport: str, edge: dict, market: str, game: str, team: str,
    ) -> dict:
        """Compute line analysis signals for an edge candidate.

        Returns kwargs dict suitable for passing directly to score_edge().
        Signals: dead number, key number, public side, contrarian, RLM, steam.
        """
        from tools.auto.loop_edge import compute_line_analysis_signals
        return compute_line_analysis_signals(self, sport, edge, market, game, team)

    def _find_analysis_candidates(self) -> list[dict]:
        """
        Scan latest edge reports for candidates worth full AGP analysis.

        Filters:
        - Implied range >= 4% (real disagreement, not noise)
        - Has soft book edges vs sharp consensus >= 3%
        - Not analyzed in the last 30 minutes
        - For totals: pace model confirmation is attached as supplementary signal
        """
        from tools.auto.loop_candidates import find_analysis_candidates
        return find_analysis_candidates(self)

    async def _analyze_edge(self, candidate: dict) -> None:
        """
        Run full AGP session on an edge candidate.

        The Architect gets the edge data as a structured query and can use
        tools (injuries, props, cross-book data) to build a complete picture.
        Worthy edges alert via telegram.alert_edge.
        """
        from tools.auto.loop_candidates import analyze_edge
        return await analyze_edge(self, candidate)

    async def _phase_parlay_correlation_scan(self) -> None:
        """Scan for correlated parlay edges across all monitored sports.

        Uses build_correlated_parlay() on games with existing single-game edges
        to check if correlated legs amplify the edge into a stronger parlay play.
        """
        from tools.auto.loop_candidates import phase_parlay_correlation_scan
        return await phase_parlay_correlation_scan(self)


    def get_parlay_scan_report(self) -> dict:
        """Return the latest parlay/SGP correlation scan results."""
        return dict(self._parlay_scan_cache)

    def _cleanup_dedup(self) -> None:
        """Remove old entries from the dedup and injury analysis caches."""
        from tools.auto.loop_status import cleanup_dedup
        return cleanup_dedup(self)

    def get_status(self) -> dict:
        """Return loop status."""
        from tools.auto.loop_status import get_status as _get_status
        return _get_status(self)

    def get_psychology_report(self) -> dict:
        """Return the latest market psychology signals for all sports."""
        from tools.auto.loop_psych import get_psychology_report as _get_psychology_report
        return _get_psychology_report(self)


# ──────────────────────────────────────────────────────────
# RESEARCH LOOP — Karpathy-style autonomous loop
# ──────────────────────────────────────────────────────────
# Design principles (from Karpathy's autoresearch):
#   1. Loop as tight as possible — rate limit is the only governor
#   2. Every iteration: hypothesize → test → measure → keep/discard
#   3. Never pause for human approval
#   4. Append-only experiment log for all attempts
#   5. Clean state between iterations (prevent error accumulation)
#   6. Maximize token throughput to Claude Code at all times
