"""Run orchestration glue extracted from tools/backtest.py (slice 3).

Resolution pipelines (scores API + local game_results), run-stat
recalculation, staleness fingerprints, and result retrieval. These are the
engine's "second half": everything that happens after events are recorded.

tools/backtest.py remains the public facade: BacktestEngine re-binds these
functions as thin methods, so call sites and signatures are unchanged.
"""

from __future__ import annotations

import inspect
import logging
from typing import Callable, Optional

from tools.btest.events_io import signal_confidence
from tools.btest.resolution import (
    build_results_index,
    extract_home_away_teams,
    find_scores_for_event,
    scores_from_odds_api_game,
)
from tools.btest.run_stats import compute_signal_metrics, fingerprint_stale, prune_fingerprints

logger = logging.getLogger("callisto.backtest")

RecalcFn = Callable[[str], "object"]


async def _await_recalc(recalc_fn: RecalcFn, run_id: str):
    """Call a recalc callback, awaiting it if it returns an awaitable."""
    out = recalc_fn(run_id)
    if inspect.isawaitable(out):
        return await out
    return out


async def populate_signals_from_backtest(db, run_id: str, hypothesis_id: str) -> int:
    """Copy backtest events with signal_generated=1 into the signals table.

    Returns the number of signals inserted.
    """
    rows = await db.execute_fetchall(
        "SELECT event_id, sport, side, market, book, book_odds_american, "
        "model_fair_prob, edge, ev_pct, kelly_fraction "
        "FROM backtest_events "
        "WHERE run_id = ? AND hypothesis_id = ? AND signal_generated = 1",
        (run_id, hypothesis_id),
    )
    if not rows:
        return 0

    inserted = 0
    for r in rows:
        edge_val = r[7] or 0  # edge column
        confidence = signal_confidence(edge_val)
        await db.execute(
            "INSERT OR IGNORE INTO signals "
            "(event_id, sport, signal_type, team, market, book, "
            "odds_american, fair_probability, fair_prob_source, "
            "edge_pct, ev_pct, confidence, kelly_fraction, "
            "recommended_stake, status, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r[0],         # event_id
                r[1],         # sport
                "backtest",   # signal_type — distinguishes from paper_trade
                r[2],         # side/team
                r[3],         # market
                r[4],         # book
                r[5] or 0,    # odds_american
                r[6] or 0,    # fair_probability
                "cross_book_devig",
                edge_val,
                r[8] or 0,    # ev_pct
                confidence,
                r[9],         # kelly_fraction
                None,         # recommended_stake
                "historical",  # status — these are resolved, not actionable
                f"hypothesis_id={hypothesis_id}, run_id={run_id}",
            ),
        )
        inserted += 1

    await db.commit()
    logger.info(
        f"Backtest {run_id}: populated {inserted} signals from backtest events"
    )
    return inserted


async def resolve_line_for_event(
    market: str,
    side: str,
    line: Optional[float],
    home_score: int,
    away_score: int,
    home_team: str,
    away_team: str,
) -> Optional[str]:
    """Resolve one event against final scores — see tools.backtest_io."""
    from tools.backtest_io import resolve_line
    return resolve_line(market, side, line, home_score, away_score, home_team, away_team)


def team_name_matches(name_a: str, name_b: str) -> bool:
    """Fuzzy team-name match — see tools.backtest_io."""
    from tools.backtest_io import _team_matches
    return _team_matches(name_a, name_b)


