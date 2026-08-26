"""ResearchLoop phase implementations, extracted from tools/autonomous.py.

Each ``phase_*`` function here is the *implementation* of the corresponding
``ResearchLoop._phase_*`` method. The methods remain on ResearchLoop as thin
wrappers (so the sequencer table and external callers are untouched) and
delegate here with ``self`` as the single argument.

This module must never import :mod:`tools.autonomous` — that would be a
circular import. Shared state flows one way: constants/helpers defined in
:mod:`tools.loop.phases.shared` are re-exported here, then imported *by*
autonomous.py.

``phase_live_execute`` stays defined in this facade so
``CALLISTO_ALLOW_LIVE_EXECUTE`` remains the first executable after
``self = loop`` / docstring / ``import os as _os``.
"""
from tools.loop.phases.shared import (  # noqa: F401 — re-exported for _impl binders
    BACKTEST_BATCH_SIZE,
    BACKTEST_GAP_DAYS,
    CLAUDE_ESCALATION_COOLDOWN,
    DATA_COLLECTION_INTERVAL,
    DEFAULT_TRAINING_WINDOW_DAYS,
    HYPOTHESIS_GEN_INTERVAL,
    MAX_EDGE_THRESHOLD_CEILING,
    MIN_EDGE_THRESHOLD_FLOOR,
    MIN_GAMES_FOR_HYPOTHESIS,
    REGIME_ANALYSIS_INTERVAL,
    RESEARCH_CYCLE_INTERVAL,
    RESEARCH_SPORTS,
    SPORT_PRIORITY,
    SYSTEM_IMPROVEMENT_INTERVAL,
    _LRUCache,
    _fetch_wiki_priors,
    _regime_cache,
    _render_wiki_priors_block,
    _wiki_in_loop_enabled,
    get_regime_for_team,
    logger,
)


