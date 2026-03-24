"""
Backtest engine — replay historical odds through a model and evaluate predictions.

This is the core of the scientific method applied to betting theses:
  1. Load hypothesis config (model params, factors, thresholds)
  2. Fetch historical odds for date range (cached after first fetch)
  3. For each event: run model, compare to book, record prediction
  4. Resolve outcomes against actual results
  5. Compute aggregate statistics and significance

The engine dispatches to existing sim functions (player_prop_sim, nba_game_sim, etc.)
based on the hypothesis's model_config. No new simulation code — reuse everything.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

from tools.historical_odds import HistoricalOddsFetcher
from tools.hypothesis import HypothesisManager
from tools.math_utils import american_to_decimal, american_to_implied
from tools.devig import devig_market, power_devig, multiplicative_devig
from tools.ev import ev_binary, evaluate_edge
from tools.sizing import kelly_binary
from tools.temporal_analysis import validate_temporal_isolation

load_dotenv()

logger = logging.getLogger("callisto.backtest")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


class BacktestEngine:
    """Replay historical odds through a model and evaluate predictions."""

    def __init__(
        self,
        hypothesis_manager: HypothesisManager,
        historical_fetcher: HistoricalOddsFetcher,
        db_path: str = DB_PATH,
    ):
        self.hypothesis_manager = hypothesis_manager
        self.historical_fetcher = historical_fetcher
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        logger.info("Backtest engine initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def _enrich_snapshot_with_multibook(
        self,
        sport: str,
        date_str: str,
        snapshot: dict,
        target_book: str,
    ) -> dict:
        """Enrich a snapshot with multi-book data from odds_snapshots.

        When the historical_odds_cache has only single-book "consensus" data
        (common for older dates), check if odds_snapshots has a richer
        multi-book snapshot for the same date and sport. If so, use that
        instead — it has the target book + comparison books needed for
        cross-book edge detection.

        Returns the original snapshot if already multi-book or no better
        data is available.
        """
        games = snapshot.get("games", [])
        if not games:
            return snapshot

        # Check if snapshot already has multi-book data with the target book
        max_books = 0
        has_target = False
        for g in games:
            book_keys = {bm.get("key", "").lower() for bm in g.get("bookmakers", [])}
            max_books = max(max_books, len(book_keys))
            if target_book in book_keys:
                has_target = True

        if has_target and max_books >= 2:
            # Already have multi-book data with target — use as-is
            return snapshot

        # Try to find a better snapshot in odds_snapshots for this date
        # Look for snapshots on this date with the most games
        try:
            cursor = await self._db.execute(
                "SELECT snapshot_json FROM odds_snapshots "
                "WHERE sport = ? AND timestamp LIKE ? AND game_count > 0 "
                "ORDER BY game_count DESC LIMIT 1",
                (sport, f"{date_str}%"),
            )
            row = await cursor.fetchone()
            if not row:
                return snapshot

            better_snapshot = json.loads(row[0])
            better_games = better_snapshot.get("games", [])

            # Verify the better snapshot actually has multi-book data
            better_max_books = 0
            better_has_target = False
            for g in better_games:
                book_keys = {bm.get("key", "").lower() for bm in g.get("bookmakers", [])}
                better_max_books = max(better_max_books, len(book_keys))
                if target_book in book_keys:
                    better_has_target = True

            if better_has_target and better_max_books > max_books:
                logger.info(
                    f"Enriched {sport} {date_str}: upgraded from {max_books} to "
                    f"{better_max_books} books (from odds_snapshots)"
                )
                return better_snapshot

        except Exception as e:
            logger.warning(f"Snapshot enrichment failed for {sport} {date_str}: {e}", exc_info=True)

        return snapshot

    async def run_backtest(
        self,
        hypothesis_id: str,
        start_date: str,
        end_date: str,
        credit_budget: int = 50,
    ) -> dict:
        """
        Full backtest pipeline for a hypothesis.

        1. Load hypothesis config
        2. Fetch/load historical odds for date range
        3. For each game: devig all books, compute consensus fair value,
           find edges on target book, record predictions
        4. Resolve outcomes (if results available)
        5. Run statistical evaluation

        Returns the run summary.
        """
        h = await self.hypothesis_manager.get_hypothesis(hypothesis_id)
        if not h:
            return {"error": "Hypothesis not found"}

        config = h["model_config"]
        sport = h["sport"]
        market_type = h["market_type"]
        edge_threshold = h["edge_threshold"]
        target_book = config.get("target_book", "draftkings")
        devig_method = config.get("devig_method", "power")
        min_books = config.get("consensus_min_books", 2)

        # ── TEMPORAL ISOLATION ENFORCEMENT ──
        # If the hypothesis was generated from data analysis, ensure the backtest
        # date range doesn't overlap with the training period.
        temporal_check = validate_temporal_isolation(config, start_date, end_date)
        if not temporal_check.get("has_temporal_metadata", False):
            logger.warning(
                f"Hypothesis {hypothesis_id} has no temporal metadata — "
                "legacy hypothesis, temporal isolation NOT enforced. "
                "Re-generate this hypothesis with temporal_analysis to fix."
            )
        elif not temporal_check["valid"]:
            adjusted = temporal_check.get("adjusted_start")
            if adjusted:
                logger.warning(
                    f"Temporal overlap detected for {hypothesis_id}: "
                    f"{temporal_check['reason']} Auto-adjusting start to {adjusted}."
                )
                start_date = adjusted
            else:
                return {
                    "error": "Temporal isolation violated",
                    "detail": temporal_check["reason"],
                    "hypothesis_id": hypothesis_id,
                }
        else:
            logger.info(
                f"Temporal isolation verified for {hypothesis_id}: "
                f"training ended {temporal_check.get('training_period_end')}, "
                f"backtest starts {start_date} "
                f"(gap: {temporal_check.get('gap_days_actual', '?')} days)"
            )

        run_id = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()

        # Record run start
        await self._db.execute(
            "INSERT INTO backtest_runs "
            "(run_id, hypothesis_id, date_range_start, date_range_end, "
            "started_at, run_config) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, hypothesis_id, start_date, end_date, now, json.dumps(config)),
        )
        await self._db.commit()

        # Update hypothesis status if still draft
        if h["status"] == "draft":
            await self.hypothesis_manager.update_status(hypothesis_id, "backtesting", "auto")

        # Fetch historical data
        logger.info(f"Backtest {run_id}: fetching {sport} odds {start_date} to {end_date}")

        # Determine which markets to fetch based on hypothesis type
        if market_type.startswith("player_"):
            # For player props, we need the main odds for game-level context
            # and then per-event prop odds
            fetch_markets = "h2h,spreads,totals"
        else:
            fetch_markets = "h2h,spreads,totals"

        fetch_result = await self.historical_fetcher.bulk_fetch_date_range(
            sport=sport,
            start_date=start_date,
            end_date=end_date,
            markets=fetch_markets,
            credit_budget=credit_budget,
        )

        logger.info(
            f"Backtest {run_id}: fetched {fetch_result['dates_fetched']} dates, "
            f"{fetch_result['dates_cached_already']} cached, "
            f"{fetch_result['credits_spent']} credits spent"
        )

        # Process each cached date
        all_dates = await self.historical_fetcher.get_cached_dates(sport)
        from datetime import datetime as dt
        start_dt = dt.strptime(start_date, "%Y-%m-%d")
        end_dt = dt.strptime(end_date, "%Y-%m-%d")
        dates_in_range = [
            d for d in all_dates
            if start_dt <= dt.strptime(d, "%Y-%m-%d") <= end_dt
        ]

        total_events = 0
        total_signals = 0
        multibook_dates = 0
        singlebook_skipped = 0

        for date_str in dates_in_range:
            snapshot = await self.historical_fetcher.fetch_historical_odds(
                sport=sport, date=date_str, markets=fetch_markets,
            )

            # Check if snapshot has multi-book data or only single-book "consensus"
            snapshot = await self._enrich_snapshot_with_multibook(
                sport, date_str, snapshot, target_book,
            )
            games = snapshot.get("games", [])

            # Track data quality
            has_target = False
            for g in games:
                book_keys = {bm.get("key", "").lower() for bm in g.get("bookmakers", [])}
                if target_book in book_keys and len(book_keys) >= 2:
                    has_target = True
                    break
            if has_target:
                multibook_dates += 1
            else:
                singlebook_skipped += 1

            for game in games:
                events, signals = await self._process_game(
                    run_id=run_id,
                    hypothesis_id=hypothesis_id,
                    game=game,
                    game_date=date_str,
                    snapshot_time=snapshot.get("timestamp", date_str),
                    market_type=market_type,
                    target_book=target_book,
                    edge_threshold=edge_threshold,
                    devig_method=devig_method,
                    min_books=min_books,
                    config=config,
                    h_sport=sport,
                )
                total_events += events
                total_signals += signals

        logger.info(
            f"Backtest {run_id}: {multibook_dates} dates with multi-book data, "
            f"{singlebook_skipped} dates with single-book only (no cross-book edges)"
        )

        # Update run with totals
        completed = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE backtest_runs SET total_events = ?, signals_generated = ?, "
            "completed_at = ? WHERE run_id = ?",
            (total_events, total_signals, completed, run_id),
        )
        await self._db.commit()

        logger.info(
            f"Backtest {run_id} complete: {total_events} events, {total_signals} signals"
        )

        # Resolve outcomes using local game_results table
        resolution = await self.resolve_from_game_results(run_id=run_id, sport=sport)
        logger.info(
            f"Backtest {run_id}: resolved {resolution['resolved']} events "
            f"({resolution['unresolved']} unresolved)"
        )

        # Run significance evaluation
        sig_report = await self.hypothesis_manager.evaluate_significance(
            hypothesis_id, "backtest"
        )

        # Update run with statistical results
        if sig_report.get("sample_size", 0) > 0:
            sig = sig_report.get("significance", {})
            risk = sig_report.get("risk", {})
            edge = sig_report.get("edge_metrics", {})
            clv = sig_report.get("clv", {})
            results = sig_report.get("results", {})

            await self._db.execute(
                "UPDATE backtest_runs SET "
                "actual_win = ?, actual_loss = ?, actual_push = ?, "
                "hit_rate = ?, avg_edge = ?, avg_ev = ?, avg_clv = ?, "
                "roi_pct = ?, p_value_binomial = ?, p_value_ttest = ?, "
                "z_score = ?, sharpe_ratio = ?, max_drawdown = ?, "
                "is_significant = ? "
                "WHERE run_id = ?",
                (
                    results.get("wins", 0), results.get("losses", 0),
                    results.get("pushes", 0), results.get("hit_rate"),
                    edge.get("avg_edge"), edge.get("avg_ev"),
                    clv.get("avg_clv"), edge.get("roi_pct"),
                    sig.get("p_value_binomial"), sig.get("p_value_ttest"),
                    sig.get("z_score"), risk.get("sharpe_ratio"),
                    risk.get("max_drawdown"), sig.get("is_significant", False),
                    run_id,
                ),
            )
            await self._db.commit()

        return {
            "run_id": run_id,
            "hypothesis_id": hypothesis_id,
            "date_range": f"{start_date} to {end_date}",
            "total_events": total_events,
            "signals_generated": total_signals,
            "fetch_summary": fetch_result,
            "significance": sig_report,
        }

    async def _process_game(
        self,
        run_id: str,
        hypothesis_id: str,
        game: dict,
        game_date: str,
        snapshot_time: str,
        market_type: str,
        target_book: str,
        edge_threshold: float,
        devig_method: str,
        min_books: int,
        config: dict,
        h_sport: str = "",
    ) -> tuple[int, int]:
        """
        Process a single game: devig, compare, record predictions.
        Returns (events_processed, signals_generated).

        Cross-book edge detection requires the target book AND at least one
        other book in the data. When only a "consensus" book exists (old
        historical data without the target book), we skip — there's no
        cross-book edge to detect without pricing from both sides.

        Falls back to game-level markets (spreads/h2h/totals) when
        player prop data isn't available, since our free historical
        data is consensus game lines, not per-player props.
        """
        # Determine available markets in this game
        available_markets = set()
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                available_markets.add(mkt["key"])

        # If hypothesis wants player props but we only have game lines,
        # fall back to the closest game-level market
        effective_market = market_type
        if market_type.startswith("player_") and market_type not in available_markets:
            # Map prop types to game-level equivalents for backtesting
            prop_to_game = {
                "player_points": "totals",
                "player_rebounds": "totals",
                "player_assists": "totals",
                "player_threes": "totals",
                "player_pra": "totals",
            }
            effective_market = prop_to_game.get(market_type, "spreads")
            if effective_market not in available_markets:
                effective_market = next(iter(available_markets), None)
                if not effective_market:
                    return 0, 0

        available_books = {bm.get("key", "").lower() for bm in game.get("bookmakers", [])}
        bookmaker_count = len(available_books)

        # Cross-book edge detection requires the target book to be present
        # in the data AND at least one other book to compare against.
        # If we only have "consensus" (single-book old data), there's no
        # cross-book edge to find — skip these games.
        if target_book not in available_books:
            if bookmaker_count == 1 and "consensus" in available_books:
                # Single "consensus" book — no cross-book comparison possible.
                # These events are noise without the target book's actual pricing.
                return 0, 0
            # Target not present but we have multiple other books — pick the
            # closest retail book as target proxy (DK -> FanDuel -> BetMGM)
            retail_fallbacks = ["fanduel", "betmgm", "caesars", "betrivers", "espnbet"]
            effective_target = target_book
            for fallback in retail_fallbacks:
                if fallback in available_books:
                    effective_target = fallback
                    break
            else:
                # No retail book found — use whatever is available
                effective_target = next(iter(available_books), target_book)
        else:
            effective_target = target_book

        # Need at least 1 non-target book for cross-book comparison
        non_target_books = available_books - {effective_target}
        if not non_target_books:
            return 0, 0

        # Adapt min_books: need at least 1 non-target book for devig
        effective_min_books = min(min_books, max(1, len(non_target_books)))

        return await self._process_game_lines(
            run_id, hypothesis_id, game, game_date, snapshot_time,
            effective_market, effective_target, edge_threshold, devig_method,
            effective_min_books, config, h_sport=h_sport,
        )

    async def _process_game_lines(
        self,
        run_id: str,
        hypothesis_id: str,
        game: dict,
        game_date: str,
        snapshot_time: str,
        market_type: str,
        target_book: str,
        edge_threshold: float,
        devig_method: str,
        min_books: int,
        config: dict,
        h_sport: str = "",
    ) -> tuple[int, int]:
        """Process spreads/totals/h2h lines for a game.

        Uses cross-book edge detection when multi-book data is available:
        1. Devig each non-target book to get fair probabilities
        2. Find the BEST (sharpest) devigged line across non-target books
        3. Also compute consensus (average) devigged fair value
        4. Use the best line as the fair value — edges exist BETWEEN books
        5. Fall back to consensus-only when only 1-2 non-target books exist
        """
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        bookmakers = game.get("bookmakers", [])

        events = 0
        signals = 0

        # Organize lines by (market, outcome_name, point) -> book -> price
        lines_by_key = {}
        for bm in bookmakers:
            bk_key = bm.get("key", "").lower()
            bk_name = bm.get("title", bk_key)
            for mkt in bm.get("markets", []):
                if mkt["key"] != market_type:
                    continue
                for outcome in mkt.get("outcomes", []):
                    name = outcome.get("name", "")
                    point = outcome.get("point")
                    price = outcome.get("price", 0)
                    key = (mkt["key"], name, point)
                    if key not in lines_by_key:
                        lines_by_key[key] = {}
                    lines_by_key[key][bk_key] = {
                        "price": price,
                        "name": bk_name,
                    }

        # For each unique line, find the opposite side and devig
        # Group by (market, point) to get both sides
        # For spreads, sides have opposite-sign points (e.g., -7.5 and +7.5)
        # so group by abs(point) to pair them correctly
        sides_by_line = {}
        for (mkt_key, name, point), books in lines_by_key.items():
            group_point = abs(point) if point is not None and mkt_key == "spreads" else point
            line_key = (mkt_key, group_point)
            if line_key not in sides_by_line:
                sides_by_line[line_key] = {}
            sides_by_line[line_key][name] = books

        for (mkt_key, point), sides in sides_by_line.items():
            side_names = list(sides.keys())
            if len(side_names) != 2:
                continue

            side_a_name, side_b_name = side_names[0], side_names[1]
            side_a_books = sides[side_a_name]
            side_b_books = sides[side_b_name]

            # Find books that have both sides
            common_books = set(side_a_books.keys()) & set(side_b_books.keys())
            if len(common_books) < min_books:
                continue

            # Check target book has both sides
            if target_book not in common_books:
                continue

            # Devig each book and compute fair values
            # CRITICAL: exclude target book from consensus to avoid self-reference bias
            fair_a_values = []  # (fair_prob_a, book_key)
            fair_b_values = []  # (fair_prob_b, book_key)
            for bk in common_books:
                if bk == target_book:
                    continue  # target book is what we compare AGAINST, not part of consensus
                price_a = side_a_books[bk]["price"]
                price_b = side_b_books[bk]["price"]
                try:
                    dec_a = american_to_decimal(price_a)
                    dec_b = american_to_decimal(price_b)
                    if devig_method == "power":
                        fair, _ = power_devig([dec_a, dec_b])
                    else:
                        fair = multiplicative_devig([dec_a, dec_b])
                    fair_a_values.append((fair[0], bk))
                    fair_b_values.append((fair[1], bk))
                except (ValueError, ZeroDivisionError) as e:
                    logger.warning(
                        f"Devig failed for book={bk}, market={mkt_key}, "
                        f"prices=({price_a}, {price_b}): {e}"
                    )
                    continue

            non_target_count = len(fair_a_values)
            if non_target_count < min_books:
                continue

            # --- Cross-book edge detection ---
            # Two fair value estimates:
            #   1. consensus = average devigged fair prob across all non-target books
            #   2. best_line = sharpest (highest fair prob for each side) from any single book
            #
            # The best_line approach finds real cross-book edges:
            #   If Pinnacle devigs to 55% on Team A but DK prices Team A at 50%,
            #   that's a 5% edge. The consensus approach dilutes this with softer books.
            #
            # Strategy: use best_line when we have 3+ non-target books (reliable sharp signal),
            # fall back to consensus when fewer books are available.

            consensus_a = sum(v[0] for v in fair_a_values) / non_target_count
            consensus_b = sum(v[0] for v in fair_b_values) / non_target_count

            # Find the sharpest line for each side (highest devigged fair prob)
            best_a_val, best_a_book = max(fair_a_values, key=lambda x: x[0])
            best_b_val, best_b_book = max(fair_b_values, key=lambda x: x[0])

            # Use cross-book best line when we have enough books for a reliable signal
            use_crossbook = non_target_count >= 3
            if use_crossbook:
                # Best-line is the primary fair value — this is where edges live
                fair_a = best_a_val
                fair_b = best_b_val
                edge_method = "cross_book_best_line"
            else:
                # With few books, consensus is more reliable
                fair_a = consensus_a
                fair_b = consensus_b
                edge_method = "consensus_devig"

            # Also track all contributing books for transparency
            contributing_books_a = [bk for _, bk in fair_a_values]
            contributing_books_b = [bk for _, bk in fair_b_values]

            # Evaluate both sides against target book
            for side_name, fair_val, consensus_val, best_val, best_book, target_books, contrib_books in [
                (side_a_name, fair_a, consensus_a, best_a_val, best_a_book, side_a_books, contributing_books_a),
                (side_b_name, fair_b, consensus_b, best_b_val, best_b_book, side_b_books, contributing_books_b),
            ]:
                target_price = target_books[target_book]["price"]
                target_implied = american_to_implied(target_price)
                ev = ev_binary(fair_val, american_to_decimal(target_price))
                kelly = kelly_binary(fair_val, american_to_decimal(target_price))
                edge = ev  # Use EV as edge metric (accounts for vig in odds)
                is_signal = ev >= edge_threshold

                events += 1
                if is_signal:
                    signals += 1

                team = side_name
                # Build a matchable event_id from game identity
                event_id = game.get("id") or f"{game_date}|{home}|{away}"
                event_sport = game.get("sport_key") or h_sport

                await self._db.execute(
                    "INSERT INTO backtest_events "
                    "(run_id, event_id, hypothesis_id, sport, player, market, "
                    "line, side, book, book_odds_american, book_implied_prob, "
                    "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
                    "signal_generated, game_date, snapshot_time) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id, event_id, hypothesis_id, event_sport,
                        None, mkt_key, point, team, target_book,
                        target_price, round(target_implied, 6),
                        round(fair_val, 6),
                        json.dumps({
                            "edge_method": edge_method,
                            "books_used": non_target_count,
                            "target_excluded": True,
                            "devig_method": devig_method,
                            "best_line_book": best_book,
                            "best_line_fair_prob": round(best_val, 6),
                            "consensus_fair_prob": round(consensus_val, 6),
                            "contributing_books": contrib_books,
                            "home_team": home,
                            "away_team": away,
                        }),
                        round(edge, 6), round(ev, 6), round(kelly, 6),
                        is_signal, game_date, snapshot_time,
                    ),
                )

        await self._db.commit()
        return events, signals

    async def _process_game_props(
        self,
        run_id: str,
        hypothesis_id: str,
        game: dict,
        game_date: str,
        snapshot_time: str,
        market_type: str,
        target_book: str,
        edge_threshold: float,
        devig_method: str,
        min_books: int,
        config: dict,
    ) -> tuple[int, int]:
        """
        Process player props for a game.
        For props, we need per-event prop data which may require separate API calls.
        If prop data is embedded in the game object, process directly.
        """
        bookmakers = game.get("bookmakers", [])
        events = 0
        signals = 0

        # Organize props: (player, market, line) -> book -> {Over, Under}
        prop_lines = {}
        book_names = {}

        for bm in bookmakers:
            bk_key = bm.get("key", "").lower()
            bk_name = bm.get("title", bk_key)
            book_names[bk_key] = bk_name

            for mkt in bm.get("markets", []):
                if not mkt["key"].startswith("player_"):
                    continue
                # Filter to specific market type if specified
                if market_type != "player_props" and mkt["key"] != market_type:
                    continue

                for outcome in mkt.get("outcomes", []):
                    player = outcome.get("description", "Unknown")
                    line = outcome.get("point")
                    side = outcome.get("name", "")  # Over or Under
                    price = outcome.get("price", 0)

                    if not side or not price:
                        continue

                    key = (player, mkt["key"], line)
                    if key not in prop_lines:
                        prop_lines[key] = {}
                    if bk_key not in prop_lines[key]:
                        prop_lines[key][bk_key] = {}
                    prop_lines[key][bk_key][side] = price

        # Process each prop line
        for (player, mkt_key, line), books in prop_lines.items():
            if target_book not in books:
                continue
            target_data = books[target_book]
            if "Over" not in target_data or "Under" not in target_data:
                continue

            # Devig all books with both sides at this line
            # Track (fair_prob, book_key) for cross-book best-line detection
            fair_overs = []   # (fair_prob, book_key)
            fair_unders = []  # (fair_prob, book_key)
            for bk_key, bk_data in books.items():
                if bk_key == target_book:
                    continue  # exclude target book from consensus
                if "Over" not in bk_data or "Under" not in bk_data:
                    continue
                try:
                    dec_o = american_to_decimal(bk_data["Over"])
                    dec_u = american_to_decimal(bk_data["Under"])
                    if devig_method == "power":
                        fair, _ = power_devig([dec_o, dec_u])
                    else:
                        fair = multiplicative_devig([dec_o, dec_u])
                    fair_overs.append((fair[0], bk_key))
                    fair_unders.append((fair[1], bk_key))
                except (ValueError, ZeroDivisionError) as e:
                    logger.warning(
                        f"Devig failed for book={bk_key}, market={mkt_key}, "
                        f"prices=(Over={bk_data['Over']}, Under={bk_data['Under']}): {e}"
                    )
                    continue

            non_target_count = len(fair_overs)
            if non_target_count < min_books:
                continue

            consensus_over = sum(v[0] for v in fair_overs) / non_target_count
            consensus_under = sum(v[0] for v in fair_unders) / non_target_count

            # Cross-book best line: sharpest devigged fair prob for each side
            best_over_val, best_over_book = max(fair_overs, key=lambda x: x[0])
            best_under_val, best_under_book = max(fair_unders, key=lambda x: x[0])

            use_crossbook = non_target_count >= 3
            contributing_books = [bk for _, bk in fair_overs]

            for side, consensus, best_val, best_book, target_price in [
                ("Over", consensus_over, best_over_val, best_over_book, target_data["Over"]),
                ("Under", consensus_under, best_under_val, best_under_book, target_data["Under"]),
            ]:
                fair_val = best_val if use_crossbook else consensus
                edge_method = "cross_book_best_line" if use_crossbook else "consensus_devig"

                target_implied = american_to_implied(target_price)
                ev = ev_binary(fair_val, american_to_decimal(target_price))
                kelly = kelly_binary(fair_val, american_to_decimal(target_price))
                edge = ev  # Use EV as edge metric (accounts for vig in odds)
                is_signal = ev >= edge_threshold

                events += 1
                if is_signal:
                    signals += 1

                event_id = game.get("id", "")

                await self._db.execute(
                    "INSERT INTO backtest_events "
                    "(run_id, event_id, hypothesis_id, sport, player, market, "
                    "line, side, book, book_odds_american, book_implied_prob, "
                    "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
                    "signal_generated, game_date, snapshot_time) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id, event_id, hypothesis_id, game.get("sport_key", ""),
                        player, mkt_key, line, side, target_book,
                        target_price, round(target_implied, 6),
                        round(fair_val, 6),
                        json.dumps({
                            "edge_method": edge_method,
                            "books_used": non_target_count,
                            "devig_method": devig_method,
                            "best_line_book": best_book,
                            "best_line_fair_prob": round(best_val, 6),
                            "consensus_fair_prob": round(consensus, 6),
                            "contributing_books": contributing_books,
                        }),
                        round(edge, 6), round(ev, 6), round(kelly, 6),
                        is_signal, game_date, snapshot_time,
                    ),
                )

        await self._db.commit()
        return events, signals

    async def resolve_with_scores(
        self,
        run_id: str,
        sport: str,
    ) -> dict:
        """
        Resolve backtest events using actual game results.
        Fetches scores from The Odds API (free endpoint).
        For player props, needs external stats source.

        Returns resolution summary.
        """
        from tools.odds_api import get_scores

        # Get unresolved events for this run
        cursor = await self._db.execute(
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
                if not game.get("completed"):
                    continue

                scores = game.get("scores", [])
                if not scores or len(scores) < 2:
                    continue

                home_score = None
                away_score = None
                for s in scores:
                    if s.get("name") == game.get("home_team"):
                        home_score = int(s.get("score", 0))
                    elif s.get("name") == game.get("away_team"):
                        away_score = int(s.get("score", 0))

                if home_score is None or away_score is None:
                    continue

                total_score = home_score + away_score
                margin = home_score - away_score

                # Resolve spreads, totals, h2h events
                ev_cursor = await self._db.execute(
                    "SELECT id, market, side, line, book_odds_american FROM backtest_events "
                    "WHERE run_id = ? AND event_id = ? AND actual_result IS NULL",
                    (run_id, event_id),
                )
                ev_rows = await ev_cursor.fetchall()

                for ev_id, market, side, line, odds in ev_rows:
                    result = self._resolve_line(
                        market, side, line, home_score, away_score,
                        game.get("home_team", ""), game.get("away_team", ""),
                    )
                    if result:
                        await self._db.execute(
                            "UPDATE backtest_events SET actual_result = ? WHERE id = ?",
                            (result, ev_id),
                        )
                        resolved_count += 1

        await self._db.commit()
        return {"run_id": run_id, "resolved": resolved_count}

    def _resolve_line(
        self,
        market: str,
        side: str,
        line: Optional[float],
        home_score: int,
        away_score: int,
        home_team: str,
        away_team: str,
    ) -> Optional[str]:
        """Determine if a bet won, lost, or pushed."""
        total = home_score + away_score
        margin = home_score - away_score

        if market == "h2h":
            if side.lower() == home_team.lower():
                return "won" if margin > 0 else "lost" if margin < 0 else "push"
            elif side.lower() == away_team.lower():
                return "won" if margin < 0 else "lost" if margin > 0 else "push"
            return None

        if market == "spreads" and line is not None:
            # side is the team name, line is their spread
            if side.lower() == home_team.lower():
                adjusted = margin + line
            else:
                adjusted = -margin + line

            if adjusted > 0:
                return "won"
            elif adjusted < 0:
                return "lost"
            return "push"

        if market == "totals" and line is not None:
            if side.lower() == "over":
                if total > line:
                    return "won"
                elif total < line:
                    return "lost"
                return "push"
            elif side.lower() == "under":
                if total < line:
                    return "won"
                elif total > line:
                    return "lost"
                return "push"

        return None

    @staticmethod
    def _normalize_team(name: str) -> str:
        """Normalize team name for fuzzy matching across data sources.

        Handles differences between Odds API names (e.g. "Los Angeles Dodgers")
        and ESPN names (e.g. "LA Dodgers", "Athletics", etc.).
        """
        if not name:
            return ""
        n = name.strip().lower()
        # Common abbreviation mappings
        replacements = {
            "los angeles": "la",
            "new york": "ny",
            "san francisco": "sf",
            "san antonio": "sa",
            "san diego": "sd",
            "golden state": "gs",
            "oklahoma city": "okc",
            "portland trail blazers": "portland blazers",
            "brooklyn": "bkn",
        }
        for full, abbrev in replacements.items():
            if n.startswith(full):
                n = abbrev + n[len(full):]
                break
        # Strip common prefixes/suffixes and extra whitespace
        n = " ".join(n.split())
        return n

    @staticmethod
    def _team_matches(name_a: str, name_b: str) -> bool:
        """Check if two team names refer to the same team.

        Handles: exact match, normalized match, last-word (mascot) match,
        and substring containment for abbreviated names.
        """
        if not name_a or not name_b:
            return False
        if name_a == name_b:
            return True

        a = BacktestEngine._normalize_team(name_a)
        b = BacktestEngine._normalize_team(name_b)

        if a == b:
            return True

        # Last word (mascot) match — "LA Dodgers" vs "Los Angeles Dodgers"
        a_last = a.rsplit(None, 1)[-1] if a else ""
        b_last = b.rsplit(None, 1)[-1] if b else ""
        if a_last == b_last and len(a_last) > 3:
            return True

        # Substring: "Athletics" matches "Oakland Athletics" or "Athletics"
        if a in b or b in a:
            return True

        return False

    async def resolve_from_game_results(
        self,
        run_id: Optional[str] = None,
        sport: Optional[str] = None,
    ) -> dict:
        """
        Resolve backtest events using the local game_results table.
        No API calls needed — matches on game_date + teams with fuzzy name matching.

        If run_id is given, resolves only that run's events.
        If sport is given without run_id, resolves all unresolved events for that sport.
        If neither, resolves everything possible.
        """
        # Build query for unresolved events
        if run_id:
            cursor = await self._db.execute(
                "SELECT id, event_id, sport, market, side, line, game_date, model_factors "
                "FROM backtest_events WHERE run_id = ? AND actual_result IS NULL",
                (run_id,),
            )
        elif sport:
            cursor = await self._db.execute(
                "SELECT id, event_id, sport, market, side, line, game_date, model_factors "
                "FROM backtest_events WHERE sport = ? AND actual_result IS NULL",
                (sport,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT id, event_id, sport, market, side, line, game_date, model_factors "
                "FROM backtest_events WHERE actual_result IS NULL",
            )

        unresolved = await cursor.fetchall()
        if not unresolved:
            return {"resolved": 0, "unresolved": 0}

        # Build a lookup of game results indexed by (sport, date) -> list of games
        result_cursor = await self._db.execute(
            "SELECT sport, game_date, home_team, away_team, home_score, away_score "
            "FROM game_results",
        )
        result_rows = await result_cursor.fetchall()

        # Index by (sport, date) for fuzzy team matching
        from collections import defaultdict
        games_by_date = defaultdict(list)
        for r_sport, r_date, r_home, r_away, r_hscore, r_ascore in result_rows:
            games_by_date[(r_sport, r_date)].append((r_home, r_away, r_hscore, r_ascore))
            games_by_date[("", r_date)].append((r_home, r_away, r_hscore, r_ascore))

        resolved_count = 0
        match_failures = 0
        for ev_id, event_id, ev_sport, market, side, line, game_date, model_factors in unresolved:
            # Extract home/away from event_id or model_factors
            home_team = ""
            away_team = ""

            if event_id and "|" in event_id:
                parts = event_id.split("|")
                if len(parts) >= 3:
                    home_team = parts[1]
                    away_team = parts[2]
            elif model_factors:
                try:
                    factors = json.loads(model_factors)
                    home_team = factors.get("home_team", "")
                    away_team = factors.get("away_team", "")
                except (json.JSONDecodeError, TypeError):
                    pass

            if not home_team or not away_team:
                continue

            # Fuzzy match: find the game in results for this date
            scores = None
            candidates = games_by_date.get((ev_sport, game_date), [])
            if not candidates:
                candidates = games_by_date.get(("", game_date), [])

            for r_home, r_away, r_hscore, r_ascore in candidates:
                if self._team_matches(home_team, r_home) and self._team_matches(away_team, r_away):
                    scores = (r_hscore, r_ascore)
                    break
                # Also try swapped home/away (data source differences)
                if self._team_matches(home_team, r_away) and self._team_matches(away_team, r_home):
                    scores = (r_ascore, r_hscore)
                    break

            if not scores:
                match_failures += 1
                continue

            home_score, away_score = scores
            if home_score is None or away_score is None:
                continue

            result = self._resolve_line(
                market, side, line, home_score, away_score, home_team, away_team
            )
            if result:
                await self._db.execute(
                    "UPDATE backtest_events SET actual_result = ? WHERE id = ?",
                    (result, ev_id),
                )
                resolved_count += 1

        await self._db.commit()
        if match_failures > 0:
            logger.warning(
                f"Resolution: {match_failures}/{len(unresolved)} events could not match "
                f"to game_results (missing game data or team name mismatch)"
            )
        logger.info(f"Resolved {resolved_count}/{len(unresolved)} backtest events from game_results")
        return {"resolved": resolved_count, "unresolved": len(unresolved) - resolved_count}

    async def generate_paper_trade_signal(
        self,
        hypothesis_id: str,
        live_odds: dict,
    ) -> list[dict]:
        """
        For paper trading: apply model to current live odds.
        Returns signals meeting threshold. Does NOT place bets.
        """
        h = await self.hypothesis_manager.get_hypothesis(hypothesis_id)
        if not h or h["status"] != "paper_trading":
            return []

        config = h["model_config"]
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (json.JSONDecodeError, TypeError):
                config = {}
        target_book = config.get("target_book", "draftkings")
        edge_threshold = h["edge_threshold"]
        devig_method = config.get("devig_method", "power")
        min_books = config.get("consensus_min_books", 2)

        signals = []
        games = live_odds.get("games", [])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now = datetime.now(timezone.utc).isoformat()

        for game in games:
            # Use same processing logic as backtest
            if h["market_type"].startswith("player_"):
                events, _ = await self._process_game_props(
                    run_id="paper",  # won't be stored via run
                    hypothesis_id=hypothesis_id,
                    game=game,
                    game_date=today,
                    snapshot_time=now,
                    market_type=h["market_type"],
                    target_book=target_book,
                    edge_threshold=edge_threshold,
                    devig_method=devig_method,
                    min_books=min_books,
                    config=config,
                )
            else:
                events, _ = await self._process_game_lines(
                    run_id="paper",
                    hypothesis_id=hypothesis_id,
                    game=game,
                    game_date=today,
                    snapshot_time=now,
                    market_type=h["market_type"],
                    target_book=target_book,
                    edge_threshold=edge_threshold,
                    devig_method=devig_method,
                    min_books=min_books,
                    config=config,
                    h_sport=h.get("sport", ""),
                )

        # Retrieve signals that were just generated with run_id="paper"
        cursor = await self._db.execute(
            "SELECT * FROM backtest_events "
            "WHERE run_id = 'paper' AND hypothesis_id = ? AND signal_generated = 1 "
            "AND game_date = ?",
            (hypothesis_id, today),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]

        for row in rows:
            event = dict(zip(cols, row))
            trade_id = str(uuid.uuid4())[:12]

            # Move to paper_trades table
            await self._db.execute(
                "INSERT OR IGNORE INTO paper_trades "
                "(trade_id, hypothesis_id, event_id, sport, player, market, "
                "line, side, book, signal_time, signal_odds_american, "
                "signal_implied_prob, model_fair_prob, edge, ev_pct, "
                "kelly_fraction, game_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_id, hypothesis_id, event["event_id"],
                    event["sport"], event.get("player"), event["market"],
                    event.get("line"), event["side"], event["book"],
                    now, event["book_odds_american"],
                    event["book_implied_prob"], event["model_fair_prob"],
                    event["edge"], event["ev_pct"],
                    event.get("kelly_fraction"), today,
                ),
            )
            # Also insert into signals table
            edge_val = event.get("edge", 0) or 0
            if edge_val > 0.05:
                confidence = "high"
            elif edge_val > 0.03:
                confidence = "medium"
            else:
                confidence = "low"
            await self._db.execute(
                "INSERT INTO signals "
                "(event_id, sport, signal_type, team, market, book, "
                "odds_american, fair_probability, fair_prob_source, "
                "edge_pct, ev_pct, confidence, kelly_fraction, "
                "recommended_stake, status, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.get("event_id"),
                    event["sport"],
                    "paper_trade",
                    event["side"],
                    event["market"],
                    event["book"],
                    event.get("book_odds_american", 0),
                    event.get("model_fair_prob", 0),
                    "cross_book_devig",
                    edge_val,
                    event.get("ev_pct", 0) or 0,
                    confidence,
                    event.get("kelly_fraction"),
                    None,
                    "paper",
                    f"hypothesis_id={hypothesis_id}, trade_id={trade_id}",
                ),
            )

            signals.append({
                "trade_id": trade_id,
                **event,
            })

        # Clean up temporary paper events from backtest_events
        await self._db.execute(
            "DELETE FROM backtest_events WHERE run_id = 'paper' AND hypothesis_id = ?",
            (hypothesis_id,),
        )
        await self._db.commit()

        return signals

    async def get_run_results(self, run_id: str) -> dict:
        """Retrieve full backtest results for a run."""
        # Run metadata
        cursor = await self._db.execute(
            "SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,),
        )
        run_row = await cursor.fetchone()
        if not run_row:
            return {"error": "Run not found"}
        run_cols = [d[0] for d in cursor.description]
        run = dict(zip(run_cols, run_row))

        # Signal events
        ev_cursor = await self._db.execute(
            "SELECT * FROM backtest_events "
            "WHERE run_id = ? AND signal_generated = 1 "
            "ORDER BY edge DESC LIMIT 100",
            (run_id,),
        )
        ev_rows = await ev_cursor.fetchall()
        ev_cols = [d[0] for d in ev_cursor.description]
        signals = [dict(zip(ev_cols, r)) for r in ev_rows]

        # Aggregate stats
        stats_cursor = await self._db.execute(
            "SELECT "
            "COUNT(*) as total, "
            "SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END) as signals, "
            "AVG(CASE WHEN signal_generated = 1 THEN edge END) as avg_edge, "
            "AVG(CASE WHEN signal_generated = 1 THEN ev_pct END) as avg_ev, "
            "SUM(CASE WHEN actual_result = 'won' THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN actual_result = 'lost' THEN 1 ELSE 0 END) as losses, "
            "SUM(CASE WHEN actual_result = 'push' THEN 1 ELSE 0 END) as pushes "
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