async def resolve_with_scores(db, run_id: str, sport: str) -> dict:
    """Resolve backtest events using actual game results.

    Fetches scores from The Odds API (free endpoint).
    For player props, needs external stats source.

    Returns resolution summary.
    """
    from tools.odds_api import get_scores

    # Get unresolved events for this run
    cursor = await db.execute(
        "SELECT DISTINCT event_id, game_date FROM backtest_events "
        "WHERE run_id = ? AND actual_result IS NULL",
        (run_id,),
    )
    unresolved = await cursor.fetchall()

    resolved_count = 0
    for event_id, game_date in unresolved:
        # Get scores (free API call)
        scores_data = await get_scores(sport=sport, days_from=3)
        games = scores_data.get("games", [])

        for game in games:
            if game.get("id") != event_id:
                continue

            scores = scores_from_odds_api_game(game)
            if scores is None:
                continue
            home_score, away_score = scores

            # Resolve spreads, totals, h2h events
            ev_cursor = await db.execute(
                "SELECT id, market, side, line, book_odds_american FROM backtest_events "
                "WHERE run_id = ? AND event_id = ? AND actual_result IS NULL",
                (run_id, event_id),
            )
            ev_rows = await ev_cursor.fetchall()

            for ev_id, market, side, line, odds in ev_rows:
                result = await resolve_line_for_event(
                    market, side, line, home_score, away_score,
                    game.get("home_team", ""), game.get("away_team", ""),
                )
                if result:
                    await db.execute(
                        "UPDATE backtest_events SET actual_result = ? WHERE id = ?",
                        (result, ev_id),
                    )
                    resolved_count += 1

    await db.commit()
    return {"run_id": run_id, "resolved": resolved_count}


async def resolve_from_game_results(
    db,
    run_id: Optional[str] = None,
    sport: Optional[str] = None,
    *,
    fingerprints_cache: Optional[dict] = None,
    recalc_fn: Optional[RecalcFn] = None,
) -> dict:
    """Resolve backtest events using the local game_results table.
    No API calls needed — matches on game_date + teams with fuzzy name matching.

    If run_id is given, resolves only that run's events.
    If sport is given without run_id, resolves all unresolved events for that sport.
    If neither, resolves everything possible.
    """
    # Build query for unresolved events
    if run_id:
        cursor = await db.execute(
            "SELECT id, event_id, sport, market, side, line, game_date, model_factors "
            "FROM backtest_events WHERE run_id = ? AND actual_result IS NULL",
            (run_id,),
        )
    elif sport:
        cursor = await db.execute(
            "SELECT id, event_id, sport, market, side, line, game_date, model_factors "
            "FROM backtest_events WHERE sport = ? AND actual_result IS NULL",
            (sport,),
        )
    else:
        # Safety LIMIT: never load the entire table (38K+ rows).
        # Callers should pass sport or run_id for targeted resolution.
        cursor = await db.execute(
            "SELECT id, event_id, sport, market, side, line, game_date, model_factors "
            "FROM backtest_events WHERE actual_result IS NULL "
            "ORDER BY game_date DESC LIMIT 500",
        )

    unresolved = await cursor.fetchall()
    if not unresolved:
        return {"resolved": 0, "unresolved": 0}

    # Build a lookup of game results indexed by (sport, date) -> list of games
    # Primary: game_results table. Fallback: game_contexts table (has scores too).
    # MEMORY FIX: Only load game_results for the date range of unresolved events
    # (±1 day for timezone offsets). Previously loaded ALL rows (14K+), causing
    # ~50 MB allocation per call that CPython's pymalloc never returns to OS.
    unresolved_dates = [row[6] for row in unresolved if row[6]]  # game_date col
    if unresolved_dates:
        min_date = min(unresolved_dates)
        max_date = max(unresolved_dates)
    else:
        min_date = max_date = "2020-01-01"

    games_by_date, _dates_with_games, ctx_added = await build_results_index(
        db, min_date, max_date,
    )
    if ctx_added > 0:
        logger.info(f"Resolution: added {ctx_added} games from game_contexts fallback")

    resolved_count = 0
    match_failures = 0
    for ev_id, event_id, ev_sport, market, side, line, game_date, model_factors in unresolved:
        # Extract home/away from event_id or model_factors
        home_team, away_team = extract_home_away_teams(event_id, model_factors)

        if not home_team or not away_team:
            continue

        # Exact-date match only.
        #
        # Pre-fix this was ±1 day to compensate for the game_date vs
        # UTC-sliced commence_time timezone mismatch (see
        # tools/game_dates.py and migration 007). With local_game_date
        # now canonical across tables, the fuzzy window would just
        # occasionally match bets to the wrong adjacent-day game.
        scores = find_scores_for_event(
            games_by_date, ev_sport, game_date, home_team, away_team,
            team_matches=team_name_matches,
        )
        if not scores:
            match_failures += 1
            if match_failures <= 5:
                logger.debug(
                    f"Resolution miss: {ev_sport} {game_date} "
                    f"{home_team} vs {away_team} — "
                    f"no matching game_result found"
                )
            continue

        home_score, away_score = scores
        if home_score is None or away_score is None:
            continue

        result = await resolve_line_for_event(
            market, side, line, home_score, away_score, home_team, away_team
        )
        if result:
            await db.execute(
                "UPDATE backtest_events SET actual_result = ? WHERE id = ?",
                (result, ev_id),
            )
            resolved_count += 1

    await db.commit()
    if match_failures > 0:
        logger.warning(
            f"Resolution: {match_failures}/{len(unresolved)} events could not match "
            f"to game_results (missing game data or team name mismatch)"
        )
    logger.info(f"Resolved {resolved_count}/{len(unresolved)} backtest events from game_results")

    # Recalculate run-level stats for any runs that had events resolved
    if resolved_count > 0:
        affected_runs = await get_affected_run_ids(db, run_id)
        recalc_count = 0
        if recalc_fn is not None:
            for rid in affected_runs:
                updated = await _await_recalc(recalc_fn, rid)
                if updated:
                    recalc_count += 1
            if recalc_count > 0:
                logger.info(f"Recalculated stats for {recalc_count} backtest runs after resolution")

        # Update fingerprint cache so recalculate_all_active_runs() in Phase 5
        # sees these runs as already-current and skips the expensive re-recalc.
        # Without this, every resolution batch triggers double-recalculation:
        # once here, once in Phase 5 when it detects stale fingerprints.
        if affected_runs and fingerprints_cache is not None:
            fp_ph = ",".join("?" for _ in affected_runs)
            fp_cursor = await db.execute(
                f"SELECT run_id, COUNT(*), "
                f"SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN actual_result IS NOT NULL THEN 1 ELSE 0 END) "
                f"FROM backtest_events WHERE run_id IN ({fp_ph}) GROUP BY run_id",
                affected_runs,
            )
            for row in await fp_cursor.fetchall():
                fingerprints_cache[row[0]] = (row[1] or 0, row[2] or 0, row[3] or 0)

    return {"resolved": resolved_count, "unresolved": len(unresolved) - resolved_count}