async def phase_live_execute(loop) -> None:
    self = loop
    """Execute bets on live (proven) hypotheses.

    SAFETY GATE: this phase is OFF by default. It only runs when the
    operator explicitly arms it via the environment variable
    ``CALLISTO_ALLOW_LIVE_EXECUTE=1`` — that env var is the ONLY
    arming switch for this phase.

    Combined flow (feat/portfolio-kelly-live-loop + feat/order-management-telegram):
      1. Run drawdown kill-switch check BEFORE any execution.
      2. Collect ALL pending signals across ALL LIVE hyps into a batch.
      3. Build correlation matrix from backtest_events history.
      4. Call ``compute_portfolio_stakes`` ONCE per cycle with per-game
         and per-sport caps.
      5. For each sized bet:
         - If ``CALLISTO_USE_ORDER_MANAGER=1`` (default): submit via
           :mod:`tools.order_manager` for Telegram approval, passing the
           portfolio-sized stake.
         - Else: execute directly via the legacy Playwright executor with
           the pre-computed ``stake_override``.
    """
    import os as _os
    if _os.getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1":
        logger.info("live_execute skipped (CALLISTO_ALLOW_LIVE_EXECUTE!=1)")
        return

    use_order_manager = _os.getenv("CALLISTO_USE_ORDER_MANAGER", "1") == "1"

    try:
        from tools.bet_executor import BetExecutor  # noqa: F401
    except ImportError:
        return

    order_manager = None
    if use_order_manager:
        try:
            from tools.order_manager import get_manager as _get_om
            order_manager = await _get_om()
            if not order_manager.is_enabled:
                # order_manager configured but disabled — fall back to
                # direct executor path below.
                order_manager = None
        except Exception as e:
            logger.warning(f"order_manager unavailable, falling back: {e}")
            order_manager = None

    # Executor is always required: we need it for drawdown check,
    # bankroll read, and compute_portfolio_stakes even when the final
    # submission hop is the Telegram-approved order_manager.
    executor = getattr(self, "_bet_executor", None)
    if not executor or not executor.is_enabled:
        return

    # Drawdown kill-switch: evaluate BEFORE we consider any new bets.
    try:
        dd = await executor.check_drawdown_and_kill()
        if dd.get("triggered"):
            logger.error(
                "Research: drawdown kill-switch fired; aborting live execution "
                f"(drawdown={dd.get('drawdown_pct'):.1%}, "
                f"paused={len(dd.get('paused_hypotheses', []))})"
            )
            return
    except Exception as e:
        logger.warning(f"Drawdown check failed: {e}")

    live = await self.hypothesis_manager.list_hypotheses(status="live")
    if not live:
        return

    logger.info(f"Research: scanning {len(live)} live hypotheses for bet signals")

    # Cache live odds per sport
    odds_cache: dict[str, dict] = {}

    # ---- Phase 1: collect signals from all LIVE hyps into a single batch ----
    batch: list[dict] = []
    signal_by_index: list[tuple[dict, dict]] = []  # (hyp, signal) for each batch row

    for h in live:
        if not self._running:
            break
        try:
            sport = h["sport"]
            market = h.get("market_type", "")

            if sport not in odds_cache:
                if market.startswith("player_"):
                    from tools.odds_api_io import get_odds
                    odds_data = await get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
                else:
                    from tools.dk_scraper import scrape_dk_odds
                    odds_data = await scrape_dk_odds(sport)
                    if odds_data.get("error") or not odds_data.get("games"):
                        from tools.odds_api_io import get_odds
                        odds_data = await get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
                if not odds_data.get("error"):
                    odds_cache[sport] = odds_data

            odds_data = odds_cache.get(sport)
            if not odds_data:
                continue

            signals = await self.backtest_engine.generate_paper_trade_signal(
                hypothesis_id=h["hypothesis_id"],
                live_odds=odds_data,
            )
            if not signals:
                continue

            for signal in signals:
                batch.append({
                    "edge": signal.get("edge", 0.0),
                    "odds": signal.get("book_odds_american", 0),
                    "confidence": signal.get("confidence_score", 0.6),
                    "event_id": signal.get("event_id", ""),
                    "sport": sport,
                    "market_type": signal.get("market", market),
                    "hypothesis_id": h["hypothesis_id"],
                    "description": (
                        f"{h['hypothesis_id'][:8]}:{signal.get('team', '')}"
                        f" {signal.get('market', market)}"
                    ),
                })
                signal_by_index.append((h, signal))

        except Exception as e:
            logger.warning(f"Live signal collection failed for {h['hypothesis_id']}: {e}")

    if not batch:
        return

    logger.info(
        f"Research: collected {len(batch)} signals across {len(set(b['hypothesis_id'] for b in batch))} "
        f"hyps on {len(set(b['event_id'] for b in batch if b['event_id']))} events"
    )

    # ---- Phase 2: correlation matrix + signals_n dampener ----
    live_ids = [h["hypothesis_id"] for h in live]
    try:
        corr_matrix = await self._build_correlation_matrix(live_ids)
    except Exception as e:
        logger.warning(f"Correlation matrix build failed: {e}")
        corr_matrix = {}

    try:
        sig_counts = await self._hyp_signals_n_map(live_ids)
    except Exception:
        sig_counts = {}
    for b in batch:
        b["signals_n"] = sig_counts.get(b["hypothesis_id"], 0)

    # ---- Phase 3: portfolio sizing, ONCE, with caps applied inside ----
    try:
        bankroll = await executor.get_bankroll()
    except Exception as e:
        logger.warning(f"Bankroll read failed, aborting live execution: {e}")
        return

    if bankroll <= 0:
        logger.warning("Bankroll is zero; skipping live execution")
        return

    sized = executor.compute_portfolio_stakes(
        bets=batch, bankroll=bankroll, correlation_matrix=corr_matrix,
    )

    # ---- Phase 4: submit each sized bet ----
    # If the order_manager is enabled (default), route the portfolio-
    # sized stake through Telegram-approved submit_order(). Otherwise
    # execute directly via the Playwright bet_executor with
    # stake_override=stake. In BOTH paths, the stake has already been
    # capped by compute_portfolio_stakes (per-game, per-sport, Kelly,
    # drawdown-aware, regime-multiplier-scaled).
    from tools.bet_executor import _regime_safe as _bet_regime_safe  # noqa: WPS433
    for i, sized_row in enumerate(sized):
        if not self._running:
            break
        stake = float(sized_row.get("stake", 0.0) or 0.0)
        if stake <= 0:
            continue
        h, signal = signal_by_index[i]

        # ── Regime-safe trading gate (feat/regime-aware-sizing) ──
        # Skip the bet if market_regime says this sport is in a known-
        # noisy phase (preseason / offseason / final days of regular
        # season). Gated by CALLISTO_REGIME_SAFETY so operators can
        # disable if the calendar is mis-configured.
        safe, phase = _bet_regime_safe(h.get("sport", ""))
        if not safe:
            logger.info(
                "LIVE bet SKIPPED: hyp=%s sport=%s reason=regime_unsafe_phase=%s",
                h.get("hypothesis_id"), h.get("sport"), phase or "unknown",
            )
            continue
        try:
            if order_manager is not None:
                stake_units = stake / bankroll if bankroll > 0 else 0.0
                try:
                    order_id = await order_manager.submit_order(
                        hypothesis_id=h["hypothesis_id"],
                        signal=signal,
                        stake_units=stake_units,
                        stake_dollars=stake,
                        book=signal.get("book", "draftkings"),
                        odds_snapshot_id=signal.get("odds_snapshot_id"),
                        edge=signal.get("edge"),
                        fair_prob=signal.get("model_fair_prob"),
                        clv_prior=signal.get("clv_prior"),
                    )
                    logger.info(
                        f"ORDER SUBMITTED for approval: order_id={order_id} "
                        f"hyp={h['hypothesis_id']} {signal.get('side')} "
                        f"${stake:.2f} @ {signal.get('book_odds_american')} "
                        f"(portfolio-sized, n={sized_row.get('signals_n', 0)})"
                    )
                except Exception as e:
                    logger.warning(f"submit_order failed: {e}")
            else:
                result = await executor.execute_bet(
                    sport=h["sport"],
                    team=signal.get("team", ""),
                    market=signal.get("market", h.get("market_type", "")),
                    side=signal.get("side", ""),
                    odds=signal.get("book_odds_american", 0),
                    fair_prob=signal.get("model_fair_prob", 0.5),
                    edge=signal.get("edge", 0),
                    hypothesis_id=h["hypothesis_id"],
                    event_id=signal.get("event_id", ""),
                    game_description=signal.get("game_description", ""),
                    stake_override=stake,
                )
                if result.get("success"):
                    logger.info(
                        f"LIVE BET PLACED: {signal.get('team')} "
                        f"${result.get('stake', 0):.2f} @ {signal.get('book_odds_american')} "
                        f"(portfolio-sized, n={sized_row.get('signals_n', 0)})"
                    )
                else:
                    logger.warning(f"Live bet failed: {result.get('reason', 'unknown')}")
        except Exception as e:
            logger.warning(f"Live execution failed for {h['hypothesis_id']}: {e}")



# ── Post-live phases live in tools.loop.phases.post_live ──────────────────
# Import at the bottom so helpers above are bound before post_live loads.
from tools.loop.phases.post_live import (  # noqa: E402
    phase_claude_deep_work,
    phase_granger_analysis,
    phase_integrity_check,
    phase_knowledge_compile,
    phase_knowledge_lint,
    phase_narrative_edges,
    phase_regime_analysis,
    phase_review_live,
    phase_system_improvement,
    phase_system_watchdog,
)
from tools.loop.phases.pre_live import (  # noqa: E402
    phase_interpret_backtests,
    phase_paper_trade,
)
from tools.loop.phases.hypgen import (  # noqa: E402
    phase_generate_hypotheses,
    phase_injury_prop_hypotheses,
)
from tools.loop.phases.collect_eval import (  # noqa: E402
    phase_collect_data,
    phase_embed_data,
    phase_evaluate,
)
from tools.loop.phases.backtest_run import (  # noqa: E402
    phase_backtest,
    phase_validate,
)
from tools.loop.phases.repair import (  # noqa: E402
    phase_self_repair,
    phase_self_diagnose,
    phase_refresh_signals,
)
