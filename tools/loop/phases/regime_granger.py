"""Granger and regime ResearchLoop phases, extracted from post_live.

Callers still import these names from tools.loop.phases.post_live / phases_impl.
This module must never import tools.autonomous (circular).
phase_live_execute stays defined in the phases_impl facade (not relocated).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from tools.loop import phases_impl as _impl

logger = _impl.logger
_regime_cache = _impl._regime_cache
REGIME_ANALYSIS_INTERVAL = _impl.REGIME_ANALYSIS_INTERVAL
RESEARCH_SPORTS = _impl.RESEARCH_SPORTS


async def phase_granger_analysis(loop) -> None:
    self = loop
    focus_sports = RESEARCH_SPORTS
    """Granger temporal prediction phase — identify which books lead each sport.

    Runs weekly (every ~100 cycles at 1-min intervals). Checks the most
    recent computed_at timestamp in granger_results and skips if the last
    analysis is less than 7 days old.

    Results feed into edge_scanner's dynamic sharp book classification:
    when a book is identified as the temporal leader for a sport, it is
    added to the sharp set for edge detection in that sport.
    """
    import aiosqlite
    from tools.granger_causality import analyze_book_leadership, store_results

    db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

    # Check if we ran recently (within 7 days) — skip if so
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            cursor = await db.execute(
                "SELECT MAX(computed_at) FROM granger_results"
            )
            row = await cursor.fetchone()
            if row and row[0]:
                last_computed = datetime.fromisoformat(row[0])
                age_days = (datetime.now(timezone.utc) - last_computed).total_seconds() / 86400
                if age_days < 7:
                    logger.debug(
                        f"Granger analysis: last run {age_days:.1f} days ago, "
                        f"skipping (< 7 days)"
                    )
                    return
    except Exception as e:
        # Table might not exist yet or be empty — proceed with analysis
        logger.debug(f"Granger recency check failed (will run analysis): {e}")

    # Run analysis for all sports
    total_stored = 0
    for sport in RESEARCH_SPORTS:
        if not self._running:
            break
        try:
            results = await analyze_book_leadership(db_path, sport)
            leader = results.get("leader_book")
            score = results.get("leader_score", 0)
            n_pairs = results.get("n_pairs_tested", 0)

            if results.get("warning"):
                logger.info(
                    f"Granger {sport}: {results['warning']}"
                )
                continue

            if leader:
                logger.info(
                    f"Granger {sport}: leader={leader} "
                    f"(score={score:.3f}, pairs={n_pairs}, "
                    f"books={results.get('books_tested', [])})"
                )
            else:
                logger.info(
                    f"Granger {sport}: no clear leader "
                    f"(pairs={n_pairs}, books={results.get('books_tested', [])})"
                )

            stored = await store_results(db_path, results)
            total_stored += stored

            # Update edge_scanner's cache immediately
            if leader:
                from tools.edge_scanner import _granger_sharp_cache
                _granger_sharp_cache[sport] = (leader, time.time())

        except Exception as e:
            logger.warning(f"Granger analysis failed for {sport}: {e}")

    if total_stored:
        logger.info(
            f"Granger phase complete: {total_stored} results stored "
            f"across {len(focus_sports)} sports"
        )

    # Record phase success for pipeline integrity tracking
    try:
        from tools.pipeline_integrity import get_checker
        get_checker().record_phase_result("granger_analysis", True)
    except Exception:
        pass


async def phase_regime_analysis(loop) -> None:
    self = loop
    """Regime analysis phase — detect regime changes, recency bias, mean reversion.

    Runs every REGIME_ANALYSIS_INTERVAL cycles (regime changes are slow —
    no point re-analyzing every minute). Results are cached in
    _regime_cache and fed into:
      1. Edge confidence scoring (via regime_data parameter)
      2. Hypothesis generation (regime context in Claude prompt)
      3. Edge candidate enrichment (regime signals on candidates)

    Uses ESPN box score data already collected by data_collector to build
    per-team performance histories, then runs full_regime_analysis() from
    tools/regime.py.
    """
    if self._cycles % REGIME_ANALYSIS_INTERVAL != 0:
        return

    from tools.regime import full_regime_analysis

    logger.info("Research: running regime analysis phase")

    # Map sport keys to the short sport name used by regime.py
    sport_short_map = {
        "basketball_nba": "nba",
        "americanfootball_nfl": "nfl",
        "icehockey_nhl": "nhl",
        "baseball_mlb": "mlb",
        "basketball_ncaab": "ncaab",
        "basketball_ncaaw": "ncaaw",
        "basketball_wnba": "wnba",
    }

    db = self.data_collector._db
    if db is None:
        logger.warning("Regime analysis: data_collector DB not initialized")
        return

    total_analyzed = 0
    total_signals = 0

    for sport in RESEARCH_SPORTS:
        if not self._running:
            break

        sport_short = sport_short_map.get(sport, "nba")

        try:
            # Query box score data for team performance histories.
            # box_scores table has: sport, game_date, team_name, points,
            # opponent_points, plus advanced stats when available.
            cursor = await db.execute(
                "SELECT team_name, points FROM box_scores "
                "WHERE sport = ? AND points IS NOT NULL "
                "ORDER BY game_date ASC",
                (sport,),
            )
            rows = await cursor.fetchall()

            if not rows:
                logger.debug(f"Regime analysis: no box score data for {sport}")
                continue

            # Group by team
            from collections import defaultdict
            team_histories: dict[str, list[float]] = defaultdict(list)
            for team_name, points in rows:
                if team_name and points is not None:
                    team_histories[team_name].append(float(points))

            if not team_histories:
                continue

            # Compute league average for this sport
            all_points = []
            for pts_list in team_histories.values():
                all_points.extend(pts_list)
            league_avg = sum(all_points) / len(all_points) if all_points else 100.0

            # Run regime analysis for each team with enough data
            for team_name, history in team_histories.items():
                if len(history) < 8:
                    continue  # Need minimum data for meaningful analysis

                try:
                    team_data = {
                        "name": team_name,
                        "performance_history": history,
                        "league_avg": league_avg,
                    }
                    result = full_regime_analysis(team_data, sport=sport_short)

                    # Cache the result keyed by team name
                    cache_key = f"{sport}:{team_name}"
                    _regime_cache[cache_key] = result
                    total_analyzed += 1

                    if result.get("has_edge_signal"):
                        total_signals += 1
                        logger.info(
                            f"Regime signal: {team_name} ({sport_short}) — "
                            f"{result.get('actionable_signals', [])}"
                        )
                except Exception as e:
                    logger.debug(f"Regime analysis failed for {team_name}: {e}")

        except Exception as e:
            logger.warning(f"Regime analysis failed for {sport}: {e}")

    self._last_regime_analysis = time.time()

    if total_analyzed > 0:
        logger.info(
            f"Regime analysis complete: {total_analyzed} teams analyzed, "
            f"{total_signals} with actionable signals, "
            f"cache size: {len(_regime_cache)}"
        )

    # Record phase success for pipeline integrity tracking
    try:
        from tools.pipeline_integrity import get_checker
        get_checker().record_phase_result("regime_analysis", True)
    except Exception:
        pass