async def get_affected_run_ids(db, run_id: Optional[str] = None) -> list[str]:
    """Get run IDs that have resolved events but stale run-level stats."""
    if run_id:
        return [run_id]
    # Find all completed runs that have resolved events but null/zero stats
    cursor = await db.execute(
        "SELECT DISTINCT br.run_id FROM backtest_runs br "
        "JOIN backtest_events be ON be.run_id = br.run_id "
        "WHERE br.completed_at IS NOT NULL "
        "AND br.total_events > 0 "
        "AND be.actual_result IS NOT NULL "
        "AND (br.actual_win = 0 AND br.actual_loss = 0 AND br.hit_rate IS NULL)"
    )
    return [r[0] for r in await cursor.fetchall()]


async def recalculate_run_stats(db, run_id: str) -> bool:
    """Recalculate ALL run stats from backtest_events.

    Updates signals_generated, total_events, win/loss/hit_rate, and edge
    metrics. This is critical because retroactive signal updates and game
    result resolution change backtest_events AFTER the run completes.

    Signal metrics (signals_count, wins, losses, pushes, hit_rate, avg_edge,
    p-value, Sharpe, Brier, IC) are deduplicated per unique event_id, keeping
    only the best-edge row per event. This matches the deduplication in
    _get_backtest_signals / evaluate_significance. total_events still counts
    all book-level rows.
    """
    # Recount total events and signals from backtest_events (source of truth)
    # total_events = all rows (book-level); signals_count = unique event_ids with signal
    # raw_signals = all signal rows before dedup (for debugging inflation)
    cursor = await db.execute(
        "SELECT COUNT(*), "
        "COUNT(DISTINCT CASE WHEN signal_generated = 1 THEN event_id END), "
        "SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END) "
        "FROM backtest_events WHERE run_id = ?",
        (run_id,),
    )
    row = await cursor.fetchone()
    total_events = row[0] or 0
    signals_count = row[1] or 0
    raw_signals = row[2] or 0

    # Get win/loss from SIGNAL events only — deduplicated by event_id (best edge)
    cursor = await db.execute(
        "WITH unique_signals AS ("
        "  SELECT event_id, actual_result, "
        "    ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY edge DESC) as rn "
        "  FROM backtest_events "
        "  WHERE run_id = ? AND signal_generated = 1 AND actual_result IS NOT NULL"
        ") "
        "SELECT actual_result, COUNT(*) FROM unique_signals "
        "WHERE rn = 1 GROUP BY actual_result",
        (run_id,),
    )
    results = {r[0]: r[1] for r in await cursor.fetchall()}

    wins = results.get("won", 0)
    losses = results.get("lost", 0)
    pushes = results.get("push", 0)
    total_decided = wins + losses

    if total_decided == 0 and signals_count == 0:
        return False  # Nothing to update

    # Count unresolved — unique signal events with NULL result
    cursor = await db.execute(
        "SELECT COUNT(DISTINCT event_id) FROM backtest_events "
        "WHERE run_id = ? AND signal_generated = 1 AND actual_result IS NULL",
        (run_id,),
    )
    unresolved = (await cursor.fetchone())[0]

    hit_rate = wins / total_decided if total_decided > 0 else None

    # Calculate avg_edge, avg_ev from signal-generated events — deduplicated
    cursor = await db.execute(
        "WITH unique_signals AS ("
        "  SELECT event_id, edge, ev_pct, clv_implied, "
        "    ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY edge DESC) as rn "
        "  FROM backtest_events "
        "  WHERE run_id = ? AND signal_generated = 1 AND actual_result IS NOT NULL"
        ") "
        "SELECT AVG(edge), AVG(ev_pct), AVG(clv_implied) "
        "FROM unique_signals WHERE rn = 1",
        (run_id,),
    )
    row = await cursor.fetchone()
    avg_edge = row[0]
    avg_ev = row[1]
    avg_clv = row[2]

    # ── Recalculate statistical metrics (p-value, Brier, IC, Sharpe, ROI) ──
    # These were previously left stale, blocking promotion even when hit rates improved.
    p_binomial = 1.0
    p_ttest = 1.0
    z_score = 0.0
    sharpe = 0.0
    sortino = None
    brier = None
    ic = None
    roi_pct = 0.0

    signal_events: list[tuple] = []
    expected_rate = 0.5
    if total_decided > 0:
        # Compute expected win rate from avg book implied probability
        # (NOT 0.5 coin-flip — a 5W-0L record on -300 favorites is NOT
        # as impressive as 5W-0L on coin-flips. The null hypothesis must
        # match the market's expected rate.)
        cursor = await db.execute(
            "WITH unique_signals AS ("
            "  SELECT event_id, book_implied_prob, "
            "    ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY edge DESC) as rn "
            "  FROM backtest_events "
            "  WHERE run_id = ? AND signal_generated = 1 "
            "  AND actual_result IN ('won', 'lost') "
            "  AND book_implied_prob IS NOT NULL AND book_implied_prob > 0"
            ") "
            "SELECT AVG(book_implied_prob) FROM unique_signals WHERE rn = 1",
            (run_id,),
        )
        row_imp = await cursor.fetchone()
        expected_rate = row_imp[0] if row_imp and row_imp[0] else 0.5

        # Get per-signal returns for t-test, Sharpe, Sortino, ROI — deduplicated
        cursor = await db.execute(
            "WITH unique_signals AS ("
            "  SELECT event_id, book_odds_american, actual_result, model_fair_prob, edge, "
            "    ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY edge DESC) as rn "
            "  FROM backtest_events "
            "  WHERE run_id = ? AND signal_generated = 1 AND actual_result IN ('won', 'lost')"
            ") "
            "SELECT book_odds_american, actual_result, model_fair_prob, edge "
            "FROM unique_signals WHERE rn = 1",
            (run_id,),
        )
        signal_events = await cursor.fetchall()

    metrics = compute_signal_metrics(wins, losses, expected_rate, signal_events)
    p_binomial = metrics["p_binomial"]
    p_ttest = metrics["p_ttest"]
    z_score = metrics["z_score"]
    sharpe = metrics["sharpe"]
    sortino = metrics["sortino"]
    brier = metrics["brier"]
    ic = metrics["ic"]
    roi_pct = metrics["roi_pct"]

    from tools.db_utils import execute_with_retry, commit_with_retry
    await execute_with_retry(
        db,
        "UPDATE backtest_runs SET "
        "total_events = ?, signals_generated = ?, "
        "actual_win = ?, actual_loss = ?, actual_push = ?, unresolved = ?, "
        "hit_rate = ?, avg_edge = ?, avg_ev = ?, avg_clv = ?, "
        "p_value_binomial = ?, p_value_ttest = ?, z_score = ?, "
        "sharpe_ratio = ?, sortino_ratio_val = ?, "
        "brier_score = ?, information_coefficient = ?, roi_pct = ? "
        "WHERE run_id = ?",
        (total_events, signals_count, wins, losses, pushes, unresolved,
         hit_rate, avg_edge, avg_ev, avg_clv,
         p_binomial, p_ttest, z_score,
         sharpe, sortino,
         brier, ic, roi_pct,
         run_id),
        operation="backtest recalculate_run_stats",
    )
    await commit_with_retry(db, operation="backtest recalculate_run_stats")
    logger.info(
        f"Run {run_id}: recalculated — {wins}W/{losses}L/{pushes}P "
        f"({unresolved} unresolved), signals={signals_count} unique/{raw_signals} raw, "
        f"hr={hit_rate:.3f}, p={p_binomial:.4f}, "
        f"brier={f'{brier:.3f}' if brier is not None else 'N/A'}, ic={f'{ic:.3f}' if ic is not None else 'N/A'}"
        if hit_rate else
        f"Run {run_id}: recalculated — {wins}W/{losses}L/{pushes}P "
        f"({unresolved} unresolved), signals={signals_count} unique/{raw_signals} raw"
    )
    return True


