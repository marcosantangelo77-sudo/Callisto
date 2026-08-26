"""
Paper-trade outcome resolution: player props and game-level markets.

Pure-DB logic — matches paper_trades rows against collected player_stats /
game_results / odds_snapshots and records won/lost/push plus CLV backfill.

"""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from tools.collect.http import _get_client
import asyncio  # noqa: F401
import os  # noqa: F401


logger = logging.getLogger("callisto.data_collector")

# Markets resolved at game level (vs player props)
GAME_LEVEL_MARKETS = (
    'spreads', 'totals', 'h2h', 'totals_f5', 'totals_first_5',
    'first_five_totals', 'total', 'total_first5', 'spread', 'moneyline',
)


def fuzzy_team_match(
    name: str, candidates: list[str], threshold: float = 0.8,
) -> Optional[str]:
    """
    Match a team name against candidates using progressively looser
    strategies: exact -> case-insensitive -> fuzzy (SequenceMatcher).
    Returns the best match or None if nothing exceeds *threshold*.
    """
    if not name:
        return None
    for c in candidates:
        if c == name:
            return c
    name_lower = name.lower()
    for c in candidates:
        if c.lower() == name_lower:
            return c
    best_match = None
    best_ratio = 0.0
    for c in candidates:
        ratio = difflib.SequenceMatcher(
            None, name_lower, c.lower(),
        ).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = c
    if best_match and best_ratio >= threshold:
        return best_match
    return None

async def resolve_prop_outcomes(
    dc,
    sport: str,
    game_date: str,
) -> dict:
    """
    Resolve paper trades using collected player stats.

    Matches paper_trades entries with player_stats to determine
    if props hit (Over/Under).
    """
    # Get unresolved paper trades for this date
    cursor = await dc._db.execute(
        "SELECT trade_id, player, market, line, side "
        "FROM paper_trades "
        "WHERE sport = ? AND game_date = ? AND actual_result IS NULL",
        (sport, game_date),
    )
    trades = await cursor.fetchall()

    resolved = 0
    for trade_id, player, market, line, side in trades:
        # Map market to stat_type
        stat_type = market.replace("player_", "")

        # Find matching stat — try exact match first
        stat_cursor = await dc._db.execute(
            "SELECT stat_value FROM player_stats "
            "WHERE sport = ? AND game_date = ? "
            "AND player_name = ? AND stat_type = ?",
            (sport, game_date, player, stat_type),
        )
        stat_row = await stat_cursor.fetchone()

        # Fuzzy player name matching if exact match fails
        if not stat_row and player:
            fuzzy_cursor = await dc._db.execute(
                "SELECT DISTINCT player_name FROM player_stats "
                "WHERE sport = ? AND game_date = ? AND stat_type = ?",
                (sport, game_date, stat_type),
            )
            candidates = [r[0] for r in await fuzzy_cursor.fetchall()]
            best_match = None
            best_ratio = 0.0
            for candidate in candidates:
                # Case-insensitive exact match
                if candidate.lower() == player.lower():
                    best_match = candidate
                    best_ratio = 1.0
                    break
                ratio = difflib.SequenceMatcher(
                    None, player.lower(), candidate.lower()
                ).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = candidate
            if best_match and best_ratio >= 0.85:
                stat_cursor = await dc._db.execute(
                    "SELECT stat_value FROM player_stats "
                    "WHERE sport = ? AND game_date = ? "
                    "AND player_name = ? AND stat_type = ?",
                    (sport, game_date, best_match, stat_type),
                )
                stat_row = await stat_cursor.fetchone()
                if stat_row:
                    logger.info(
                        f"Fuzzy matched player '{player}' -> '{best_match}' "
                        f"(ratio={best_ratio:.2f})"
                    )

        if not stat_row or line is None:
            continue

        actual_stat = stat_row[0]

        # Determine result
        if side == "Over":
            result = "won" if actual_stat > line else "lost" if actual_stat < line else "push"
        elif side == "Under":
            result = "won" if actual_stat < line else "lost" if actual_stat > line else "push"
        else:
            continue

        await dc._db.execute(
            "UPDATE paper_trades SET actual_result = ?, actual_stat = ? "
            "WHERE trade_id = ?",
            (result, actual_stat, trade_id),
        )
        resolved += 1

    from tools.db_utils import commit_with_retry
    await commit_with_retry(dc._db, operation="data_collector resolve_paper_trades")

    clv_written = 0
    try:
        from tools.clv_tracker import CLVTracker
        _clv = CLVTracker(dc.db_path)
        _clv._db = dc._db
        clv_written = await _clv.sync_paper_trades_to_clv_log()
    except Exception as e:
        logger.warning(f"clv_log player-prop sync failed ({sport} {game_date}): {e}")

    logger.info(
        f"Resolved {resolved}/{len(trades)} paper trades for {sport} "
        f"on {game_date} (clv_log +{clv_written})"
    )

    return {
        "sport": sport,
        "game_date": game_date,
        "total_pending": len(trades),
        "resolved": resolved,
        "clv_log_written": clv_written,
    }

