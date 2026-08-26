"""Paper-trade signal pipeline extracted from tools/backtest.py (slice 4).

``generate_paper_trade_signal`` is the live-ish read path: it applies the
model to current odds and writes paper trades + signals rows. The HARD
GATE (paper_trading status only) stays in the facade method before this
pipeline is ever reached — see tools/signals/paper.py for the canonical
status gate. This module assumes the gate already passed; it never widens
or re-checks the allowed statuses itself.

tools/backtest.py remains the public facade: BacktestEngine re-binds
``_generate_paper_trade_signal_body`` as the method body, so call sites
and signatures are unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from tools.btest.events_io import (
    dedup_best_edge_by_event,
    new_trade_id,
    signal_confidence,
)
from tools.btest.paper_diagnostics import (
    edge_distribution,
    suppression_reasons,
)
from tools.signals.schedule import game_date_from_commence

logger = logging.getLogger("callisto.backtest")


async def generate_paper_trade_signal(
    engine,
    hypothesis_id: str,
    live_odds: dict,
) -> list[dict]:
    """
    For paper trading: apply model to current live odds.
    Returns signals meeting threshold. Does NOT place bets.

    Caller (BacktestEngine.generate_paper_trade_signal) owns the HARD GATE:
    only hypotheses with status exactly "paper_trading" reach this body.
    """
    db = engine._db
    h = await engine.hypothesis_manager.get_hypothesis(hypothesis_id)
    if not h:
        return []

    config = h["model_config"]
    if isinstance(config, str):
        import json as _json
        try:
            config = _json.loads(config)
        except (_json.JSONDecodeError, TypeError):
            config = {}
    target_book = config.get("target_book", "draftkings")
    edge_threshold = h["edge_threshold"]
    devig_method = config.get("devig_method", "power")
    min_books = config.get("consensus_min_books", 3)

    signals = []
    games = live_odds.get("games", [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()

    def _game_date_from_commence(game_obj: dict) -> str:
        """Thin wrapper over the canonical helper in tools.signals.schedule."""
        return game_date_from_commence(game_obj, sport=sport, today=today)

    # Parse hypothesis-specific filters (same as main backtest path)
    thesis = h.get("thesis", "")
    h_name = h.get("name", "")
    sport = h.get("sport", "")
    filters = engine._parse_hypothesis_filters(thesis, config, h_name)

    # ── Build schedule context for game-level filtering (matches backtest path) ──
    # Without this, context-based hypotheses (b2b, road_trip, rest, etc.)
    # will NEVER produce signals because _game_matches_context_filter fails closed.
    use_context_filter = engine._needs_context_filter(h_name, thesis, config)
    schedule_context = {}
    context_filtered = 0
    if use_context_filter and sport:
        # Use 30-day lookback so _build_schedule_context can compute
        # days_rest, b2b, road_streak etc. from prior game_results.
        # A 1-day window causes all teams to get defaults (b2b=False,
        # days_rest=99) which then fail context filters → 0 trades.
        context_start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        # Pass today's live games so context is computed for upcoming
        # games not yet in game_results (the previous code only had
        # context for completed games → today's games always got {} →
        # fail-closed filter rejected them all → 0 paper trades).
        live_game_tuples = [
            (today, g.get("home_team", ""), g.get("away_team", ""))
            for g in games
            if g.get("home_team") and g.get("away_team")
        ]
        schedule_context = await engine._build_schedule_context(
            sport, context_start, today,
            live_games=live_game_tuples,
        )
        if schedule_context:
            logger.info(
                f"Paper trade {hypothesis_id}: context filter ENABLED — "
                f"{len(schedule_context)} games have schedule context"
            )
        else:
            logger.warning(
                f"Paper trade {hypothesis_id}: context filter ENABLED but "
                f"schedule_context is EMPTY — falling through WITHOUT context filter"
            )
            use_context_filter = False  # fail-open: proceed without context gating

    all_paper_rows: list[tuple] = []
    # Map event_id → (home_team, away_team, game_date) for paper trade insertion
    _paper_game_info: dict[str, tuple[str, str, str]] = {}
    total_events = 0
    total_signals_found = 0
    games_processed = 0
    for game in games:
        # ── Game-level context filter (same as backtest path) ──
        if use_context_filter:
            if not schedule_context:
                context_filtered += 1
                continue
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            game_ctx = schedule_context.get((today, home, away), {})
            if not engine._game_matches_context_filter(
                game_ctx, h_name, thesis, config,
            ):
                context_filtered += 1
                continue

        games_processed += 1
        # Derive actual game date from commence_time (not signal date)
        actual_game_date = _game_date_from_commence(game)
        g_home = game.get("home_team", "")
        g_away = game.get("away_team", "")
        g_eid = game.get("id", "")
        if g_eid:
            _paper_game_info[g_eid] = (g_home, g_away, actual_game_date)

        # Use same processing logic as backtest
        if h["market_type"].startswith("player_"):
            events, _ = await engine._process_game_props(
                run_id="paper",  # won't be stored via run
                hypothesis_id=hypothesis_id,
                game=game,
                game_date=actual_game_date,
                snapshot_time=now,
                market_type=h["market_type"],
                target_book=target_book,
                edge_threshold=edge_threshold,
                devig_method=devig_method,
                min_books=min_books,
                config=config,
                filters=filters,
            )
            total_events += events
        else:
            events, sigs, _paper_rows = await engine._process_game_lines(
                run_id="paper",
                hypothesis_id=hypothesis_id,
                game=game,
                game_date=actual_game_date,
                snapshot_time=now,
                market_type=h["market_type"],
                target_book=target_book,
                edge_threshold=edge_threshold,
                devig_method=devig_method,
                min_books=min_books,
                config=config,
                h_sport=sport,
                filters=filters,
            )
            total_events += events
            total_signals_found += sigs
            all_paper_rows.extend(_paper_rows)

    # Edge distribution diagnostic — shows why 0-signal cycles happen
    diag = edge_distribution(all_paper_rows)
    max_edge = diag["max_edge"]
    min_edge = diag["min_edge"]
    above_thresh = sum(1 for row in all_paper_rows if row[13] >= edge_threshold)
    min_books_seen = diag["min_books_seen"]
    max_books_seen = diag["max_books_seen"]

    # Diagnose WHY above_thresh > 0 but signals = 0 (prevents false "broken" alarms)
    suppression_reasons_list = []
    if above_thresh > 0 and total_signals_found == 0 and all_paper_rows:
        suppression_reasons_list = suppression_reasons(
            all_paper_rows, edge_threshold, h["market_type"]
        )

    logger.info(
        f"Paper trade {hypothesis_id[:12]}: {games_processed}/{len(games)} games processed, "
        f"{total_events} events, {total_signals_found} signals, "
        f"{len(all_paper_rows)} pending rows, "
        f"market={h['market_type']}, filters={filters}, threshold={edge_threshold}, "
        f"edge_range=[{min_edge:.4f}, {max_edge:.4f}], above_thresh={above_thresh}, "
        f"books_range=[{min_books_seen}, {max_books_seen}]"
    )
    if suppression_reasons_list:
        logger.info(
            f"Paper trade {hypothesis_id[:12]}: {above_thresh} edge(s) above threshold "
            f"SUPPRESSED — {'; '.join(suppression_reasons_list)}"
        )

    # Batch-insert paper events so the SELECT below can find signals.
    # _process_game_lines returns pending rows (deferred write pattern)
    # but never inserts them — the caller must do it.
    if all_paper_rows:
        await db.executemany(
            "INSERT OR IGNORE INTO backtest_events "
            "(run_id, event_id, hypothesis_id, sport, player, market, "
            "line, side, book, book_odds_american, book_implied_prob, "
            "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
            "signal_generated, game_date, snapshot_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            all_paper_rows,
        )
        await db.commit()

    if context_filtered:
        logger.info(
            f"Paper trade {hypothesis_id}: {context_filtered} games "
            f"filtered by context, {len(games) - context_filtered} processed"
        )

    # Retrieve signals that were just generated with run_id="paper"
    cursor = await db.execute(
        "SELECT * FROM backtest_events "
        "WHERE run_id = 'paper' AND hypothesis_id = ? AND signal_generated = 1 "
        "AND game_date = ?",
        (hypothesis_id, today),
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    row_dicts = [dict(zip(cols, row)) for row in rows]

    # ── Game-level dedup: keep only best-edge book per game ──
    deduped_events = dedup_best_edge_by_event(row_dicts)
    multi_book_skipped = len(rows) - len(deduped_events)
    if multi_book_skipped:
        logger.info(
            f"Paper trade {hypothesis_id[:12]}: kept {len(deduped_events)} "
            f"best-edge trades, skipped {multi_book_skipped} multi-book duplicates"
        )

    dupes_skipped = 0
    for event in deduped_events:

        # Look up game info (home_team, away_team, actual game_date)
        eid = event.get("event_id", "")
        gi = _paper_game_info.get(eid, ("", "", event.get("game_date", today)))
        home_team, away_team, actual_gd = gi

        # ── Dedup: skip if we already recorded this game for this hypothesis ──
        dup_cur = await db.execute(
            "SELECT 1 FROM paper_trades "
            "WHERE hypothesis_id = ? AND game_date = ? AND home_team = ? AND away_team = ?",
            (hypothesis_id, actual_gd, home_team, away_team),
        )
        if await dup_cur.fetchone():
            dupes_skipped += 1
            continue

        trade_id = new_trade_id()

        # Move to paper_trades table. ``actual_gd`` is already the
        # venue-local date (see _game_date_from_commence above) — write
        # it to BOTH game_date (legacy) and local_game_date (canonical)
        # so new rows don't need a backfill.
        await db.execute(
            "INSERT OR IGNORE INTO paper_trades "
            "(trade_id, hypothesis_id, event_id, sport, player, market, "
            "line, side, book, signal_time, signal_odds_american, "
            "signal_implied_prob, model_fair_prob, edge, ev_pct, "
            "kelly_fraction, game_date, local_game_date, "
            "home_team, away_team) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade_id, hypothesis_id, eid,
                event["sport"], event.get("player"), event["market"],
                event.get("line"), event["side"], event["book"],
                now, event["book_odds_american"],
                event["book_implied_prob"], event["model_fair_prob"],
                event["edge"], event["ev_pct"],
                event.get("kelly_fraction"), actual_gd, actual_gd,
                home_team, away_team,
            ),
        )
        # Also insert into signals table
        edge_val = event.get("edge", 0) or 0
        confidence = signal_confidence(edge_val)
        await db.execute(
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

    if dupes_skipped:
        logger.info(
            f"Paper trade {hypothesis_id[:12]}: skipped {dupes_skipped} "
            f"duplicate trades (already recorded)"
        )

    # Clean up temporary paper events from backtest_events
    await db.execute(
        "DELETE FROM backtest_events WHERE run_id = 'paper' AND hypothesis_id = ?",
        (hypothesis_id,),
    )
    await db.commit()

    return signals