async def recalculate_all_active_runs(
    db,
    fingerprints_cache: dict,
    run_fp_max: int,
    recalc_fn: RecalcFn,
    hypothesis_ids: list[str] | None = None,
) -> int:
    """Recompute stats for runs belonging to active (backtesting) hypotheses.

    This fixes the stale backtest_runs problem: when retroactive signal updates
    or game result resolution change backtest_events AFTER the original run,
    backtest_runs stats become outdated. The promotion gate checks these stats,
    so stale data blocks promotion of winning hypotheses.

    Uses a lightweight fingerprint cache to skip runs whose underlying
    backtest_events haven't changed since the last recalculation.  The
    fingerprint is (total_events, signals_count, resolved_count) per run —
    a single cheap aggregate query that catches new events, retroactive
    signal_generated flips, AND game result resolution updates.  Only runs
    with a changed fingerprint get the expensive scipy/numpy recompute.
    This cuts the typical 10-15 min stall to seconds.

    Args:
        hypothesis_ids: If provided, only recalculate runs for these hypotheses.
                       If None, recalculates ALL active runs (legacy behavior).

    Returns number of runs updated.
    """
    if hypothesis_ids:
        placeholders = ",".join("?" for _ in hypothesis_ids)
        cursor = await db.execute(
            f"SELECT DISTINCT run_id FROM backtest_runs "
            f"WHERE hypothesis_id IN ({placeholders})",
            hypothesis_ids,
        )
    else:
        cursor = await db.execute(
            "SELECT DISTINCT br.run_id FROM backtest_runs br "
            "JOIN hypotheses h ON br.hypothesis_id = h.hypothesis_id "
            "WHERE h.status = 'backtesting'"
        )
    run_ids = [row[0] for row in await cursor.fetchall()]

    if not run_ids:
        return 0

    # ── Staleness check: compute lightweight fingerprints ──
    # One query for ALL candidate runs — far cheaper than per-run recalculate_run_stats
    # which does 4 queries + scipy/numpy each.
    fp_placeholders = ",".join("?" for _ in run_ids)
    cursor = await db.execute(
        f"SELECT run_id, "
        f"  COUNT(*), "
        f"  SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END), "
        f"  SUM(CASE WHEN actual_result IS NOT NULL THEN 1 ELSE 0 END) "
        f"FROM backtest_events "
        f"WHERE run_id IN ({fp_placeholders}) "
        f"GROUP BY run_id",
        run_ids,
    )
    current_fps: dict[str, tuple[int, int, int]] = {}
    for row in await cursor.fetchall():
        current_fps[row[0]] = (row[1] or 0, row[2] or 0, row[3] or 0)

    # Determine which runs actually need recalculation
    stale_run_ids = [
        rid for rid in run_ids
        if fingerprint_stale(
            fingerprints_cache.get(rid),
            current_fps.get(rid, (0, 0, 0)),
        )
    ]

    if not stale_run_ids:
        logger.debug(
            f"Staleness check: all {len(run_ids)} runs unchanged, skipping recalculation"
        )
        return 0

    logger.info(
        f"Staleness check: {len(stale_run_ids)}/{len(run_ids)} runs have new data, recalculating"
    )

    updated = 0
    for run_id in stale_run_ids:
        try:
            if await _await_recalc(recalc_fn, run_id):
                updated += 1
            # Update cache AFTER successful recalculation
            if run_id in current_fps:
                fingerprints_cache[run_id] = current_fps[run_id]
        except Exception as e:
            logger.warning(f"Failed to recalculate run {run_id}: {e}")

    # Prune fingerprint cache — only keep entries for currently active runs
    new_cache = prune_fingerprints(dict(fingerprints_cache), run_ids, run_fp_max)
    fingerprints_cache.clear()
    fingerprints_cache.update(new_cache)

    if updated:
        logger.info(f"Recalculated stats for {updated}/{len(stale_run_ids)} stale backtest runs (skipped {len(run_ids) - len(stale_run_ids)} unchanged)")
    return updated