async def resolve_game_level_outcomes(
    dc,
    sport: str,
    game_date: str,
) -> dict:
    """
    Resolve paper trades for game-level markets (spreads, totals, h2h/moneyline).

    Uses the game_results table to determine outcomes for paper trades
    that are NOT player props.

    Args:
        sport: Odds API sport key (e.g. 'basketball_nba')
        game_date: YYYY-MM-DD format

    Returns:
        Summary dict with counts of pending, resolved, and unmatched trades.
    """
    # Build a comma-separated placeholder list for the IN clause
    placeholders = ",".join("?" for _ in GAME_LEVEL_MARKETS)

    # Fetch unresolved game-level paper trades (include home_team/away_team for matching)
    cursor = await dc._db.execute(
        f"SELECT trade_id, event_id, market, line, side, home_team, away_team "
        f"FROM paper_trades "
        f"WHERE sport = ? AND game_date = ? AND actual_result IS NULL "
        f"AND market IN ({placeholders})",
        (sport, game_date, *GAME_LEVEL_MARKETS),
    )
    trades = await cursor.fetchall()

    if not trades:
        return {
            "sport": sport,
            "game_date": game_date,
            "total_pending": 0,
            "resolved": 0,
            "unmatched": 0,
        }

    # Fetch all game results for this sport + date
    gr_cursor = await dc._db.execute(
        "SELECT home_team, away_team, home_score, away_score, "
        "total_score, spread_result, winner "
        "FROM game_results WHERE sport = ? AND game_date = ?",
        (sport, game_date),
    )
    game_rows = await gr_cursor.fetchall()

    # Build lookup structures
    games = []
    all_team_names = []
    for home, away, h_score, a_score, total, spread_res, winner in game_rows:
        games.append({
            "home_team": home,
            "away_team": away,
            "home_score": h_score,
            "away_score": a_score,
            "total_score": total,
            "spread_result": spread_res,
            "winner": winner,
        })
        all_team_names.extend([home, away])

    resolved = 0
    unmatched = 0

    for trade_row in trades:
        trade_id, event_id, market, line, side = trade_row[:5]
        pt_home = trade_row[5] if len(trade_row) > 5 else None
        pt_away = trade_row[6] if len(trade_row) > 6 else None
        game = None

        # Strategy 1: match by event_id if paper trade has one
        if event_id:
            eid_cursor = await dc._db.execute(
                "SELECT gr.home_team, gr.away_team, gr.home_score, gr.away_score, "
                "gr.total_score, gr.spread_result, gr.winner "
                "FROM game_results gr "
                "JOIN game_contexts gc ON gr.sport = gc.sport "
                "  AND gr.game_date = gc.game_date "
                "  AND gr.home_team = gc.home_team "
                "  AND gr.away_team = gc.away_team "
                "WHERE gc.event_id = ? AND gr.sport = ? AND gr.game_date = ?",
                (event_id, sport, game_date),
            )
            eid_row = await eid_cursor.fetchone()
            if eid_row:
                game = {
                    "home_team": eid_row[0],
                    "away_team": eid_row[1],
                    "home_score": eid_row[2],
                    "away_score": eid_row[3],
                    "total_score": eid_row[4],
                    "spread_result": eid_row[5],
                    "winner": eid_row[6],
                }

        # Strategy 2: match by team name from the side field
        if not game and side and games:
            matched_team = fuzzy_team_match(side, all_team_names)
            if matched_team:
                for g in games:
                    if matched_team in (g["home_team"], g["away_team"]):
                        game = g
                        break

        # Strategy 3: match by stored home_team/away_team (critical for totals
        # where side="Over"/"Under" and can't team-match via Strategy 2)
        if not game and games and (pt_home or pt_away):
            match_name = pt_home or pt_away
            matched_team = fuzzy_team_match(match_name, all_team_names)
            if matched_team:
                for g in games:
                    if matched_team in (g["home_team"], g["away_team"]):
                        game = g
                        break

        if not game:
            unmatched += 1
            continue

        result = None

        # ── h2h / moneyline ──
        if market in ('h2h', 'moneyline'):
            winner = game["winner"]
            if winner == "push":
                result = "push"
            else:
                matched = fuzzy_team_match(side, [game["home_team"], game["away_team"]])
                if matched:
                    winner_matched = fuzzy_team_match(
                        winner, [game["home_team"], game["away_team"]],
                    )
                    result = "won" if matched == winner_matched else "lost"

        # ── spreads ──
        elif market in ('spreads', 'spread'):
            if line is not None:
                # Determine if the side is the home or away team
                matched = fuzzy_team_match(side, [game["home_team"], game["away_team"]])
                if matched:
                    if matched == game["home_team"]:
                        # Home team: margin = home_score - away_score
                        margin = game["home_score"] - game["away_score"]
                    else:
                        # Away team: margin = away_score - home_score
                        margin = game["away_score"] - game["home_score"]
                    # The team covers if margin + line > 0
                    adjusted = margin + line
                    if adjusted > 0:
                        result = "won"
                    elif adjusted < 0:
                        result = "lost"
                    else:
                        result = "push"

        # ── totals ──
        elif market in ('totals', 'total', 'totals_f5', 'totals_first_5',
                        'first_five_totals', 'total_first5'):
            if line is not None and game["total_score"] is not None:
                total = game["total_score"]
                side_lower = (side or "").lower().strip()
                if side_lower == "over":
                    if total > line:
                        result = "won"
                    elif total < line:
                        result = "lost"
                    else:
                        result = "push"
                elif side_lower == "under":
                    if total < line:
                        result = "won"
                    elif total > line:
                        result = "lost"
                    else:
                        result = "push"

        if result is None:
            unmatched += 1
            continue

        await dc._db.execute(
            "UPDATE paper_trades SET actual_result = ? "
            "WHERE trade_id = ?",
            (result, trade_id),
        )
        resolved += 1

    from tools.db_utils import commit_with_retry
    await commit_with_retry(dc._db, operation="data_collector resolve_game_paper_trades")

    # Backfill closing odds from closing_lines table for trades missing them
    cl_backfilled = 0
    backfill_cursor = await dc._db.execute(
        "SELECT trade_id, event_id, market, side, signal_implied_prob "
        "FROM paper_trades "
        "WHERE sport = ? AND game_date = ? AND closing_odds IS NULL",
        (sport, game_date),
    )
    backfill_trades = await backfill_cursor.fetchall()

    for bt in backfill_trades:
        bt_id, bt_event, bt_market, bt_side, bt_signal_imp = bt
        cl_cursor = await dc._db.execute(
            "SELECT closing_odds, closing_implied FROM closing_lines "
            "WHERE event_id = ? AND market = ? AND team = ? "
            "ORDER BY CASE WHEN source = 'Pinnacle' THEN 0 "
            "WHEN source = 'LowVig.ag' THEN 1 ELSE 2 END, "
            "captured_at DESC LIMIT 1",
            (bt_event, bt_market, bt_side),
        )
        cl_row = await cl_cursor.fetchone()

        if not cl_row:
            cl_row = await _closing_from_snapshot(
                dc, sport, game_date, bt_event, bt_market, bt_side
            )

        if cl_row:
            cl_odds, cl_implied = cl_row
            clv = None
            if bt_signal_imp is not None and cl_implied is not None:
                clv = round(cl_implied - bt_signal_imp, 4)
            await dc._db.execute(
                "UPDATE paper_trades SET closing_odds = ?, "
                "closing_implied = ?, clv_implied = ? "
                "WHERE trade_id = ?",
                (cl_odds, cl_implied, clv, bt_id),
            )
            cl_backfilled += 1

    if cl_backfilled > 0:
        await commit_with_retry(
            dc._db,
            operation="data_collector backfill_closing_odds",
        )
        logger.info(
            f"Backfilled closing odds for {cl_backfilled} paper trades "
            f"({sport} {game_date})"
        )

    # Promote every freshly-resolved paper trade into clv_log — this is
    # the permanent signal-quality ledger. Without this call, paper_trade
    # wins/losses never reach the CLV analysis surface. Idempotent: the
    # sync method only touches rows missing a matching clv_log entry.
    clv_written = 0
    try:
        from tools.clv_tracker import CLVTracker
        _clv = CLVTracker(dc.db_path)
        _clv._db = dc._db  # reuse the caller's connection for the same tx
        clv_written = await _clv.sync_paper_trades_to_clv_log()
    except Exception as e:
        logger.warning(f"clv_log paper-trade sync failed ({sport} {game_date}): {e}")

    logger.info(
        f"Resolved {resolved}/{len(trades)} game-level paper trades "
        f"for {sport} on {game_date} ({unmatched} unmatched, "
        f"clv_log +{clv_written})"
    )

    return {
        "sport": sport,
        "game_date": game_date,
        "total_pending": len(trades),
        "resolved": resolved,
        "unmatched": unmatched,
        "clv_log_written": clv_written,
    }

