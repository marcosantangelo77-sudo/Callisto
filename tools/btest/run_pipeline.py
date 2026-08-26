"""Full backtest run pipeline extracted from tools/backtest.py (slice 4).

``run_backtest`` is the core replay loop: load hypothesis config, fetch
historical odds, process every game's lines into pending event rows,
batch-write everything in one deferred transaction, then hand off to
resolution + significance evaluation.

The facade method on BacktestEngine delegates here with ``engine`` as the
first argument; all state (hypothesis_manager, historical_fetcher, _db,
db_path) is read off the engine so no behavior changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("callisto.backtest")


async def run_backtest(
    engine,
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
    h = await engine.hypothesis_manager.get_hypothesis(hypothesis_id)
    if not h:
        return {"error": "Hypothesis not found"}

    config = h["model_config"]
    sport = h["sport"]
    market_type = h["market_type"]
    edge_threshold = h["edge_threshold"]
    thesis = h.get("thesis", "")
    h_name = h.get("name", hypothesis_id)  # Readable name for filter parsing
    target_book = config.get("target_book", "draftkings")
    devig_method = config.get("devig_method", "power")
    min_books = config.get("consensus_min_books", 3)

    # ── SPRING TRAINING GATE ──
    # MLB spring training (Feb-late March) uses split-squad rosters, shortened
    # starts, and matchups that don't appear in any results source. Backtesting
    # against spring training produces unresolvable events. Skip it.
    if sport == "baseball_mlb":
        from datetime import datetime as _dt_check
        try:
            end_dt = _dt_check.strptime(end_date, "%Y-%m-%d")
            # MLB Opening Day varies by year. Use dynamic detection: check if
            # game_results has MLB games before falling back to a conservative
            # date. 2026 season started March 20.
            mlb_season_start = _dt_check(end_dt.year, 3, 20)
            try:
                if engine.db:
                    # Check for actual MLB regular season games in game_results
                    cursor = await engine.db.execute(
                        "SELECT MIN(game_date) FROM game_results "
                        "WHERE sport = 'baseball_mlb' AND game_date >= ? "
                        "AND game_date LIKE ?",
                        (f"{end_dt.year}-03-01", f"{end_dt.year}-%"),
                    )
                    row = await cursor.fetchone()
                    if row and row[0]:
                        actual_start = _dt_check.strptime(row[0][:10], "%Y-%m-%d")
                        mlb_season_start = actual_start
            except Exception:
                pass  # Fall back to March 20 default
            if end_dt < mlb_season_start:
                return {
                    "hypothesis_id": hypothesis_id,
                    "hypothesis_name": h_name,
                    "error": "spring_training",
                    "detail": (
                        f"Date range [{start_date} .. {end_date}] falls entirely "
                        f"in MLB spring training. Spring training matchups from "
                        f"odds sources don't match actual results — skipping."
                    ),
                    "total_events": 0,
                    "signals_generated": 0,
                }
            # Clamp start_date to season start if range spans the boundary
            start_dt = _dt_check.strptime(start_date, "%Y-%m-%d")
            if start_dt < mlb_season_start:
                start_date = mlb_season_start.strftime("%Y-%m-%d")
                logger.info(
                    f"Backtest {hypothesis_id}: clamped MLB start_date to "
                    f"{start_date} (skip spring training)"
                )
        except ValueError:
            pass

    # ── HYPOTHESIS-AWARE FILTERING ──
    # Parse line-based filters from thesis text, model_config, and name (Tier 1)
    filters = engine._parse_hypothesis_filters(thesis, config, h_name)
    if filters:
        logger.info(
            f"Backtest {hypothesis_id}: applying hypothesis filters: {filters}"
        )
    else:
        logger.info(
            f"Backtest {hypothesis_id}: no line-based filters parsed — "
            f"processing all lines (generic cross-book edge detection)"
        )

    # HARD GATE (2026-04-22 FWER audit): binary-both-sides markets
    # (totals O/U, h2h ML) without a side_filter double-count events —
    # each game produces rows for BOTH sides, diluting/selection-biasing
    # the signal population.  Refuse to run unless:
    #   - the hypothesis config sets model_config['legacy']=True  (grandfather), OR
    #   - CALLISTO_ALLOW_BOTH_SIDES=1 is set in the env (emergency override)
    _binary_both = market_type in ("totals", "h2h")
    _has_side = "side_filter" in filters
    _allow_both = os.getenv("CALLISTO_ALLOW_BOTH_SIDES", "0").strip() in ("1", "true", "yes")
    _is_legacy = bool((config or {}).get("legacy") is True)
    if _binary_both and not _has_side and not _allow_both and not _is_legacy:
        logger.error(
            f"Backtest {hypothesis_id}: REJECTED — market_type={market_type} "
            f"requires side_filter.  Add 'side_filter' to model_config or "
            f"encode 'over'/'under'/'home'/'away' in thesis/name, or set "
            f"CALLISTO_ALLOW_BOTH_SIDES=1, or mark model_config['legacy']=True."
        )
        return {
            "hypothesis_id": hypothesis_id,
            "hypothesis_name": h_name,
            "error": "side_filter_required",
            "detail": (
                f"Binary-both-sides hypothesis ({market_type}) without "
                f"side_filter — cannot run.  Both sides would be evaluated "
                f"and double-count events, selection-biasing the signal "
                f"set.  Fix: set model_config['side_filter']='Over'|'Under' "
                f"(or 'home'|'away'), or mark legacy=True for grandfather."
            ),
            "total_events": 0,
            "signals_generated": 0,
            "hypothesis_filters": filters if filters else {},
        }
    if _binary_both and not _has_side and (_allow_both or _is_legacy):
        logger.warning(
            f"Backtest {hypothesis_id}: ALLOW_BOTH_SIDES bypass — legacy="
            f"{_is_legacy}, env_override={_allow_both}.  BOTH sides will "
            f"be evaluated (2x events, diluted signal)."
        )

    # Log unfilterable context factors (Tier 2)
    unfilterable = engine._log_unfilterable_context_factors(hypothesis_id, config)
    context_coverage = engine.compute_context_coverage(config)

    # ── INFER MISSING CONTEXT FACTORS ──
    inferred_unfilterable = engine._infer_context_needs(thesis, h_name)
    if inferred_unfilterable:
        existing = set(
            f.lower().replace(" ", "_") for f in config.get("context_factors", [])
        )
        merged = existing | set(inferred_unfilterable)
        filterable_in_merged = sum(
            1 for f in merged
            if f in type(engine).FILTERABLE_CONTEXT_FACTORS
        )
        context_coverage = filterable_in_merged / len(merged) if merged else 1.0
        unfilterable = list(set(unfilterable or []) | set(inferred_unfilterable))
        logger.warning(
            f"Backtest {hypothesis_id} ({h_name}): inferred unfilterable "
            f"context needs from thesis/name: {inferred_unfilterable}. "
            f"Effective coverage after merge: {context_coverage:.0%}."
        )

    structured = engine.has_structured_filters(config)
    if context_coverage < 0.5 and not structured:
        logger.warning(
            f"Backtest {hypothesis_id}: context_coverage={context_coverage:.0%} — "
            f"most game-selection conditions are unfilterable. Results will be "
            f"indistinguishable from testing ALL games in the sport/market."
        )
        # ── HARD GATE: skip backtests that can't filter meaningfully ──
        return {
            "hypothesis_id": hypothesis_id,
            "hypothesis_name": h_name,
            "error": "untestable",
            "detail": (
                f"Context coverage is {context_coverage:.0%}. "
                f"Unfilterable factors: {unfilterable}. "
                f"Without game-level context data, this hypothesis tests "
                f"ALL {sport} games indistinguishably from other hypotheses."
            ),
            "context_coverage": context_coverage,
            "unfilterable_context_factors": unfilterable,
            "hypothesis_filters": filters if filters else {},
            "suggestion": (
                "Either: (1) add venue/weather/player enrichment data to enable "
                "filtering, or (2) restructure as a pure line-based hypothesis "
                "(home underdog, spread range, etc.), or (3) add context_factors "
                "to model_config and populate the corresponding data."
            ),
        }
    elif context_coverage < 0.5 and structured:
        logger.info(
            f"Backtest {hypothesis_id}: context_coverage={context_coverage:.0%} but "
            f"has structured line_filters/game_filters — proceeding with backtest. "
            f"Unfilterable factors (informational): {unfilterable}"
        )

    # ── DATE RANGE SAFETY ──
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if end_date >= today_str:
        end_date = str(datetime.now(timezone.utc).date() - timedelta(days=1))
        logger.info(f"Backtest {hypothesis_id}: capped end_date to {end_date} (today's games unfinished)")
    if start_date > end_date:
        return {
            "error": "No valid date range",
            "detail": f"start_date {start_date} > end_date {end_date} after capping at yesterday",
            "hypothesis_id": hypothesis_id,
        }

    # ── TEMPORAL ISOLATION ENFORCEMENT ──
    from tools.temporal_analysis import validate_temporal_isolation
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

    # ── DUPLICATE BACKTEST DETECTION ──
    context_factors_sorted = sorted(config.get("context_factors", []))
    uses_context = engine._needs_context_filter(h_name, thesis, config)
    game_filters = config.get("game_filters")
    line_filters = config.get("line_filters")
    fp_parts = json.dumps(
        {"sport": sport, "market": market_type, "start": start_date,
         "end": end_date, "filters": filters, "target": target_book,
         "threshold": edge_threshold, "devig": devig_method, "min_books": min_books,
         "context_factors": context_factors_sorted,
         "uses_context": uses_context,
         "game_filters": game_filters,
         "line_filters": line_filters,
         "hypothesis_name": h_name},
        sort_keys=True,
    )
    fingerprint = hashlib.md5(fp_parts.encode()).hexdigest()[:16]

    # Primary check: exact fingerprint match (includes filters, threshold, etc.)
    existing = await engine._db.execute_fetchall(
        """SELECT br.hypothesis_id, br.run_id, br.total_events, br.signals_generated,
                  br.hit_rate, br.avg_edge, br.is_significant, h.name
           FROM backtest_runs br
           JOIN hypotheses h ON h.hypothesis_id = br.hypothesis_id
           WHERE json_extract(br.run_config, '$.backtest_fingerprint') = ?
             AND br.hypothesis_id != ?
             AND br.total_events > 0
           LIMIT 1""",
        (fingerprint, hypothesis_id),
    )
    if existing:
        dup = existing[0]
        logger.warning(
            f"Backtest {hypothesis_id} ({h_name}): DUPLICATE of {dup[7]} "
            f"({dup[0]}) — same sport/market/dates/filters. "
            f"Prior run had {dup[2]} events, {dup[3]} signals. Skipping."
        )
        return {
            "hypothesis_id": hypothesis_id,
            "hypothesis_name": h_name,
            "error": "duplicate_backtest",
            "detail": (
                f"Identical backtest already ran for hypothesis '{dup[7]}' "
                f"(run {dup[1]}): {dup[2]} events, {dup[3]} signals, "
                f"avg_edge={dup[5]}. Same sport/market/dates/filters."
            ),
            "duplicate_of": dup[0],
            "duplicate_run": dup[1],
            "fingerprint": fingerprint,
        }

    # Embed fingerprint in config for future detection
    config["backtest_fingerprint"] = fingerprint

    run_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()

    # DEFERRED WRITE: Don't INSERT backtest_runs yet — the write lock
    # contention with line_monitor causes 5-minute blocks. Do all reads
    # first (computing events/signals), then batch-write at the end.
    _deferred_status_update = h["status"] == "draft"

    # Fetch historical data
    logger.info(f"Backtest {run_id}: fetching {sport} odds {start_date} to {end_date}")

    # Determine which markets to fetch based on hypothesis type
    is_prop_hypothesis = market_type.startswith("player_")

    if is_prop_hypothesis:
        fetch_markets = "h2h,spreads,totals"  # Still need game-level for context
        prop_lines = await engine.historical_fetcher.fetch_prop_snapshots(
            sport=sport,
            start_date=start_date,
            end_date=end_date,
            market_type=market_type,
        )
        logger.info(
            f"Backtest {run_id}: fetched {len(prop_lines)} prop lines from prop_snapshots"
        )
    else:
        fetch_markets = "h2h,spreads,totals"
        prop_lines = []

    fetch_result = await engine.historical_fetcher.bulk_fetch_date_range(
        sport=sport,
        start_date=start_date,
        end_date=end_date,
        markets=fetch_markets,
        credit_budget=credit_budget,
    )

    logger.info(
        f"Backtest {run_id}: fetched {fetch_result.get('dates_fetched', 0)} dates, "
        f"{fetch_result.get('dates_cached_already', 0)} cached"
    )

    # Process each cached date
    all_dates = await engine.historical_fetcher.get_cached_dates(sport)
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
    context_filtered = 0
    all_pending_rows: list[tuple] = []  # Collect ALL event rows for batch INSERT

    # ── Pre-compute schedule context for game-level filtering ──
    use_context_filter = engine._needs_context_filter(h_name, thesis, config)
    schedule_context = {}
    if use_context_filter:
        schedule_context = await engine._build_schedule_context(
            sport, start_date, end_date,
        )
        if schedule_context:
            logger.info(
                f"Backtest {hypothesis_id}: context filter ENABLED — "
                f"{len(schedule_context)} games have schedule context"
            )
        else:
            logger.warning(
                f"Backtest {hypothesis_id}: context filter ENABLED but "
                f"schedule_context is EMPTY — all games will be rejected (fail-closed)"
            )

    # Track aggregate snapshot-quality mix across the whole run so the
    # promotion gate downstream can enforce the >=80% pre_commence rule.
    run_quality_mix = {"pre_commence": 0, "closing_fallback": 0, "closing_mode": 0}

    for date_str in dates_in_range:
        snapshot = await engine.historical_fetcher.fetch_historical_odds(
            sport=sport, date=date_str, markets=fetch_markets,
        )

        snapshot = await engine._enrich_snapshot_with_multibook(
            sport, date_str, snapshot, target_book,
        )
        games = snapshot.get("games", [])
        snapshot_time = snapshot.get("timestamp", date_str)
        # Aggregate the lookahead-mix summary emitted by historical_odds.py
        _snap_mix = snapshot.get("snapshot_quality_mix") or {}
        for k in run_quality_mix:
            run_quality_mix[k] += int(_snap_mix.get(k, 0) or 0)
        # Release the top-level snapshot dict early — games list is all we need
        del snapshot

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
            # ── Sport-level defense-in-depth filter ──
            game_sport = game.get("sport_key", "")
            if not game_sport or game_sport != sport:
                continue

            # ── Game-level context filter ──
            if use_context_filter:
                if not schedule_context:
                    # No schedule data available — fail CLOSED.
                    context_filtered += 1
                    continue
                home = game.get("home_team", "")
                away = game.get("away_team", "")
                game_ctx = schedule_context.get((date_str, home, away), {})
                if not engine._game_matches_context_filter(
                    game_ctx, h_name, thesis, config,
                ):
                    context_filtered += 1
                    continue

            events, signals, rows = await engine._process_game(
                run_id=run_id,
                hypothesis_id=hypothesis_id,
                game=game,
                game_date=date_str,
                snapshot_time=snapshot_time,
                market_type=market_type,
                target_book=target_book,
                edge_threshold=edge_threshold,
                devig_method=devig_method,
                min_books=min_books,
                config=config,
                h_sport=sport,
                thesis=thesis,
                filters=filters,
            )
            total_events += events
            total_signals += signals
            all_pending_rows.extend(rows)

    if context_filtered > 0:
        logger.info(
            f"Backtest {run_id}: context filter removed {context_filtered} games "
            f"that didn't match schedule requirements"
        )

    # ── Player prop backtesting from prop_snapshots ──
    if is_prop_hypothesis and prop_lines:
        events_from_props, signals_from_props = await engine._process_prop_snapshots(
            run_id=run_id,
            hypothesis_id=hypothesis_id,
            prop_lines=prop_lines,
            target_book=target_book,
            edge_threshold=edge_threshold,
            devig_method=devig_method,
            config=config,
            h_sport=sport,
            filters=filters,
        )
        total_events += events_from_props
        total_signals += signals_from_props
        logger.info(
            f"Backtest {run_id}: prop snapshots produced "
            f"{events_from_props} events, {signals_from_props} signals"
        )

    # ── Compound filter fallback ──
    if total_events == 0 and context_filtered > 0:
        logger.warning(
            f"Backtest {run_id}: COMPOUND FILTER KILLED ALL EVENTS — "
            f"{context_filtered} games filtered, 0 survived. "
            f"Hypothesis may need simpler context requirements."
        )

    logger.info(
        f"Backtest {run_id}: {multibook_dates} dates with multi-book data, "
        f"{singlebook_skipped} dates with single-book only (no cross-book edges)"
    )

    # Persist lookahead-mode summary into run_config so the promotion gate
    # and re-eval harness can audit which lead_minutes + snapshot_quality
    # mix produced these stats.
    try:
        _lead_minutes = int(os.getenv("CALLISTO_BACKTEST_LEAD_MINUTES", "60"))
    except (ValueError, TypeError):
        _lead_minutes = 60
    config = dict(config) if isinstance(config, dict) else {"raw": config}
    config["_lookahead"] = {
        "lead_minutes": _lead_minutes,
        "snapshot_quality_mix": run_quality_mix,
    }

    # ── DEFERRED WRITE: batch ALL writes into one transaction ──
    _diag_parts = []
    for _dname, _ddb in [
        ("self._db", engine._db),
        ("self.db", getattr(engine, "db", None)),
    ]:
        if _ddb and hasattr(_ddb, "_conn") and _ddb._conn:
            try:
                _in_tx = _ddb._conn.in_transaction
                _diag_parts.append(f"{_dname}.in_transaction={_in_tx}")
            except Exception:
                _diag_parts.append(f"{_dname}=check_failed")
    logger.info(
        f"Backtest {run_id}: starting deferred write — "
        f"{total_events} events, {len(all_pending_rows)} rows, "
        f"status_update={_deferred_status_update}, "
        f"conn_state=[{', '.join(_diag_parts)}]"
    )
    completed = datetime.now(timezone.utc).isoformat()
    # WriteCoordinator path (single-writer pattern).
    try:
        from tools.db_writer import get_writer_if_running
        coord = get_writer_if_running(engine.db_path)
    except Exception:
        coord = None
    if coord is not None:
        ops: list[tuple[str, tuple]] = [
            (
                "INSERT OR REPLACE INTO backtest_runs "
                "(run_id, hypothesis_id, date_range_start, date_range_end, "
                "started_at, run_config, total_events, signals_generated, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, hypothesis_id, start_date, end_date, now,
                 json.dumps(config), total_events, total_signals, completed),
            ),
        ]
        if _deferred_status_update:
            ops.append((
                "UPDATE hypotheses SET status = 'backtesting', "
                "updated_at = datetime('now') WHERE hypothesis_id = ? "
                "AND status = 'draft'",
                (hypothesis_id,),
            ))
        await coord.transaction(ops)
        if all_pending_rows:
            await coord.executemany(
                "INSERT OR IGNORE INTO backtest_events "
                "(run_id, event_id, hypothesis_id, sport, player, market, "
                "line, side, book, book_odds_american, book_implied_prob, "
                "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
                "signal_generated, game_date, snapshot_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                all_pending_rows,
            )
    else:
        # Legacy path: open a per-write connection, retry on lock.
        import random as _rnd_bt
        import aiosqlite as _aiosqlite_bt
        for _bt_write_attempt in range(5):
            _write_db = None
            try:
                _write_db = await _aiosqlite_bt.connect(engine.db_path)
                await _write_db.execute("PRAGMA busy_timeout = 60000")
                await _write_db.execute("PRAGMA journal_mode = WAL")
                await _write_db.execute("PRAGMA synchronous = NORMAL")
                await _write_db.execute(
                    "INSERT OR REPLACE INTO backtest_runs "
                    "(run_id, hypothesis_id, date_range_start, date_range_end, "
                    "started_at, run_config, total_events, signals_generated, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, hypothesis_id, start_date, end_date, now,
                     json.dumps(config), total_events, total_signals, completed),
                )
                if _deferred_status_update:
                    await _write_db.execute(
                        "UPDATE hypotheses SET status = 'backtesting', "
                        "updated_at = datetime('now') WHERE hypothesis_id = ? "
                        "AND status = 'draft'",
                        (hypothesis_id,),
                    )
                if all_pending_rows:
                    await _write_db.executemany(
                        "INSERT OR IGNORE INTO backtest_events "
                        "(run_id, event_id, hypothesis_id, sport, player, market, "
                        "line, side, book, book_odds_american, book_implied_prob, "
                        "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
                        "signal_generated, game_date, snapshot_time) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        all_pending_rows,
                    )
                await _write_db.commit()
                await _write_db.close()
                break
            except Exception as _bw_e:
                if _write_db:
                    try:
                        await _write_db.close()
                    except Exception:
                        pass
                if "locked" in str(_bw_e).lower() and _bt_write_attempt < 4:
                    _wait = min(2 * (2 ** _bt_write_attempt), 15) + _rnd_bt.uniform(0, 1)
                    logger.warning(
                        f"Backtest {run_id} deferred write locked "
                        f"(attempt {_bt_write_attempt + 1}/5), retrying in {_wait:.1f}s"
                    )
                    await asyncio.sleep(_wait)
                else:
                    raise

    logger.info(
        f"Backtest {run_id} complete: {total_events} events, {total_signals} signals"
    )

    # ── Copy signal events to signals table ──
    if total_signals > 0:
        await engine._populate_signals_from_backtest(run_id, hypothesis_id)

    # Resolve outcomes using local game_results table
    resolution = await engine.resolve_from_game_results(run_id=run_id, sport=sport)
    logger.info(
        f"Backtest {run_id}: resolved {resolution['resolved']} events "
        f"({resolution['unresolved']} unresolved)"
    )

    # Run significance evaluation
    sig_report = await engine.hypothesis_manager.evaluate_significance(
        hypothesis_id, "backtest"
    )

    # Update run with statistical results from signal evaluation
    if sig_report.get("sample_size", 0) > 0:
        sig = sig_report.get("significance", {})
        risk = sig_report.get("risk", {})
        edge = sig_report.get("edge_metrics", {})
        clv = sig_report.get("clv", {})
        results = sig_report.get("results", {})
        cal_score = sig_report.get("calibration_score", {})

        await engine._db.execute(
            "UPDATE backtest_runs SET "
            "actual_win = ?, actual_loss = ?, actual_push = ?, "
            "hit_rate = ?, avg_edge = ?, avg_ev = ?, avg_clv = ?, "
            "roi_pct = ?, p_value_binomial = ?, p_value_ttest = ?, "
            "z_score = ?, sharpe_ratio = ?, max_drawdown = ?, "
            "is_significant = ?, "
            "sortino_ratio_val = ?, brier_score = ?, information_coefficient = ? "
            "WHERE run_id = ?",
            (
                results.get("wins", 0), results.get("losses", 0),
                results.get("pushes", 0), results.get("hit_rate"),
                edge.get("avg_edge"), edge.get("avg_ev"),
                clv.get("avg_clv"), edge.get("roi_pct"),
                sig.get("p_value_binomial"), sig.get("p_value_ttest"),
                sig.get("z_score"), risk.get("sharpe_ratio"),
                risk.get("max_drawdown"), sig.get("is_significant", False),
                risk.get("sortino_ratio"),
                cal_score.get("brier_score"),
                cal_score.get("information_coefficient"),
                run_id,
            ),
        )
        await engine._db.commit()
    else:
        # No signal events — still populate run stats from ALL resolved events
        updated = await engine.recalculate_run_stats(run_id)
        if updated:
            logger.info(
                f"Backtest {run_id}: 0 signal events but populated run stats "
                f"from resolved events"
            )

    return {
        "run_id": run_id,
        "hypothesis_id": hypothesis_id,
        "date_range": f"{start_date} to {end_date}",
        "actual_start_date": start_date,
        "actual_end_date": end_date,
        "total_events": total_events,
        "signals_generated": total_signals,
        "fetch_summary": fetch_result,
        "significance": sig_report,
        "hypothesis_filters": filters if filters else {},
        "unfilterable_context_factors": unfilterable,
        "unfiltered_totals_side": (
            market_type == "totals" and "side_filter" not in (filters or {})
        ),
        "filter_quality": (
            "fully_filtered" if filters and not unfilterable
            else "partially_filtered" if filters
            else "unfiltered_noisy" if unfilterable
            else "generic"
        ),
        "context_coverage": context_coverage,
    }