async def get_run_results(db, run_id: str) -> dict:
    """Retrieve full backtest results for a run."""
    # Run metadata
    cursor = await db.execute(
        "SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,),
    )
    run_row = await cursor.fetchone()
    if not run_row:
        return {"error": "Run not found"}
    run_cols = [d[0] for d in cursor.description]
    run = dict(zip(run_cols, run_row))

    # Signal events
    ev_cursor = await db.execute(
        "SELECT * FROM backtest_events "
        "WHERE run_id = ? AND signal_generated = 1 "
        "ORDER BY edge DESC LIMIT 100",
        (run_id,),
    )
    ev_rows = await ev_cursor.fetchall()
    ev_cols = [d[0] for d in ev_cursor.description]
    signals = [dict(zip(ev_cols, r)) for r in ev_rows]

    # Aggregate stats
    stats_cursor = await db.execute(
        "SELECT "
        "COUNT(*) as total, "
        "SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END) as signals, "
        "AVG(CASE WHEN signal_generated = 1 THEN edge END) as avg_edge, "
        "AVG(CASE WHEN signal_generated = 1 THEN ev_pct END) as avg_ev, "
        "SUM(CASE WHEN signal_generated = 1 AND actual_result = 'won' THEN 1 ELSE 0 END) as wins, "
        "SUM(CASE WHEN signal_generated = 1 AND actual_result = 'lost' THEN 1 ELSE 0 END) as losses, "
        "SUM(CASE WHEN signal_generated = 1 AND actual_result = 'push' THEN 1 ELSE 0 END) as pushes "
        "FROM backtest_events WHERE run_id = ?",
        (run_id,),
    )
    stats_row = await stats_cursor.fetchone()
    stats_cols = [d[0] for d in stats_cursor.description]
    stats = dict(zip(stats_cols, stats_row))

    return {
        "run": run,
        "stats": stats,
        "top_signals": signals,
    }