async def _closing_from_snapshot(
    dc, sport: str, game_date: str, event_id: str, market: str, side: str
):
    """Extract closing odds from the last odds snapshot containing this game."""
    import json

    try:
        cursor = await dc._db.execute(
            "SELECT snapshot_json FROM odds_snapshots "
            "WHERE sport = ? AND timestamp LIKE ? "
            "ORDER BY timestamp DESC LIMIT 10",
            (sport, f"{game_date}%"),
        )
        rows = await cursor.fetchall()

        # Canonicalize the sharp-books allowlist so "Betfair Exchange"
        # (odds-api.com title casing with space) and "betfair_exchange"
        # (odds-api.io key) both resolve. Before this fix, the literal
        # space-form vs underscore-form meant odds-api.io snapshots
        # never matched and the function silently returned soft-book
        # closes — making close_reliable=False for most paper trades.
        from tools.book_keys import canonicalize_book, canonicalize_book_set
        sharp_books = canonicalize_book_set(
            {"pinnacle", "lowvig.ag", "betfair_exchange", "circa", "sharp"}
        )
        for row in rows:
            try:
                data = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                continue

            for game in data.get("games", []):
                if game.get("id") != event_id:
                    continue

                best_odds = None
                best_implied = None
                is_sharp = False

                for bm in game.get("bookmakers", []):
                    book = canonicalize_book(bm.get("title") or bm.get("key") or "")
                    for mkt in bm.get("markets", []):
                        if mkt.get("key") != market:
                            continue
                        for outcome in mkt.get("outcomes", []):
                            if outcome.get("name") != side:
                                continue
                            price = outcome.get("price")
                            if price is None:
                                continue
                            price = int(price)
                            imp = 1 / (1 + 100 / abs(price)) if price > 0 else abs(price) / (abs(price) + 100)
                            if book in sharp_books:
                                return (price, round(imp, 4))
                            if best_odds is None or not is_sharp:
                                best_odds = price
                                best_implied = round(imp, 4)

                if best_odds is not None:
                    return (best_odds, best_implied)

    except Exception as e:
        logger.debug(f"Snapshot closing line lookup failed: {e}")

    return None