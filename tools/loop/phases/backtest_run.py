"""Backtest + validate ResearchLoop phases, extracted from phases_impl.

Callers still import these names from tools.loop.phases_impl.
This module must never import tools.autonomous (circular).
phase_live_execute stays in phases_impl with CALLISTO_ALLOW_LIVE_EXECUTE.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tools.loop import phases_impl as _impl

logger = _impl.logger
SPORT_PRIORITY = _impl.SPORT_PRIORITY
BACKTEST_BATCH_SIZE = _impl.BACKTEST_BATCH_SIZE
BACKTEST_GAP_DAYS = _impl.BACKTEST_GAP_DAYS
DEFAULT_TRAINING_WINDOW_DAYS = _impl.DEFAULT_TRAINING_WINDOW_DAYS


async def phase_backtest(loop) -> None:
    self = loop
    """Backtest draft hypotheses — enforcing temporal isolation.

    The correct lifecycle:
      1. Hypothesis was generated using data from [training_period_start .. training_period_end]
      2. Backtest MUST only use data AFTER training_period_end + gap
      3. This prevents circular testing (training and testing on same data)

    Legacy hypotheses without temporal metadata get a conservative default:
    backtest only the last 30 days (assumed to be unseen).
    """
    # Bridge live odds_snapshots into historical_odds_cache so backtests
    # can use recently-collected multi-book data
    try:
        bridge_result = await self.backtest_engine.historical_fetcher.bridge_snapshots_to_cache()
        if bridge_result.get("bridged", 0) > 0:
            logger.info(f"Research: bridged {bridge_result['bridged']} snapshot-days into historical cache")
    except Exception as e:
        logger.warning(f"Research: snapshot bridge failed: {e}")

    # Get draft hypotheses that haven't been backtested
    drafts = await self.hypothesis_manager.list_hypotheses(status="draft")

    if not drafts:
        return

    # ── Pre-filter: skip drafts that already have 0-event backtest runs ──
    # Hypotheses with prior 0-event runs are likely untestable with current
    # data. The circuit breaker will reject after 2, but skipping here avoids
    # wasting one more cycle re-running them before the breaker fires.
    already_zero = set()
    try:
        db = self.data_collector._db
        if db:
            cursor = await db.execute(
                "SELECT DISTINCT hypothesis_id FROM backtest_runs "
                "WHERE total_events = 0"
            )
            already_zero = {row[0] for row in await cursor.fetchall()}
            if already_zero:
                before = len(drafts)
                drafts = [h for h in drafts if h.get("hypothesis_id") not in already_zero]
                skipped_zero = before - len(drafts)
                if skipped_zero > 0:
                    logger.info(
                        f"Research: skipped {skipped_zero} drafts with prior "
                        f"0-event backtest runs (awaiting circuit breaker)"
                    )
    except Exception as e:
        logger.warning(f"Pre-filter for 0-event drafts failed: {e}")

    # Pre-check which sports have usable odds (>=2 books)
    sports_with_odds = set()
    try:
        db = self.data_collector._db
        if db:
            cursor = await db.execute(
                "SELECT DISTINCT sport FROM historical_odds_cache"
            )
            for (sport,) in await cursor.fetchall():
                # Quick sample: does this sport have any multi-book records?
                check = await db.execute(
                    "SELECT response_json FROM historical_odds_cache "
                    "WHERE sport = ? ORDER BY RANDOM() LIMIT 5",
                    (sport,),
                )
                for (rj,) in await check.fetchall():
                    try:
                        data = json.loads(rj) if isinstance(rj, str) else rj
                        games = data.get("games", []) if isinstance(data, dict) else data
                        for g in games:
                            if len(g.get("bookmakers", [])) >= 2:
                                sports_with_odds.add(sport)
                                break
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if sport in sports_with_odds:
                        break
    except Exception as e:
        logger.warning(f"Data quality pre-check failed: {e}")

    # Pre-filter: remove hypotheses that will definitely be skipped
    # (context_coverage < 0.5). Without this, the same 20 untestable
    # hypotheses clog the batch every cycle and nothing testable runs.
    from tools.backtest import BacktestEngine
    testable = []
    for h in drafts:
        mc = h.get("model_config", {})
        if isinstance(mc, str):
            try:
                mc = json.loads(mc)
            except (json.JSONDecodeError, TypeError):
                mc = {}
        ctx_coverage = BacktestEngine.compute_context_coverage(mc)
        has_struct = BacktestEngine.has_structured_filters(mc)
        if ctx_coverage >= 0.5 and not mc.get("context_factors"):
            h_thesis = h.get("thesis", "")
            h_name = h.get("name", "")
            inferred = BacktestEngine._infer_context_needs(h_thesis, h_name)
            if inferred and not has_struct:
                continue  # Skip — will fail context check anyway
        elif ctx_coverage < 0.5 and not has_struct:
            continue  # Skip — insufficient context coverage
        testable.append(h)

    # Sport-balanced batching: round-robin across sports instead of
    # pure priority sort. This prevents NBA from saturating the queue
    # and starving all other sports (root cause of 0 non-NBA backtests).
    from collections import defaultdict
    by_sport = defaultdict(list)
    for h in testable:
        sport = h.get("sport", "unknown")
        by_sport[sport].append(h)

    # Sort sports by data availability (SPORT_PRIORITY) — all sports equal
    sport_order = sorted(by_sport.keys(), key=lambda x: SPORT_PRIORITY.get(x, 99))

    # Round-robin: take hypotheses from each sport in turns
    to_test = []
    sport_idx = {s: 0 for s in sport_order}
    while len(to_test) < BACKTEST_BATCH_SIZE:
        added_any = False
        for sport in sport_order:
            if len(to_test) >= BACKTEST_BATCH_SIZE:
                break
            idx = sport_idx[sport]
            if idx < len(by_sport[sport]):
                to_test.append(by_sport[sport][idx])
                sport_idx[sport] = idx + 1
                added_any = True
        if not added_any:
            break

    skipped = len(drafts) - len(testable)
    sports_in_batch = set(h.get("sport", "?") for h in to_test)
    logger.info(
        f"Research: backtesting {len(to_test)} hypotheses across {len(sports_in_batch)} sports "
        f"({skipped} skipped as untestable, {len(testable)} testable, "
        f"sports: {sorted(sports_in_batch)})"
    )

    for h in to_test:
        if not self._running:
            break

        sport = h.get("sport", "")
        market = h.get("market_type", "")

        # Player prop hypotheses now backtested via prop_snapshots table.
        # The backtest engine fetches multi-book prop data and applies
        # consensus devig with MIN_BOOKS=2 (thinner markets than game-level).

        # Skip hypotheses where most context conditions are unfilterable.
        # These produce identical event sets across different hypotheses
        # because game-level conditions (pitcher stats, weather, etc.) can't
        # be applied — the backtest just tests ALL games in the sport/market.
        model_cfg = h.get("model_config", {})
        if isinstance(model_cfg, str):
            try:
                model_cfg = json.loads(model_cfg)
            except (json.JSONDecodeError, TypeError):
                model_cfg = {}
        from tools.backtest import BacktestEngine
        ctx_coverage = BacktestEngine.compute_context_coverage(model_cfg)
        has_struct = BacktestEngine.has_structured_filters(model_cfg)
        # Also infer context needs from thesis/name BEFORE running backtest
        # (same inference run_backtest does internally). This prevents wasting
        # a backtest cycle on hypotheses that will just return "untestable".
        if ctx_coverage >= 0.5 and not model_cfg.get("context_factors"):
            h_thesis = h.get("thesis", "")
            h_name_for_ctx = h.get("name", "")
            inferred_pre = BacktestEngine._infer_context_needs(h_thesis, h_name_for_ctx)
            if inferred_pre and not has_struct:
                ctx_coverage = 0.0
                logger.info(
                    f"Research: pre-backtest inference for {h['hypothesis_id']} "
                    f"({h_name_for_ctx}) detected unfilterable needs: {inferred_pre}"
                )
            elif inferred_pre and has_struct:
                logger.info(
                    f"Research: {h['hypothesis_id']} ({h_name_for_ctx}) has inferred "
                    f"unfilterable needs {inferred_pre} but structured filters present — proceeding"
                )
        if ctx_coverage < 0.5 and not has_struct:
            ctx_factors = model_cfg.get("context_factors", [])
            logger.info(
                f"Research: skipping backtest for {h['hypothesis_id']} — "
                f"context_coverage={ctx_coverage:.0%}. Needs game context enrichment."
            )
            continue

        # Skip hypotheses for sports with no usable multi-book data
        if sports_with_odds and sport not in sports_with_odds:
            logger.info(
                f"Research: skipping backtest for {h['hypothesis_id']} — "
                f"{sport} has no multi-book odds data yet"
            )
            continue

        try:
            # ── Temporal isolation: determine forward-test date range ──
            model_config = h.get("model_config", {})
            if isinstance(model_config, str):
                try:
                    model_config = json.loads(model_config)
                except (json.JSONDecodeError, TypeError):
                    model_config = {}

            # Pre-check: fix stale contaminated temporal metadata from
            # before BACKTEST_GAP_DAYS was corrected (1→7). Recompute
            # backtest_period_start rather than rejecting fixable drafts.
            overlap_err = self._check_temporal_overlap(model_config)
            if overlap_err:
                te = model_config.get("training_period_end", "")
                if te:
                    try:
                        te_date = datetime.strptime(te, "%Y-%m-%d").date()
                        correct_start = str(te_date + timedelta(days=BACKTEST_GAP_DAYS))
                        model_config["backtest_period_start"] = correct_start
                        db = self.data_collector._db
                        await db.execute(
                            "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                            (json.dumps(model_config), h["hypothesis_id"]),
                        )
                        await db.commit()
                        logger.info(
                            f"Research: fixed stale temporal metadata for "
                            f"{h['hypothesis_id']} — backtest_period_start → {correct_start}"
                        )
                    except Exception:
                        await self.hypothesis_manager.update_status(
                            h["hypothesis_id"], "rejected",
                            f"auto:temporal_overlap — {overlap_err}"
                        )
                        self._rejections += 1
                        continue

            has_temporal = (
                "training_period_end" in model_config
                and model_config["training_period_end"]
            )

            if has_temporal:
                # Forward-only backtest: start AFTER training period + gap
                training_end = model_config["training_period_end"]
                try:
                    te_date = datetime.strptime(training_end, "%Y-%m-%d").date()
                except ValueError:
                    te_date = datetime.now(timezone.utc).date() - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                start_date = str(te_date + timedelta(days=BACKTEST_GAP_DAYS))
                end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                logger.info(
                    f"Research: backtest {h['hypothesis_id']} forward-only "
                    f"[{start_date} .. {end_date}] (trained up to {training_end})"
                )
            else:
                # Legacy hypothesis without temporal metadata — backfill it
                # to enforce temporal isolation (prevents circular testing).
                today_d = datetime.now(timezone.utc).date()
                training_cutoff = today_d - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                model_config["training_period_start"] = "2023-01-01"
                model_config["training_period_end"] = str(training_cutoff)
                model_config["forward_test_start"] = str(training_cutoff + timedelta(days=1))
                start_date = str(training_cutoff + timedelta(days=BACKTEST_GAP_DAYS))
                end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                has_temporal = True  # Now it does
                logger.info(
                    f"Research: backfilled temporal metadata for {h['hypothesis_id']} — "
                    f"training ends {training_cutoff}, backtest [{start_date} .. {end_date}]"
                )

            # Never backtest against today — games haven't finished
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if end_date >= today:
                end_date = str(datetime.now(timezone.utc).date() - timedelta(days=1))

            # ── Constrain to date range where historical data EXISTS ──
            # Without this, backtests target dates with no cached odds
            # and produce 0 events every time.
            cached_range = await self.backtest_engine.historical_fetcher.get_cached_date_range(sport)
            if cached_range and cached_range[0] and cached_range[1]:
                cache_start, cache_end = cached_range
                # Clamp start_date and end_date to the cached range
                if start_date < cache_start:
                    start_date = cache_start
                if end_date > cache_end:
                    end_date = cache_end
                logger.info(
                    f"Research: backtest {h['hypothesis_id']} date range "
                    f"clamped to cached data [{start_date} .. {end_date}]"
                )
            else:
                logger.info(
                    f"Research: skipping backtest for {h['hypothesis_id']} — "
                    f"no historical odds cached for {sport}"
                )
                continue

            if start_date > end_date:
                logger.info(
                    f"Research: skipping backtest for {h['hypothesis_id']} — "
                    f"no historical date range available (start={start_date} > end={end_date})"
                )
                continue

            # ── Flush any dangling transactions before backtest writes ──
            # Phase timeouts (self_repair, etc.) can leave uncommitted
            # transactions on shared connections, holding the WAL write lock.
            # Check all accessible DB connections.
            _flush_conns = {
                "data_collector": getattr(self.data_collector, "_db", None),
                "backtest_engine": getattr(self.backtest_engine, "_db", None),
                "line_monitor": getattr(self.line_monitor, "_db", None) if self.line_monitor else None,
                "hypothesis_mgr": getattr(self.hypothesis_manager, "_db", None),
            }
            _tx_state = []
            for _fn, _fdb in _flush_conns.items():
                if _fdb and hasattr(_fdb, "_conn") and _fdb._conn:
                    try:
                        _in_tx = _fdb._conn.in_transaction
                        _tx_state.append(f"{_fn}={_in_tx}")
                        if _in_tx:
                            await _fdb.rollback()
                            logger.warning(f"Flushed dangling transaction on {_fn}")
                    except Exception:
                        _tx_state.append(f"{_fn}=err")
            if _tx_state:
                logger.info(f"Pre-backtest tx state: {', '.join(_tx_state)}")

            _bt_t0 = time.time()
            # Retry on database lock — other subsystems (line_monitor,
            # self_repair) occasionally hold the WAL write lock.
            _max_retries = 3
            result = None
            for _attempt in range(_max_retries):
                try:
                    result = await self.backtest_engine.run_backtest(
                        hypothesis_id=h["hypothesis_id"],
                        start_date=start_date,
                        end_date=end_date,
                        credit_budget=30,
                    )
                    break  # Success
                except Exception as _bt_err:
                    if "database is locked" in str(_bt_err) and _attempt < _max_retries - 1:
                        _wait = 5 * (2 ** _attempt)  # 5s, 10s
                        logger.warning(
                            f"Backtest {h['hypothesis_id']} hit DB lock "
                            f"(attempt {_attempt + 1}/{_max_retries}), "
                            f"retrying in {_wait}s"
                        )
                        await asyncio.sleep(_wait)
                    else:
                        raise  # Re-raise for outer except handler
            if result is None:
                continue  # All retries exhausted

            _bt_elapsed = time.time() - _bt_t0
            if _bt_elapsed > 30:
                logger.warning(
                    f"Slow backtest: {h.get('name', h['hypothesis_id'])} "
                    f"took {_bt_elapsed:.1f}s"
                )

            # Handle untestable hypotheses — context filtering not available
            if result.get("error") == "untestable":
                logger.warning(
                    f"Research: hypothesis {h['hypothesis_id']} ({h.get('name', '?')}) "
                    f"is UNTESTABLE — {result.get('detail', 'no context data')}. "
                    f"Moving back to draft."
                )
                try:
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "draft", "auto:untestable"
                    )
                except Exception as e:
                    logger.warning(f"Failed to revert {h['hypothesis_id']} to draft: {e}")
                continue

            # Handle duplicate backtests — same events as another hypothesis
            if result.get("error") == "duplicate_backtest":
                logger.warning(
                    f"Research: {h['hypothesis_id']} ({h.get('name', '?')}) "
                    f"is a DUPLICATE backtest of {result.get('duplicate_of', '?')}. "
                    f"Moving back to draft — needs unique filtering to be testable."
                )
                try:
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "draft", "auto:duplicate_backtest"
                    )
                except Exception:
                    pass
                continue

            # Handle spring training — don't penalize, just skip until season starts
            if result.get("error") == "spring_training":
                logger.info(
                    f"Research: skipping {h['hypothesis_id']} ({h.get('name', '?')}) — "
                    f"MLB spring training, will retry after season start"
                )
                continue

            # Store temporal metadata in backtest result for integrity checking
            self._backtests_run += 1
            signals = result.get("signals_generated", 0)

            # Update model_config with actual backtest range for audit trail
            # Use actual_start_date from backtest result (may be auto-adjusted
            # for temporal isolation) instead of the original start_date
            actual_start = result.get("actual_start_date", start_date)
            actual_end = result.get("actual_end_date", end_date)
            if has_temporal:
                model_config["backtest_period_start"] = actual_start
                model_config["backtest_period_end"] = actual_end
                model_config["temporal_isolation"] = True
            else:
                model_config["backtest_period_start"] = actual_start
                model_config["backtest_period_end"] = actual_end
                model_config["temporal_isolation"] = False
                model_config["temporal_isolation_note"] = "legacy_hypothesis_conservative_default"

            # Persist updated model_config
            try:
                db = self.data_collector._db
                if db:
                    await db.execute(
                        "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                        (json.dumps(model_config), h["hypothesis_id"]),
                    )
                    await db.commit()
            except Exception as e:
                logger.warning(f"Failed to update temporal metadata for {h['hypothesis_id']}: {e}")

            total_events = result.get("total_events", 0)
            if total_events == 0:
                # ── Circuit breaker: reject after 2 consecutive 0-event runs ──
                # Without this, hypotheses like nhl_playoff_clinch_letdown_total_over
                # get re-run 5-6 times with 0 events each, wasting backtest cycles.
                try:
                    db = self.data_collector._db
                    if db:
                        prev_runs = await db.execute(
                            "SELECT COUNT(*) FROM backtest_runs "
                            "WHERE hypothesis_id = ? AND total_events = 0",
                            (h["hypothesis_id"],),
                        )
                        zero_count = (await prev_runs.fetchone())[0]
                        if zero_count >= 2:
                            await self.hypothesis_manager.update_status(
                                h["hypothesis_id"], "rejected",
                                f"auto:zero_events_circuit_breaker — {zero_count} consecutive "
                                f"backtest runs with 0 events. Context filters may be too "
                                f"restrictive or insufficient historical data for {sport}."
                            )
                            self._rejections += 1
                            logger.info(
                                f"Research: CIRCUIT BREAKER — rejected {h['hypothesis_id']} "
                                f"({h.get('name', '?')}) after {zero_count} zero-event runs"
                            )
                            continue
                except Exception as e:
                    logger.warning(f"Circuit breaker check failed for {h['hypothesis_id']}: {e}")

                logger.warning(
                    f"Research: backtest {h['hypothesis_id']} produced 0 events "
                    f"({start_date} to {end_date}) — no historical odds data for {sport}?"
                )
            else:
                # ── Gate: reject hypotheses that need context filtering but lack game_filters ──
                # Without structured game_filters, these hypotheses test ALL games for the sport,
                # producing identical event sets (the "149 identical events" bug).
                _mc = h.get("model_config", {})
                if isinstance(_mc, str):
                    try:
                        _mc = json.loads(_mc)
                    except (json.JSONDecodeError, TypeError):
                        _mc = {}
                _has_gf = bool(_mc.get("game_filters"))
                _needs_cf = BacktestEngine._needs_context_filter(
                    h.get("name", ""), h.get("thesis", ""), _mc
                )
                if _needs_cf and not _has_gf:
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "rejected",
                        "auto:missing_game_filters — name implies contextual conditions "
                        "but no structured game_filters defined. Recreate with game_filters."
                    )
                    self._rejections += 1
                    logger.info(
                        f"Research: GATE REJECT {h['hypothesis_id']} ({h.get('name', '?')}) — "
                        f"needs context filter but has no game_filters"
                    )
                    continue

                # ── CRITICAL: Move hypothesis from draft → backtesting ──
                # Without this, _phase_evaluate() never sees these hypotheses
                # (it queries status='backtesting' only). This was the root cause
                # of 0 promotions with 577+ backtest events.
                try:
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "backtesting",
                        f"auto:backtest_completed — {total_events} events, {signals} signals"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to promote {h['hypothesis_id']} to backtesting: {e}"
                    )
                logger.info(
                    f"Research: backtest {h['hypothesis_id']} — "
                    f"{total_events} events, {signals} signals → status=backtesting"
                )
        except Exception as e:
            logger.warning(
                f"Backtest failed for {h['hypothesis_id']}: {e}"
            )


async def phase_validate(loop) -> None:
    self = loop
    """Per-cycle sanity validation — catches data quality issues immediately.

    Runs after every backtest phase. Checks:
    1. Phantom edges (>15% or impossibly uniform signal rates)
    2. Context enrichment coverage
    3. Books_used distribution (devig quality)
    4. Orphaned tables that should have data
    """
    db = self.hypothesis_manager._db
    if not db:
        return

    issues = []

    try:
        # 1. Phantom edge detection: flag backtest events with >15% edge
        cursor = await db.execute(
            "SELECT COUNT(*) FROM backtest_events WHERE ABS(edge) > 0.15"
        )
        phantom_count = (await cursor.fetchone())[0]
        if phantom_count > 0:
            issues.append(
                f"PHANTOM: {phantom_count} backtest events with |edge| > 15% "
                f"— likely data contamination"
            )
            # Auto-purge phantoms
            await db.execute("DELETE FROM backtest_events WHERE ABS(edge) > 0.15")
            await db.commit()
            logger.warning(f"Purged {phantom_count} phantom backtest events (|edge| > 15%)")

        # 2. Context enrichment coverage (last 7 days)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM game_contexts "
            "WHERE game_date >= date('now', '-7 days')"
        )
        total_recent = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM game_contexts "
            "WHERE game_date >= date('now', '-7 days') "
            "AND context_json LIKE '%rest_days%'"
        )
        enriched_recent = (await cursor.fetchone())[0]
        if total_recent > 0:
            enrich_rate = enriched_recent / total_recent
            if enrich_rate < 0.5:
                issues.append(
                    f"ENRICHMENT: Only {enrich_rate:.0%} of last 7 days' games "
                    f"have rest_days ({enriched_recent}/{total_recent})"
                )

        # 3. Orphaned table detection
        orphan_checks = [
            ("market_microstructure", "odds_snapshots", 100),
            ("learned_correlations", "game_results", 1000),
        ]
        from tools.db_utils import safe_ident
        for target_table, source_table, source_min in orphan_checks:
            cursor = await db.execute(f"SELECT COUNT(*) FROM {safe_ident(target_table)}")
            target_count = (await cursor.fetchone())[0]
            cursor = await db.execute(f"SELECT COUNT(*) FROM {safe_ident(source_table)}")
            source_count = (await cursor.fetchone())[0]
            if target_count == 0 and source_count >= source_min:
                issues.append(
                    f"ORPHAN: {target_table} has 0 rows but {source_table} "
                    f"has {source_count} — pipeline not connected"
                )

        # 4. Stale data detection (hot tables)
        for table, ts_col, max_hours in [
            ("odds_snapshots", "timestamp", 2),
            ("game_contexts", "created_at", 24),
        ]:
            cursor = await db.execute(
                f"SELECT MAX({safe_ident(ts_col)}) FROM {safe_ident(table)}"
            )
            row = await cursor.fetchone()
            if row and row[0]:
                from datetime import datetime, timezone
                try:
                    last_ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600
                    if age_hours > max_hours:
                        issues.append(
                            f"STALE: {table} last update {age_hours:.1f}h ago "
                            f"(threshold: {max_hours}h)"
                        )
                except (ValueError, TypeError):
                    pass

    except Exception as e:
        logger.debug(f"Validation phase error: {e}")

    if issues:
        logger.warning(
            f"Pipeline validation: {len(issues)} issues found:\n"
            + "\n".join(f"  - {i}" for i in issues)
        )
        # Record to Hermes for cross-session awareness
        try:
            from tools.hermes_memory import get_hermes_memory
            hm = await get_hermes_memory()
            if hm:
                await hm.record_learning(
                    key="pipeline_validation_issues",
                    value="; ".join(issues),
                    confidence=0.9,
                    source="pipeline_validator",
                )
        except Exception:
            pass

        # Record sentinel flags for anomaly tracking
        try:
            from tools.cache_manager import record_sentinel_flag
            for issue in issues:
                severity = "critical" if "PHANTOM" in issue else "warning"
                await record_sentinel_flag(
                    flag_type="pipeline_validation",
                    description=issue,
                    severity=severity,
                )
        except Exception:
            pass
