"""ResearchLoop phase implementations, extracted from tools/autonomous.py.

Each ``phase_*`` function here is the *implementation* of the corresponding
``ResearchLoop._phase_*`` method. The methods remain on ResearchLoop as thin
wrappers (so the sequencer table and external callers are untouched) and
delegate here with ``self`` as the single argument.

This module must never import :mod:`tools.autonomous` — that would be a
circular import. Shared state flows one way: constants/helpers defined here
are imported *by* autonomous.py.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from tools import telegram
from tools.backtest import _signal_confidence

logger = logging.getLogger("callisto.autonomous")


# ── Cadence controls (moved verbatim from tools/autonomous.py) ────────────
# MAXIMUM THROUGHPUT (Karpathy loop: rate limit is the only governor)
RESEARCH_CYCLE_INTERVAL = 60        # 1 min between cycles — tight as possible
DATA_COLLECTION_INTERVAL = 300      # 5 min between data pulls — fresher data for live edges
HYPOTHESIS_GEN_INTERVAL = 120       # 2 min between hypothesis generation — Claude drives, smaller batches
BACKTEST_BATCH_SIZE = 5             # 50 was timing out every cycle (5min/hyp from DB locks). 5 fits in 600s.
CLAUDE_ESCALATION_COOLDOWN = 75      # 75s cooldown — prevents burst of 3-5 calls in 30s that was causing 5x/day stalls
SYSTEM_IMPROVEMENT_INTERVAL = 11    # Run system improvement every N cycles (prime — avoids collision with regime/integrity)
REGIME_ANALYSIS_INTERVAL = 7        # Run regime analysis every N cycles — regime changes are slow (coprime with 4,11,13)

# ── Temporal isolation defaults ──
# Hypotheses train on data before the cutoff, backtest on data after.
# This prevents look-ahead bias / circular testing.
DEFAULT_TRAINING_WINDOW_DAYS = 30    # Train on everything before (today - N days)
BACKTEST_GAP_DAYS = 2                # 2 days: enough temporal isolation to prevent leakage, but avoids the 7-day deadlock where start > end when training_period_end is recent

# ── Sport priority for backtest queue ──
# Sports with more historical data get tested first.
# This ensures NBA/NFL hypotheses (abundant data) are validated before
# MLB (season just started, sparse data). Lower number = higher priority.
SPORT_PRIORITY = {
    "basketball_nba": 1,
    "americanfootball_nfl": 2,
    "icehockey_nhl": 3,
    "baseball_mlb": 4,
    "basketball_ncaab": 5,
    "basketball_ncaaw": 6,
    "basketball_wnba": 7,
    "golf_pga": 8,
}

# Domains to research (ordered by data availability)
RESEARCH_SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "basketball_ncaab",
    "basketball_ncaaw",
    "basketball_wnba",
    "icehockey_nhl",
    "baseball_mlb",
    "golf_pga",
]

# Minimum game contexts required before a sport is eligible for hypothesis generation
MIN_GAMES_FOR_HYPOTHESIS = 100

# GATE POLICY bounds for automated threshold modification (_phase_interpret_backtests).
# An automated actor may raise a hypothesis's edge_threshold (tightening the gate)
# but never lower it; refusals are logged to hypothesis notes for human review.
MIN_EDGE_THRESHOLD_FLOOR = 0.005   # never below the creation default (hypothesis.py:488)
MAX_EDGE_THRESHOLD_CEILING = 0.10  # sanity clamp against LLM garbage (e.g. 25.0)



# Module-level regime cache — shared between AutonomousLoop and ResearchLoop.
# ResearchLoop populates it; AutonomousLoop reads it for edge enrichment.
# LRU-capped to prevent unbounded memory growth (~385 MB/hr leak source).
class _LRUCache:
    """Simple LRU dict with max size. Evicts oldest on overflow."""
    def __init__(self, maxsize: int = 5000):
        from collections import OrderedDict
        self._cache: OrderedDict = OrderedDict()
        self.maxsize = maxsize
    def get(self, key, default=None):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return default
    def __setitem__(self, key, value):
        if key in self._cache:
            self._cache.move_to_end(key)
        elif len(self._cache) >= self.maxsize:
            self._cache.popitem(last=False)
        self._cache[key] = value
    def __contains__(self, key):
        return key in self._cache
    def __bool__(self):
        return bool(self._cache)
    def items(self):
        return self._cache.items()
    def values(self):
        return self._cache.values()
    def __len__(self):
        return len(self._cache)

_regime_cache: _LRUCache = _LRUCache(maxsize=500)


# ── Wiki-in-the-loop toggles (feat/wiki-in-the-loop, 2026-04-22) ─────────
# Opt-in via env var so the retrieval path can be cleanly disabled for
# A/B comparison or if the wiki itself is broken. Default on in this branch.
def _wiki_in_loop_enabled() -> bool:
    return os.getenv("CALLISTO_WIKI_IN_LOOP", "1") == "1"


async def _fetch_wiki_priors(
    db,
    query: str,
    *,
    top_k: int = 10,
    domain: Optional[str] = None,
    min_similarity: float = 0.0,
) -> list[dict]:
    """Retrieve top-K relevant wiki articles for ``query``.

    Safe: all failures return ``[]``. Wiki being down cannot break the
    calling flow. Respects ``CALLISTO_WIKI_IN_LOOP`` toggle.
    """
    if not _wiki_in_loop_enabled():
        return []
    try:
        from tools.knowledge_wiki import get_wiki
        wiki = get_wiki()
        return await wiki.search(
            db, query, top_k=top_k, domain=domain,
            min_similarity=min_similarity,
        )
    except Exception as e:
        logger.warning(f"_fetch_wiki_priors failed for '{query[:80]}': {e}")
        return []


def _render_wiki_priors_block(articles: list[dict], max_chars_per: int = 400) -> str:
    """Render wiki articles into a compact "PRIOR KNOWLEDGE" block for LLM prompts.

    Returns empty string if no articles — caller can unconditionally concat.
    """
    if not articles:
        return ""
    lines = ["PRIOR KNOWLEDGE (wiki articles most relevant to this decision):"]
    for a in articles:
        sim = a.get("similarity")
        sim_str = f"(sim={sim:.2f}) " if isinstance(sim, (int, float)) else ""
        summary = (a.get("summary") or a.get("content") or "")[:max_chars_per]
        lines.append(
            f"- [{a.get('topic')}] {sim_str}{a.get('title', '')}: {summary}"
        )
    return "\n".join(lines) + "\n\n"


def get_regime_for_team(sport: str, team_name: str) -> Optional[dict]:
    """Module-level lookup for cached regime analysis.

    Tries exact match first, then partial match for team name flexibility.
    """
    cache_key = f"{sport}:{team_name}"
    result = _regime_cache.get(cache_key)
    if result:
        return result
    # Partial match — team names vary across data sources
    team_lower = team_name.lower()
    for key, val in _regime_cache.items():
        if key.startswith(sport + ":") and team_lower in key.lower():
            return val
    return None













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
