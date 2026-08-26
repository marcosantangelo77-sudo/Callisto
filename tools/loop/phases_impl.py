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



async def phase_self_repair(loop) -> None:
    self = loop
    """
    Self-repair phase — detect issues, fix them autonomously, verify,
    and record to Hermes. Runs every 5 cycles to avoid overhead.
    Also runs cache rotation to maintain operational hygiene.
    """
    if self._cycles % 5 != 1:
        return  # Only run every 5 cycles (cycle 1, 6, 11, ...)

    # Cache rotation — rebuild hot cache, archive stale data
    try:
        from tools.cache_manager import rotate_caches
        await rotate_caches()
    except Exception as e:
        logger.debug(f"Cache rotation failed (non-fatal): {e}")

    try:
        from tools.self_repair import get_repair_engine
        engine = get_repair_engine()
        result = await engine.run_repair_cycle()

        if result["fixed"] > 0:
            logger.info(
                f"Self-repair: fixed {result['fixed']}/{result['issues_found']} issues"
            )

        # Record phase success for pipeline integrity tracking
        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("self_repair", True)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Self-repair phase failed: {e}", exc_info=True)
        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("self_repair", False)
        except Exception:
            pass


async def phase_self_diagnose(loop) -> None:
    self = loop
    """
    Self-diagnostic phase — detects broken pipelines BEFORE wasting cycles.

    Checks data quality, pipeline throughput, and data freshness.
    Escalates critical issues to Claude Code exactly once per issue.
    """
    from datetime import datetime, timedelta, timezone

    db = self.data_collector._db
    if db is None:
        logger.warning("DIAG: data_collector DB not initialized, skipping diagnostics")
        return

    issues: list[dict] = []  # {"key": str, "severity": str, "message": str}

    # ── 1. Data quality: avg books per record per sport ──
    try:
        cursor = await db.execute(
            "SELECT sport, COUNT(*) as cnt "
            "FROM historical_odds_cache GROUP BY sport"
        )
        rows = await cursor.fetchall()
        for row in rows:
            sport, cnt = row[0], row[1]
            # Sample up to 50 records to estimate avg books
            sample_cursor = await db.execute(
                "SELECT response_json FROM historical_odds_cache "
                "WHERE sport = ? ORDER BY RANDOM() LIMIT 50",
                (sport,),
            )
            samples = await sample_cursor.fetchall()
            total_books = 0
            parsed = 0
            scores_only = 0
            usable = 0
            for (rj,) in samples:
                try:
                    data = json.loads(rj) if isinstance(rj, str) else rj
                    # Cached format: {"games": [...], "sport": "...", ...}
                    # Each game has a "bookmakers" list
                    games = []
                    if isinstance(data, dict) and "games" in data:
                        games = data["games"]
                    elif isinstance(data, list):
                        games = data
                    elif isinstance(data, dict) and "bookmakers" in data:
                        games = [data]

                    record_books = 0
                    for game in games:
                        bm_count = len(game.get("bookmakers", []))
                        total_books += bm_count
                        record_books = max(record_books, bm_count)
                        parsed += 1
                    if record_books == 0:
                        scores_only += 1
                    elif record_books >= 2:
                        usable += 1
                except (json.JSONDecodeError, TypeError):
                    continue
            avg_books = total_books / max(parsed, 1)
            if scores_only > 0 and usable == 0:
                issue_key = f"scores_only_{sport}"
                msg = (
                    f"DIAG: {sport} has {cnt} cached records but ALL are "
                    f"scores-only (0 bookmakers) — no odds data for backtesting"
                )
                logger.warning(msg)
                issues.append({"key": issue_key, "severity": "CRITICAL", "message": msg})
            elif avg_books < 2:
                issue_key = f"low_books_{sport}"
                msg = (
                    f"DIAG: {sport} has avg {avg_books:.1f} books/game "
                    f"({cnt} records, {usable}/{len(samples)} usable) — "
                    f"backtests against <2 books are unreliable"
                )
                logger.warning(msg)
                issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
            else:
                logger.info(
                    f"DIAG: {sport} data quality OK — avg {avg_books:.1f} books/game "
                    f"({cnt} records, {usable}/{len(samples)} usable)"
                )
    except Exception as e:
        logger.warning(f"DIAG: data quality check failed: {e}")

    # ── 1b. Check game_results overlap with odds data ──
    try:
        cursor = await db.execute(
            "SELECT h.sport, COUNT(DISTINCT h.snapshot_date) as odds_dates, "
            "COUNT(DISTINCT g.game_date) as result_dates "
            "FROM historical_odds_cache h "
            "LEFT JOIN game_results g ON h.sport = g.sport "
            "AND h.snapshot_date = g.game_date "
            "GROUP BY h.sport"
        )
        for row in await cursor.fetchall():
            sport, odds_dates, result_dates = row[0], row[1], row[2]
            if odds_dates > 0 and result_dates == 0:
                issue_key = f"no_results_overlap_{sport}"
                msg = (
                    f"DIAG: {sport} has {odds_dates} odds dates but 0 matching "
                    f"game_results dates — backtest resolution will fail"
                )
                logger.warning(msg)
                issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
    except Exception as e:
        logger.warning(f"DIAG: date overlap check failed: {e}")

    # ── 2. Pipeline throughput ──
    try:
        if self._hypotheses_generated > 0 and self._backtests_run > 0:
            # Check total signals across all backtest events
            cursor = await db.execute(
                "SELECT COUNT(DISTINCT event_id) as total, "
                "COUNT(DISTINCT CASE WHEN signal_generated = 1 THEN event_id END) as signals "
                "FROM backtest_events"
            )
            row = await cursor.fetchone()
            if row:
                total_events, total_signals = row[0] or 0, row[1] or 0
                if total_events > 0:
                    signal_rate = total_signals / total_events
                    if signal_rate < 0.01:
                        issue_key = "low_signal_rate"
                        msg = (
                            f"DIAG: signal rate {signal_rate:.2%} "
                            f"({total_signals}/{total_events} events) — "
                            f"<1% signal generation indicates broken hypothesis logic"
                        )
                        logger.warning(msg)
                        issues.append({"key": issue_key, "severity": "WARNING", "message": msg})

        if self._backtests_run >= 100 and self._promotions == 0:
            issue_key = "zero_promotions"
            msg = (
                f"DIAG: 0 promotions after {self._backtests_run} backtests — "
                f"promotion gates may be too strict or data insufficient"
            )
            logger.warning(msg)
            issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
    except Exception as e:
        logger.warning(f"DIAG: throughput check failed: {e}")

    # ── 3. Data freshness ──
    try:
        now = datetime.now(timezone.utc)

        # Latest game_context
        cursor = await db.execute(
            "SELECT MAX(game_date) FROM game_contexts WHERE sport != 'meta_research'"
        )
        row = await cursor.fetchone()
        if row and row[0]:
            try:
                latest_ctx = datetime.strptime(str(row[0]), "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                age_days = (now - latest_ctx).days
                if age_days > 2:
                    issue_key = "stale_game_contexts"
                    msg = (
                        f"DIAG: latest game_context is {age_days} days old "
                        f"({row[0]}) — data collection may be broken"
                    )
                    logger.warning(msg)
                    issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
            except ValueError:
                pass
        else:
            issue_key = "no_game_contexts"
            msg = "DIAG: no game_contexts found at all — data collection has never succeeded"
            logger.warning(msg)
            issues.append({"key": issue_key, "severity": "CRITICAL", "message": msg})

        # Latest odds_snapshot (from line_monitor's table)
        try:
            cursor = await db.execute(
                "SELECT MAX(timestamp) FROM odds_snapshots"
            )
            row = await cursor.fetchone()
            if row and row[0]:
                try:
                    latest_snap = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
                    if latest_snap.tzinfo is None:
                        latest_snap = latest_snap.replace(tzinfo=timezone.utc)
                    age_hours = (now - latest_snap).total_seconds() / 3600
                    if age_hours > 1:
                        issue_key = "stale_odds_snapshots"
                        msg = (
                            f"DIAG: latest odds_snapshot is {age_hours:.1f}h old — "
                            f"snapshot collection may be failing"
                        )
                        logger.warning(msg)
                        issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            # odds_snapshots table may be in a different DB (line_monitor's)
            logger.info(f"DIAG: odds_snapshots freshness check skipped: {e}")

    except Exception as e:
        logger.warning(f"DIAG: freshness check failed: {e}")

    # ── 4. Escalate critical issues to Claude Code (once per issue) ──
    new_critical = [
        i for i in issues
        if i["severity"] == "CRITICAL" and i["key"] not in self._diagnostic_issues
    ]
    if new_critical:
        from inference import escalate_with_ladder

        now = time.time()
        if now - self._last_claude_call < CLAUDE_ESCALATION_COOLDOWN:
            logger.debug("DIAG: skipping Claude escalation — cooldown active")
        elif self._claude_ok():
            # Load error patterns for institutional memory
            _error_patterns = ""
            try:
                with open("memory/error_patterns.md", "r") as f:
                    _error_patterns = f.read()[:1500]
            except Exception:
                pass

            diag_report = (
                "CALLISTO SELF-DIAGNOSTIC — CRITICAL ISSUES DETECTED\n\n"
                + "\n".join(
                    f"[{i['severity']}] {i['message']}" for i in issues
                )
                + "\n\nPipeline state:\n"
                f"- Cycles: {self._cycles}\n"
                f"- Hypotheses generated: {self._hypotheses_generated}\n"
                f"- Backtests run: {self._backtests_run}\n"
                f"- Promotions: {self._promotions}\n"
                f"- Rejections: {self._rejections}\n\n"
                + (f"KNOWN ERROR PATTERNS (do not repeat):\n{_error_patterns}\n\n" if _error_patterns else "")
                + f"Analyze these diagnostics and suggest specific fixes. "
                f"Focus on: which data is missing, what to collect, "
                f"and whether the pipeline should pause or adjust parameters."
            )
            try:
                result = await escalate_with_ladder(
                    diag_report,
                    task_type="deep_work",
                    hermes_caller="default",
                )
                self._last_claude_call = time.time()
                if result.get("content") and not result.get("error"):
                    logger.info(
                        f"DIAG: Claude analysis received — "
                        f"{len(result['content'])} chars"
                    )
                # Mark all critical issues as escalated regardless of response
                for i in new_critical:
                    self._diagnostic_issues.add(i["key"])
            except Exception as e:
                logger.warning(f"DIAG: Claude escalation failed: {e}")
        else:
            # Defer diagnostic escalation to queue for when Claude returns
            diag_report = (
                "CALLISTO SELF-DIAGNOSTIC — CRITICAL ISSUES DETECTED\n\n"
                + "\n".join(
                    f"[{i['severity']}] {i['message']}" for i in issues
                )
                + "\n\nPipeline state:\n"
                f"- Cycles: {self._cycles}\n"
                f"- Hypotheses generated: {self._hypotheses_generated}\n"
                f"- Backtests run: {self._backtests_run}\n"
                f"- Promotions: {self._promotions}\n"
                f"- Rejections: {self._rejections}\n\n"
                f"Analyze these diagnostics and suggest specific fixes. "
                f"Focus on: which data is missing, what to collect, "
                f"and whether the pipeline should pause or adjust parameters."
            )
            await self._work_queue.enqueue("diagnostic_escalation", diag_report, priority=1)
            self._downtime_tracker.item_queued()
            logger.warning(
                f"DIAG: {len(new_critical)} critical issues deferred to work queue "
                f"(Claude unavailable)"
            )

    # Mark non-critical issues as seen too (no re-escalation)
    for i in issues:
        if i["severity"] != "CRITICAL":
            self._diagnostic_issues.add(i["key"])

    # Evict oldest entries if set exceeds cap (prevents unbounded growth)
    if len(self._diagnostic_issues) > self._DIAGNOSTIC_ISSUES_MAX:
        # Sets are unordered; drop arbitrary entries to get back under limit
        excess = len(self._diagnostic_issues) - self._DIAGNOSTIC_ISSUES_MAX
        for _ in range(excess):
            self._diagnostic_issues.pop()

    if not issues:
        logger.info("DIAG: all pipeline health checks passed")


async def phase_refresh_signals(loop) -> None:
    self = loop
    """Retroactive signal refresh — WRITE PATH GATED, OFF BY DEFAULT.

    This phase used to UPDATE backtest_events.signal_generated = 1 whenever
    edge >= threshold, which let a later threshold drop retroactively
    rewrite history (laundered evidence). By default this phase is now
    DIAGNOSE-ONLY: it counts rows that *would* have been upgraded (SELECT,
    no UPDATE) and returns without writing.

    The write path is operator-explicit only:
        CALLISTO_ALLOW_SIGNAL_REFRESH=1
    enables the original retroactive UPDATE (backtest_events +
    backtest_runs.signals_generated + stats recalc).
    """
    import aiosqlite

    db_path = self.backtest_engine.db_path
    allow_write = os.getenv("CALLISTO_ALLOW_SIGNAL_REFRESH") == "1"
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            # Diagnose-only: count events where edge now exceeds threshold
            # but signal=0. Read-only — no evidence rewriting.
            count_row = await db.execute(
                """SELECT COUNT(*) FROM backtest_events be
                   JOIN hypotheses h ON be.hypothesis_id = h.hypothesis_id
                   WHERE be.edge >= h.edge_threshold AND be.edge > 0
                   AND be.signal_generated = 0"""
            )
            row = await count_row.fetchone()
            would_upgrade = row[0] if row else 0
            if not allow_write:
                if would_upgrade:
                    logger.info(
                        f"Signal refresh: {would_upgrade} events WOULD be "
                        "upgraded to signal=1 (write gated; set "
                        "CALLISTO_ALLOW_SIGNAL_REFRESH=1 to enable)"
                    )
                return

            # ── Gated write path (operator-explicit) ──
            # Find events where edge now exceeds threshold but signal=0
            updated = await db.execute(
                """UPDATE backtest_events SET signal_generated = 1
                   WHERE id IN (
                       SELECT be.id FROM backtest_events be
                       JOIN hypotheses h ON be.hypothesis_id = h.hypothesis_id
                       WHERE be.edge >= h.edge_threshold AND be.edge > 0
                       AND be.signal_generated = 0
                   )"""
            )
            if updated.rowcount > 0:
                await db.commit()
                logger.info(
                    f"Signal refresh: upgraded {updated.rowcount} events "
                    f"to signal=1 (threshold lowered after backtest)"
                )
                # Sync backtest_runs.signals_generated from backtest_events
                # so monitoring/display data reflects retroactive updates.
                await db.execute(
                    """UPDATE backtest_runs SET signals_generated = (
                           SELECT COUNT(DISTINCT event_id)
                           FROM backtest_events
                           WHERE backtest_events.run_id = backtest_runs.run_id
                           AND signal_generated = 1
                       )
                       WHERE run_id IN (
                           SELECT DISTINCT run_id FROM backtest_events
                           WHERE signal_generated = 1
                       )"""
                )
                await db.commit()
                # Recalculate full stats for affected runs
                affected_runs = await db.execute(
                    "SELECT DISTINCT run_id FROM backtest_events "
                    "WHERE signal_generated = 1"
                )
                run_ids = [r[0] for r in await affected_runs.fetchall()]
                for rid in run_ids:
                    try:
                        await self.backtest_engine.recalculate_run_stats(rid)
                    except Exception as rc_e:
                        logger.warning(f"Signal refresh: recalculate_run_stats({rid[:8]}) failed: {rc_e}")
    except Exception as e:
        logger.warning(f"Signal refresh failed: {e}")


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


async def phase_generate_hypotheses(loop) -> None:
    self = loop
    """Generate new hypotheses — Claude Code PRIMARY, templates FALLBACK.

    Claude Code is the primary hypothesis generator. Every cycle where
    Claude is available, we ask it to generate hypotheses based on current
    pipeline state, data stats, and what hasn't been tried. Template
    generation is the fallback when Claude is rate-limited.
    """
    now = time.time()
    if now - self._last_hypothesis_gen < HYPOTHESIS_GEN_INTERVAL:
        return

    # When spinning, generate hypotheses biased toward TESTABLE patterns.
    # Previously this disabled generation entirely, creating a permanent
    # deadlock: all existing drafts exhausted → 0 testable → spinning →
    # generation disabled → no new testable drafts → spinning forever.
    spinning_mode = self._spinning_detected
    if spinning_mode:
        logger.info(
            "Research: generating hypotheses in SPINNING RECOVERY mode — "
            "biasing toward pure line-based patterns (no context factors)"
        )
    else:
        logger.info("Research: generating hypotheses (Claude-primary)")
    self._last_hypothesis_gen = now

    total_created = 0
    used_claude = False

    # ── Temporal isolation: compute training cutoff ──
    # All hypotheses generated this cycle train on data before the cutoff.
    # Backtests will only use data AFTER cutoff + gap.
    today = datetime.now(timezone.utc).date()
    training_cutoff = today - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
    training_period_start = "2023-01-01"  # earliest cached data

    # Try to use temporal_analysis module for walk-forward windows
    try:
        from tools.temporal_analysis import get_training_window
        window = get_training_window()
        if window:
            training_period_start = window.get("start", training_period_start)
            training_cutoff = datetime.strptime(
                window.get("end", str(training_cutoff)), "%Y-%m-%d"
            ).date()
            logger.info(f"Research: using temporal_analysis window {training_period_start} to {training_cutoff}")
    except (ImportError, Exception) as e:
        logger.debug(f"Research: temporal_analysis not available, using default window: {e}")

    training_period_end = str(training_cutoff)
    forward_test_start = str(training_cutoff + timedelta(days=BACKTEST_GAP_DAYS))
    logger.info(
        f"Research: temporal isolation — train [{training_period_start} .. {training_period_end}], "
        f"forward-test from {forward_test_start}"
    )

    # ── PRIMARY: hypothesis generation through the ladder ──
    # The ladder picks the best available model for hypothesis_gen
    # (QWEN36 primary, Claude last-resort per MODEL_LADDER).
    from inference import escalate_with_ladder

    if (now - self._last_claude_call > CLAUDE_ESCALATION_COOLDOWN
            and self._claude_ok()):
        try:
            # Gather context for Claude — use lightweight queries instead of loading all rows
            existing_names = list(await self.hypothesis_manager.get_all_names())
            draft_count = await self.hypothesis_manager.count_by_status("draft")
            active_count = await self.hypothesis_manager.count_by_status("backtesting", "paper_trading", "live")
            rejected_count = await self.hypothesis_manager.count_by_status("rejected")

            data_stats = await self.data_collector.get_collection_stats()

            # Get date ranges per sport from DB
            date_ranges = {}
            db = self.data_collector._db
            if db:
                try:
                    cursor = await db.execute(
                        "SELECT sport, MIN(snapshot_date), MAX(snapshot_date), COUNT(*) "
                        "FROM historical_odds_cache GROUP BY sport"
                    )
                    for row in await cursor.fetchall():
                        date_ranges[row[0]] = {
                            "from": row[1], "to": row[2], "records": row[3]
                        }
                except Exception as e:
                    logger.warning(f"Failed to query historical_odds_cache date ranges: {e}")


            # ── Filter sports by data availability ──
            game_counts_by_sport = {}
            if db:
                try:
                    cursor = await db.execute(
                        "SELECT sport, COUNT(*) FROM game_contexts GROUP BY sport"
                    )
                    for row in await cursor.fetchall():
                        game_counts_by_sport[row[0]] = row[1]
                except Exception:
                    pass  # Fall back to unfiltered if query fails

            # Gate on BOTH game count AND odds data availability
            sports_with_odds = {s for s, dr in date_ranges.items() if dr.get("records", 0) > 0}
            eligible_sports = [
                s for s in RESEARCH_SPORTS
                if game_counts_by_sport.get(s, 0) >= MIN_GAMES_FOR_HYPOTHESIS
                and s in sports_with_odds
            ]
            ineligible_sports = []
            for s in RESEARCH_SPORTS:
                gc = game_counts_by_sport.get(s, 0)
                if gc < MIN_GAMES_FOR_HYPOTHESIS:
                    ineligible_sports.append(f"{s} ({gc} games)")
                elif s not in sports_with_odds:
                    ineligible_sports.append(f"{s} ({gc} games, NO odds data)")
            if ineligible_sports:
                logger.info(
                    f"Research: sports excluded from hypothesis gen "
                    f"(need >={MIN_GAMES_FOR_HYPOTHESIS} games AND odds data): {ineligible_sports}"
                )

            # Build regime analysis context — highlight teams with actionable signals
            regime_context = ""
            if _regime_cache:
                regime_lines = []
                for cache_key, regime in _regime_cache.items():
                    if regime.get("has_edge_signal"):
                        signals = regime.get("actionable_signals", [])
                        team = regime.get("team", cache_key)
                        pr = regime.get("power_rating", {})
                        regime_label = pr.get("regime", "stable") if isinstance(pr, dict) else "stable"
                        recency = regime.get("recency_bias", {})
                        bias_dir = recency.get("bias_direction", "neutral") if isinstance(recency, dict) else "neutral"
                        bias_mag = recency.get("bias_magnitude", 0) if isinstance(recency, dict) else 0
                        mr = regime.get("mean_reversion", {})
                        mr_expected = mr.get("reversion_expected", False) if isinstance(mr, dict) else False
                        mr_z = mr.get("current_zscore", 0) if isinstance(mr, dict) else 0
                        regime_lines.append(
                            f"  {team}: regime={regime_label}, "
                            f"bias={bias_dir}({bias_mag:.2f}), "
                            f"mean_reversion={'yes' if mr_expected else 'no'}(z={mr_z:.1f}), "
                            f"signals={signals}"
                        )
                if regime_lines:
                    regime_context = (
                        "REGIME ANALYSIS (teams with actionable signals — "
                        "prioritize hypotheses around these):\n"
                        + "\n".join(regime_lines[:20]) + "\n\n"
                    )

            # Build correlation context — strongest market pairs per focus sport
            correlation_context = ""
            try:
                from tools.correlation import list_correlated_markets
                corr_lines = []
                sports_to_check = RESEARCH_SPORTS[:4]
                key_markets = [
                    "team_total", "game_total", "team_spread", "player_points",
                ]
                for fs in sports_to_check:
                    sport_pairs = []
                    for km in key_markets:
                        related = list_correlated_markets(km, fs, min_abs_rho=0.35)
                        for r in related[:3]:
                            pair_str = (
                                f"{km}<->{r['market']}"
                                f"(rho={r['correlation']:.2f})"
                            )
                            if pair_str not in sport_pairs:
                                sport_pairs.append(pair_str)
                    if sport_pairs:
                        corr_lines.append(
                            f"  {fs}: {', '.join(sport_pairs[:6])}"
                        )
                if corr_lines:
                    correlation_context = (
                        "CROSS-MARKET CORRELATIONS (strongest pairs — "
                        "use for SGP/parlay hypotheses):\n"
                        + "\n".join(corr_lines) + "\n\n"
                    )
            except Exception as e:
                logger.debug(f"Correlation context generation failed: {e}")

            spinning_preamble = ""
            if spinning_mode:
                spinning_preamble = (
                    "** SPINNING RECOVERY MODE **\n"
                    "The research loop has been spinning with 0 progress. "
                    "ALL existing 2000+ drafts are untestable — they require context "
                    "factors (weather, pitcher, travel, venue, etc.) that can't be filtered.\n"
                    "You MUST generate hypotheses that are PURELY LINE-BASED — "
                    "using ONLY game_filters and line_filters from the AVAILABLE FILTERS list below.\n"
                    "DO NOT reference weather, pitchers, venue type, travel, altitude, "
                    "bullpen, spring training, roster changes, or any factor not in the filters list.\n"
                    "Focus on: schedule spots (B2B, rest mismatch, road streaks), "
                    "win% ranges, spread ranges, underdog/favorite dynamics, "
                    "and cross-book consensus divergence.\n\n"
                )

            # ── Wiki-in-the-loop: prior-knowledge injection ──
            # Pull relevant articles from the knowledge wiki AND the most
            # recent REJECTED hypotheses in the same cohort. Replaces the
            # old HARDCODED banned list — the banned list below is now
            # generated from live wiki + DB state instead of being frozen
            # in source. (feat/wiki-in-the-loop, 2026-04-22)
            wiki_priors_block = ""
            dynamic_banned_lines: list[str] = []
            if db and _wiki_in_loop_enabled():
                try:
                    # Top-10 wiki articles across the eligible sports, weighted
                    # toward SIGNAL domain (demotion lessons, backtest nulls).
                    priors_query = (
                        f"hypothesis generation priors for sports "
                        f"{eligible_sports} — dead patterns, demotion lessons, "
                        f"null backtests"
                    )
                    prior_articles = await _fetch_wiki_priors(
                        db, priors_query, top_k=10,
                    )
                    wiki_priors_block = _render_wiki_priors_block(
                        prior_articles, max_chars_per=360
                    )
                    # Build banned-list from wiki topics that look like null
                    # results or demotion lessons.
                    for a in prior_articles:
                        topic = a.get("topic", "")
                        if (
                            "null_result" in topic
                            or "live_demotion" in topic
                            or "dead" in topic.lower()
                        ):
                            title = a.get("title", topic)
                            dynamic_banned_lines.append(
                                f"  - {title} (wiki:{topic})"
                            )
                except Exception as e:
                    logger.debug(f"Wiki priors fetch failed (non-fatal): {e}")

                # Last 20 REJECTED hypotheses in similar cohort — negative
                # examples so the generator doesn't re-propose them.
                try:
                    cursor = await db.execute(
                        "SELECT name, thesis, sport, market_type "
                        "FROM hypotheses WHERE status = 'rejected' "
                        "ORDER BY COALESCE(updated_at, created_at) DESC "
                        "LIMIT 20"
                    )
                    rejected_rows = await cursor.fetchall()
                    if rejected_rows:
                        dynamic_banned_lines.append(
                            "  (recently-rejected in same pipeline — "
                            "don't resubmit structurally identical variants)"
                        )
                        for r in rejected_rows[:20]:
                            dynamic_banned_lines.append(
                                f"  - {r[0]} [{r[2]}/{r[3]}]: "
                                f"{(r[1] or '')[:120]}"
                            )
                except Exception as e:
                    logger.debug(f"Rejected-cohort fetch failed: {e}")

            # Fall back to the static banned list ONLY when wiki yields nothing
            # — this preserves behaviour on a fresh install with an empty wiki.
            if dynamic_banned_lines:
                banned_block = (
                    "  BANNED (from LIVE wiki + rejected cohort — "
                    "these patterns are demonstrably dead, do NOT re-propose):\n"
                    + "\n".join(dynamic_banned_lines) + "\n"
                )
            else:
                banned_block = (
                    "  BANNED (already priced, stop generating these):\n"
                    "  - Generic rest/B2B/travel advantages\n"
                    "  - Home underdog ATS\n"
                    "  - Eliminated team fades\n"
                    "  - Basic weather totals\n"
                    "  - Blowout-loss bounce-back (63 variants tested, 0 promoted, 3 anti-predictive at p<0.02 — structurally dead)\n"
                    "  - Any hypothesis that is just 'situational factor X is underpriced'\n"
                    "    without specifying WHY models can't capture it\n"
                )

            prompt = (
                f"CALLISTO HYPOTHESIS GENERATION — Cycle #{self._cycles}\n\n"
                f"{spinning_preamble}"
                f"{wiki_priors_block}"
                f"You are a skeptical quantitative researcher. Your default stance: "
                f"most hypotheses are noise. Your job is to find the rare ones that aren't.\n\n"
                f"BEFORE GENERATING: scrutinize the pipeline state below. If something "
                f"is broken or data quality is insufficient, say so in a 'pipeline_warning' "
                f"field instead of generating garbage hypotheses.\n\n"
                f"PIPELINE STATE:\n"
                f"  Total hypotheses: {draft_count + active_count + rejected_count} "
                f"({draft_count} draft, {active_count} active, {rejected_count} rejected)\n"
                f"  Rejection rate: {rejected_count}/{max(1, rejected_count + active_count)}"
                f" — if this is >90%, challenge whether the pipeline can test ANY hypothesis\n"
                f"  Eligible sports (>={MIN_GAMES_FOR_HYPOTHESIS} games): {', '.join(eligible_sports)}\n"
                f"  Ineligible (insufficient data): {', '.join(ineligible_sports) if ineligible_sports else 'none'}\n"
                f"  Data ranges: {json.dumps(date_ranges)}\n"
                f"  Collection stats: {json.dumps(data_stats)}\n"
                f"  Model: consensus devig (power method) — needs 3+ books to be reliable. "
                f"If most events show books_used=1, the devig is meaningless.\n\n"
                f"EXISTING HYPOTHESIS NAMES (avoid duplicates):\n"
                f"  {json.dumps(existing_names[:50])}\n\n"
                f"ELIGIBLE SPORTS ONLY: {eligible_sports}\n"
                f"DO NOT generate hypotheses for ineligible sports — they will be auto-rejected.\n\n"
                f"{regime_context}"
                f"{correlation_context}"
                f"EDGE PHILOSOPHY — READ THIS CAREFULLY:\n"
                f"Vegas prices rest, travel, B2Bs, weather, and schedule spots CORRECTLY.\n"
                f"Every model already has those columns. Do NOT generate more of these.\n"
                f"We need edges in dimensions that models DON'T HAVE COLUMNS FOR:\n\n"
                f"  UNCONVENTIONAL FACTORS (the kind of thing no model prices):\n"
                f"  - Team identity/cohesion: racial composition, regional identity, religious\n"
                f"    institutional values (e.g. BYU, Notre Dame, Liberty), coaching culture\n"
                f"  - Roster sociology: age variance, draft capital distribution, contract year\n"
                f"    clusters, language barriers, shared alma mater connections\n"
                f"  - Referee/umpire biases: specific officials' tendencies with specific teams,\n"
                f"    foul call patterns by game context, home-whistle strength by ref\n"
                f"  - Psychological momentum: post-trade deadline chemistry disruption, coaching\n"
                f"    hire/fire bounce, rivalry game emotional overperformance, clinch letdown\n"
                f"    dynamics in specific roster age profiles\n"
                f"  - Structural market inefficiencies: SGP correlation mispricing (correlated\n"
                f"    legs priced as independent), alt-line vs main-line gaps, live betting\n"
                f"    overreaction to early scores, cross-book consensus divergence\n"
                f"  - Scheme/matchup geometry: specific offensive system vs specific defensive\n"
                f"    scheme interactions, pace-forcing mismatches, platoon advantages\n"
                f"  - Media/narrative mispricing: nationally televised game line inflation,\n"
                f"    star player absence overreaction, preseason ranking anchor bias\n"
                f"  - Venue-specific micro-factors: altitude, turf vs grass transitions,\n"
                f"    dome-to-outdoor, timezone-specific circadian effects\n"
                f"  - Calendar/scheduling quirks: exam week in college sports, holiday games,\n"
                f"    conference tournament motivation asymmetry\n\n"
                f"{banned_block}\n"
                f"RESPOND WITH EXACTLY THIS JSON (no other text):\n"
                f'{{"hypotheses": [\n'
                f'  {{"name": "unique_snake_case_name", '
                f'"thesis": "Clear testable statement", '
                f'"sport": "<sport_key>", '
                f'"market_type": "spreads|totals|h2h|player_props", '
                f'"edge_threshold": 0.015, '
                f'"game_filters": {{"STRUCTURED filters on game context — see AVAILABLE FILTERS below"}}, '
                f'"line_filters": {{"STRUCTURED filters on bet lines — see AVAILABLE FILTERS below"}}'
                f'}}\n'
                f'], "pipeline_warning": "optional — flag if data quality makes testing pointless"}}\n\n'
                f"AVAILABLE GAME FILTERS (applied per-game BEFORE edge calculation):\n"
                f"  These filter which GAMES are tested. Use ONLY keys listed here:\n"
                f"  - require_b2b: true — at least one team on back-to-back\n"
                f"  - min_rest_mismatch: N — abs(home_rest - away_rest) >= N days\n"
                f"  - max_rest_days: N — at least one team with rest <= N days\n"
                f"  - min_games_in_4: N — at least one team played N+ games in 4 days\n"
                f"  - require_road_streak: N — at least one team on N+ consecutive road games\n"
                f"  - require_sandwich: true — at least one team in sandwich game spot\n"
                f"  - require_revenge: true — rematch within 30 days\n"
                f"  - min_win_pct: 0.65 — at least one team above this win%\n"
                f"  - max_win_pct: 0.35 — at least one team below this win%\n"
                f"  - win_pct_range: [0.43, 0.57] — at least one team in this range (bubble)\n"
                f"  - max_prev_margin: -10 — at least one team lost prev game by 10+ pts\n"
                f"  - min_prev_margin: 15 — at least one team won prev game by 15+ pts\n"
                f"  - side: 'home'|'away' — apply conditions to THIS side specifically\n"
                f"  If your hypothesis doesn't need game filtering, use {{}}\n\n"
                f"AVAILABLE LINE FILTERS (applied per-line DURING edge calculation):\n"
                f"  These filter which BET LINES are evaluated within matching games:\n"
                f"  - home_away: 'home'|'away' — only evaluate this team's line\n"
                f"  - dog_fav: 'underdog'|'favorite' — only evaluate this role\n"
                f"  - side: 'Over'|'Under' — for totals, only evaluate this side\n"
                f"  - spread_range: [3, 7] — only test spreads in this point range\n"
                f"  - spread_min: 3 — only test spreads >= this value\n"
                f"  If your hypothesis doesn't need line filtering, use {{}}\n\n"
                f"AVAILABLE CONTEXT DATA per game (what game_contexts actually stores):\n"
                f"  ALL SPORTS: scores (home/away), rest_days, b2b (boolean), records,\n"
                f"    attendance, venue (name, dome, altitude_ft), tz_offset, national_tv,\n"
                f"    officials (refs/umps), broadcasts, spread, total,\n"
                f"    play_by_play (period-level scoring summaries)\n"
                f"  MLB ONLY: park_factor (venue-specific run environment multiplier)\n"
                f"  NOT AVAILABLE (do NOT reference): umpire strike zones, ref crew tendencies,\n"
                f"    goalie workloads, pitch-level data, weather, public betting %,\n"
                f"    player prop lines, lineup data, advanced team stats\n\n"
                f"CRITICAL: Every hypothesis MUST include game_filters and line_filters.\n"
                f"If a hypothesis can't be expressed with these filters, it CANNOT be tested.\n"
                f"Do NOT generate hypotheses requiring data NOT in the available context list above.\n\n"
                f"RULES:\n"
                f"- Generate 3-5 hypotheses per call\n"
                f"- Spread across multiple sports — do NOT cluster on one sport\n"
                f"- Each hypothesis MUST explain WHY this edge would survive (why can't\n"
                f"  Vegas or sharp models capture this factor?)\n"
                f"- Each must be testable with the ACTUAL data we have (check collection stats)\n"
                f"- Names must be unique (not in existing list)\n"
                f"- Thesis must be specific and falsifiable\n"
                f"- If the pipeline state shows systemic issues (high rejection rate, "
                f"thin data, broken resolution), flag them — do NOT just generate more "
                f"hypotheses into a broken funnel\n"
            )

            result = await escalate_with_ladder(
                prompt,
                task_type="hypothesis_gen",
                hermes_caller="hypothesis_gen",
            )
            self._last_claude_call = time.time()
            self._claude_escalations += 1

            if result.get("content") and not result.get("error"):
                content = result["content"]
                try:
                    # Extract JSON from response
                    json_str = content
                    if "```" in json_str:
                        parts = json_str.split("```")
                        for part in parts:
                            stripped = part.strip()
                            if stripped.startswith("json"):
                                stripped = stripped[4:].strip()
                            if stripped.startswith("{"):
                                json_str = stripped
                                break
                    elif "{" in json_str:
                        start = json_str.index("{")
                        end = json_str.rindex("}") + 1
                        json_str = json_str[start:end]

                    parsed = json.loads(json_str)
                    for nh in parsed.get("hypotheses", []):
                        try:
                            h_sport = nh.get("sport", "basketball_nba")
                            # Hard gate: reject hypotheses for sports with insufficient data
                            if eligible_sports and h_sport not in eligible_sports:
                                logger.info(
                                    f"Research: rejected '{nh.get('name')}' — "
                                    f"sport '{h_sport}' has insufficient data "
                                    f"({game_counts_by_sport.get(h_sport, 0)} games < {MIN_GAMES_FOR_HYPOTHESIS})"
                                )
                                continue
                            h_config = {
                                "source": "claude_primary_gen",
                                "cycle": self._cycles,
                                "training_period_start": training_period_start,
                                "training_period_end": training_period_end,
                                "forward_test_start": forward_test_start,
                            }
                            # Pass structured filters from Claude to model_config
                            if nh.get("game_filters"):
                                h_config["game_filters"] = nh["game_filters"]
                            if nh.get("line_filters"):
                                h_config["line_filters"] = nh["line_filters"]
                            # Enrich with regime data if available for this sport
                            if _regime_cache:
                                sport_regimes = {
                                    k: v for k, v in _regime_cache.items()
                                    if k.startswith(h_sport + ":")
                                    and v.get("has_edge_signal")
                                }
                                if sport_regimes:
                                    # Attach summary of regime signals for backtester
                                    regime_summary = {}
                                    for rk, rv in list(sport_regimes.items())[:5]:
                                        team = rv.get("team", rk)
                                        rb = rv.get("recency_bias", {})
                                        regime_summary[team] = {
                                            "regime": rv.get("power_rating", {}).get("regime", "stable") if isinstance(rv.get("power_rating"), dict) else "stable",
                                            "recency_bias_score": rb.get("bias_magnitude", 0) if isinstance(rb, dict) else 0,
                                            "signals": rv.get("actionable_signals", []),
                                        }
                                    h_config["regime_signals"] = regime_summary
                            await self.hypothesis_manager.create_hypothesis(
                                name=nh.get("name", f"claude_gen_{self._cycles}"),
                                thesis=nh.get("thesis", ""),
                                sport=h_sport,
                                market_type=nh.get("market_type", "spreads"),
                                edge_threshold=nh.get("edge_threshold", 0.015),
                                model_config=h_config,
                            )
                            total_created += 1
                        except Exception as e:
                            logger.warning(f"Failed to create Claude hypothesis: {e}")

                    used_claude = True
                    logger.info(
                        f"Research: Claude generated {total_created} hypotheses"
                    )
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(
                        f"Claude hypothesis response not valid JSON: {e}"
                    )
            elif result.get("rate_limited"):
                logger.info(
                    "Research: Claude rate-limited during hypothesis gen — "
                    "falling back to templates"
                )
        except Exception as e:
            logger.warning(f"Claude hypothesis generation failed: {e}")

    # ── FALLBACK: Template + local model generation when Claude unavailable ──
    if not used_claude:
        # Defer the Claude prompt so it runs when Claude comes back
        from tools.claude_code import is_available as _ca
        if not _ca():
            try:
                existing_names = await self.hypothesis_manager.get_names()
                deferred_prompt = (
                    f"CALLISTO HYPOTHESIS GENERATION — Deferred from Cycle #{self._cycles}\n\n"
                    f"Generate 3-5 UNCONVENTIONAL sports betting hypotheses across: {RESEARCH_SPORTS}\n\n"
                    f"BANNED: rest/B2B, home underdog ATS, eliminated fades, basic weather. "
                    f"Vegas prices these. Find edges in dimensions models lack columns for: "
                    f"team identity/cohesion, roster sociology, ref biases, scheme geometry, "
                    f"SGP correlation mispricing, media narrative inflation, calendar quirks.\n\n"
                    f"AVAILABLE game_filters: require_b2b, min_rest_mismatch, max_rest_days, "
                    f"min_games_in_4, require_road_streak, require_sandwich, require_revenge, "
                    f"min_win_pct, max_win_pct, win_pct_range, max_prev_margin, min_prev_margin, side\n"
                    f"AVAILABLE line_filters: home_away, dog_fav, side, spread_range, spread_min\n\n"
                    f"EXISTING NAMES (avoid duplicates): {json.dumps(existing_names[:30])}\n\n"
                    f"RESPOND WITH JSON: {{\"hypotheses\": [{{\"name\": \"...\", \"thesis\": \"...\", "
                    f"\"sport\": \"...\", \"market_type\": \"...\", \"edge_threshold\": 0.015, "
                    f"\"game_filters\": {{}}, \"line_filters\": {{}}}}]}}"
                )
                await self._work_queue.enqueue("hypothesis_gen", deferred_prompt, priority=2)
                self._downtime_tracker.item_queued()
                logger.info("Research: hypothesis gen deferred to work queue (Claude unavailable)")
            except Exception as e:
                logger.warning(f"Failed to enqueue deferred hypothesis gen: {e}")

            # Try local model via escalation ladder (Apriel > Qwen3 > DeepSeek)
            try:
                from inference import escalate_with_ladder
                existing_l = (await self.hypothesis_manager.get_names())[:30]
                ladder_prompt = (
                    f"Generate 3 testable sports betting hypotheses.\n"
                    f"Sports: {RESEARCH_SPORTS}\n"
                    f"Market types: h2h, spreads, totals, player_points, player_strikeouts\n"
                    f"EXISTING (avoid duplicates): {json.dumps(existing_l)}\n\n"
                    f"AVAILABLE game_filters: require_b2b, min_rest_mismatch, max_rest_days, "
                    f"min_games_in_4, require_road_streak, require_sandwich, require_revenge, "
                    f"min_win_pct, max_win_pct, win_pct_range, max_prev_margin, min_prev_margin, side\n"
                    f"AVAILABLE line_filters: home_away, dog_fav, side, spread_range, spread_min\n\n"
                    f"RESPOND WITH JSON ONLY:\n"
                    f'{{"hypotheses": [{{"name": "sport_descriptive_name", '
                    f'"thesis": "Testable claim", "sport": "basketball_nba", '
                    f'"market_type": "spreads", "edge_threshold": 0.003, '
                    f'"game_filters": {{}}, "line_filters": {{}}}}]}}'
                )
                ladder_result = await escalate_with_ladder(
                    ladder_prompt, task_type="hypothesis_gen", timeout=120,
                )
                ladder_content = ladder_result.get("content", "")
                if ladder_content:
                    from inference import _parse_json_response
                    parsed = _parse_json_response(ladder_content)
                    if parsed and isinstance(parsed, dict):
                        for nh in parsed.get("hypotheses", []):
                            try:
                                _ladder_config = {
                                        "source": f"ladder_{ladder_result.get('model_used', 'unknown')}",
                                        "cycle": self._cycles,
                                        "training_period_start": training_period_start,
                                        "training_period_end": training_period_end,
                                        "forward_test_start": forward_test_start,
                                    }
                                if nh.get("game_filters"):
                                    _ladder_config["game_filters"] = nh["game_filters"]
                                if nh.get("line_filters"):
                                    _ladder_config["line_filters"] = nh["line_filters"]
                                await self.hypothesis_manager.create_hypothesis(
                                    name=nh.get("name", f"ladder_gen_{self._cycles}"),
                                    thesis=nh.get("thesis", ""),
                                    sport=nh.get("sport", "basketball_nba"),
                                    market_type=nh.get("market_type", "spreads"),
                                    edge_threshold=nh.get("edge_threshold", 0.003),
                                    model_config=_ladder_config,
                                )
                                total_created += 1
                                self._hypotheses_generated += 1
                            except Exception as e:
                                logger.debug(f"Ladder hypothesis creation failed: {e}")
                        logger.info(
                            f"Research: ladder model ({ladder_result.get('model_used')}) "
                            f"generated {total_created} hypotheses"
                        )
            except Exception as e:
                logger.warning(f"Ladder hypothesis generation failed: {e}")

            # Also try template-based local fallback for quick hypothesis ideas
            try:
                from tools.work_queue import local_fallback_hypothesis_gen
                pipeline_state = (
                    f"Cycles: {self._cycles}, Hypotheses: {self._hypotheses_generated}, "
                    f"Backtests: {self._backtests_run}"
                )
                existing_names = await self.hypothesis_manager.get_names()
                local_hypos = await local_fallback_hypothesis_gen(
                    pipeline_state, existing_names, ""
                )
                for nh in local_hypos:
                    try:
                        _local_config = {
                                "source": "local_fallback_gen",
                                "cycle": self._cycles,
                                "training_period_start": training_period_start,
                                "training_period_end": training_period_end,
                                "forward_test_start": forward_test_start,
                            }
                        if nh.get("game_filters"):
                            _local_config["game_filters"] = nh["game_filters"]
                        if nh.get("line_filters"):
                            _local_config["line_filters"] = nh["line_filters"]
                        await self.hypothesis_manager.create_hypothesis(
                            name=nh.get("name", f"local_gen_{self._cycles}"),
                            thesis=nh.get("thesis", ""),
                            sport=nh.get("sport", "basketball_nba"),
                            market_type=nh.get("market_type", "spreads"),
                            edge_threshold=nh.get("edge_threshold", 0.015),
                            model_config=_local_config,
                        )
                        total_created += 1
                    except Exception as e:
                        logger.debug(f"Local fallback hypothesis creation failed: {e}")
                if local_hypos:
                    logger.info(f"Research: local model generated {len(local_hypos)} hypotheses")
            except Exception as e:
                logger.debug(f"Local fallback hypothesis gen failed: {e}")

        # Template fallback always runs when Claude didn't
        # Re-check data availability for template path
        _template_eligible = RESEARCH_SPORTS
        if hasattr(self, 'data_collector') and self.data_collector._db:
            try:
                _gc = {}
                cursor = await self.data_collector._db.execute(
                    "SELECT sport, COUNT(*) FROM game_contexts GROUP BY sport"
                )
                for row in await cursor.fetchall():
                    _gc[row[0]] = row[1]
                _template_eligible = [
                    s for s in RESEARCH_SPORTS
                    if _gc.get(s, 0) >= MIN_GAMES_FOR_HYPOTHESIS
                ]
                _skipped = [s for s in RESEARCH_SPORTS if s not in _template_eligible]
                if _skipped:
                    logger.info(
                        f"Research: template gen skipping sports with <{MIN_GAMES_FOR_HYPOTHESIS} games: {_skipped}"
                    )
            except Exception:
                pass
        logger.info("Research: using template fallback for hypothesis generation")
        for sport in _template_eligible:
            try:
                quota = 20
                created = await self.hypothesis_generator.generate_from_templates(
                    sport=sport,
                    max_hypotheses=quota,
                    training_cutoff_date=training_period_end,
                )
                total_created += len(created)
            except Exception as e:
                logger.warning(f"Template generation failed for {sport}: {e}")

    # ── DATA-DRIVEN PATTERN DISCOVERY ──
    # Pure computation — no LLM needed. Discovers statistical anomalies
    # from historical data using temporal splits. Runs EVERY cycle
    # regardless of Claude availability because data-driven hypotheses
    # are grounded in actual patterns, not LLM-plausible theses.
    if total_created < 3:  # Always try unless we already have enough
        try:
            from tools.temporal_analysis import generate_hypotheses_from_analysis
            import asyncio

            pattern_hypotheses = await asyncio.get_event_loop().run_in_executor(
                None,
                generate_hypotheses_from_analysis,
                os.getenv("CALLISTO_DB_PATH", "memory/callisto.db"),
                None,  # all sports
                training_period_end,
                20,  # min_sample
                3.0,  # min_edge %
                0.10,  # max p-value
            )
            for h_def in pattern_hypotheses[:5]:  # cap at 5 per cycle
                try:
                    await self.hypothesis_manager.create_hypothesis(
                        name=h_def["name"],
                        thesis=h_def["thesis"],
                        sport=h_def["sport"],
                        market_type=h_def["market_type"],
                        model_config=h_def.get("model_config", {}),
                    )
                    total_created += 1
                except Exception as e:
                    logger.debug(f"Pattern hypothesis creation failed: {e}")
            if pattern_hypotheses:
                logger.info(
                    f"Research: data-driven pattern discovery generated "
                    f"{min(len(pattern_hypotheses), 5)} hypotheses"
                )
        except Exception as e:
            logger.debug(f"Pattern discovery failed (non-fatal): {e}")

    self._hypotheses_generated += total_created
    logger.info(f"Research: generated {total_created} new hypotheses")


async def phase_injury_prop_hypotheses(loop) -> None:
    self = loop
    """Generate prop hypotheses from current injury data.

    When a key player is out, redistribute_usage() predicts which
    teammates absorb the production. This directly feeds into player
    prop edges: if Tatum is out, Jaylen Brown's usage increases and
    his over on points has value.

    Creates draft hypotheses for each high-confidence prop opportunity.
    """
    from tools.contextual_data import get_injuries as _get_injuries
    from tools.injury_model import redistribute_usage as _redistribute

    _sport_map = {
        "basketball_nba": "NBA",
        "americanfootball_nfl": "NFL",
        "baseball_mlb": "MLB",
    }

    active_sports = list(self.line_monitor._snapshots.keys()) if self.line_monitor else []
    if not active_sports:
        active_sports = ["basketball_nba"]

    total_created = 0
    for sport_key in active_sports:
        model_sport = _sport_map.get(sport_key)
        if not model_sport:
            continue

        try:
            inj_data = await _get_injuries(sport_key)
        except Exception as e:
            logger.warning(f"Injury fetch failed for {sport_key}: {e}")
            continue

        injuries = inj_data.get("injuries", [])
        # Only process players who are OUT (not questionable)
        out_players = [i for i in injuries if (i.get("status") or "").lower() == "out"]
        if not out_players:
            continue

        for inj in out_players[:10]:  # cap at 10 per sport
            player = inj.get("player", "")
            team = inj.get("team", "")
            position = inj.get("position", "")
            if not player or not team:
                continue

            # Build minimal absent player stats from position heuristics
            absent_stats = {}
            if model_sport == "NBA":
                # Default to a starter-level stat line; real data would be better
                absent_stats = {"ppg": 18.0, "rpg": 5.0, "apg": 4.0, "usage_rate": 25.0}
            elif model_sport == "NFL":
                absent_stats = {"role": position or "WR1"}

            try:
                redist = _redistribute(
                    absent_player=player,
                    team_roster=[],  # empty roster triggers generic redistribution
                    sport=model_sport,
                    absent_player_stats=absent_stats or None,
                )
            except Exception as e:
                logger.debug(f"Redistribution failed for {player}: {e}")
                continue

            if not redist:
                continue

            # Create draft hypotheses for top beneficiaries
            for r in redist[:3]:
                beneficiary = r.player if hasattr(r, "player") else "Unknown"
                usage_inc = r.usage_increase if hasattr(r, "usage_increase") else 0
                stat_chg = r.projected_stat_change if hasattr(r, "projected_stat_change") else {}

                if usage_inc < 2.0:
                    continue  # too small to be actionable

                hypo_name = (
                    f"injury_prop_{team}_{beneficiary}_{player}_out"
                ).replace(" ", "_").lower()

                # Check if hypothesis already exists
                try:
                    existing_names = await self.hypothesis_manager.get_all_names()
                    if hypo_name in existing_names:
                        continue
                except Exception:
                    pass

                ppg_inc = stat_chg.get("ppg_increase", stat_chg.get("projected_ppg_increase", 0))
                description = (
                    f"With {player} OUT for {team}, {beneficiary} absorbs "
                    f"+{usage_inc:.1f}% usage (projected +{ppg_inc:.1f} PPG). "
                    f"Player prop overs for {beneficiary} have value when "
                    f"{player} is confirmed out."
                )

                try:
                    await self.hypothesis_manager.create_hypothesis(
                        name=hypo_name,
                        description=description,
                        sport=sport_key,
                        tags=["injury", "prop", "usage_redistribution", "auto_generated"],
                    )
                    total_created += 1
                    logger.info(
                        f"Injury prop hypothesis created: {beneficiary} "
                        f"benefits from {player} OUT (+{usage_inc:.1f}% usage)"
                    )
                except Exception as e:
                    logger.debug(f"Failed to create injury prop hypothesis: {e}")

    if total_created:
        logger.info(f"Injury prop phase: created {total_created} hypotheses")


async def phase_collect_data(loop) -> None:
    self = loop
    """Collect post-game data from ESPN (free).

    Normal cadence: last 7 days every DATA_COLLECTION_INTERVAL.
    Bulk backfill: if game_contexts < 100, one-time 30-day pull to seed the system.
    """
    from datetime import datetime, timedelta, timezone

    now = time.time()
    if now - self._last_data_collect < DATA_COLLECTION_INTERVAL:
        return

    self._last_data_collect = now

    # Determine how far back to collect
    # First collection: 7-day window. Subsequent: 2-day window (today + yesterday)
    lookback_days = 7 if self._data_collections == 0 else 2

    # One-time bulk backfill when data is thin
    if not self._bulk_backfill_done:
        try:
            stats = await self.data_collector.get_collection_stats()
            total_contexts = sum(
                row.get("count", 0)
                for row in stats.get("game_contexts", [])
            )
            if total_contexts < 100:
                lookback_days = 30
                logger.info(
                    f"Research: bulk backfill triggered — only {total_contexts} "
                    f"game contexts, collecting last 30 days"
                )
            else:
                logger.info(
                    f"Research: {total_contexts} game contexts already present, "
                    f"skipping bulk backfill"
                )
        except Exception as e:
            logger.warning(f"Could not check collection stats for backfill: {e}")
        self._bulk_backfill_done = True

        # Also trigger historical odds backfill from odds-api.io Pro
        try:
            from tools.odds_api_io import get_usage_status as _io_usage
            usage = _io_usage()
            remaining = usage.get("remaining", 0)
            if remaining > 1000:
                logger.info(
                    f"Research: triggering historical odds backfill "
                    f"(odds-api.io budget: {remaining} remaining)"
                )
                # Use the HistoricalOddsFetcher to backfill all core sports
                from api import historical_fetcher as _hf
                if _hf:
                    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    thirty_ago = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
                    backfill_sports = [
                        "basketball_nba", "icehockey_nhl",
                        "americanfootball_nfl", "baseball_mlb",
                        "basketball_ncaab",
                    ]
                    for bs in backfill_sports:
                        if not self._running:
                            break
                        try:
                            result = await _hf.bulk_fetch_date_range(
                                sport=bs,
                                start_date=thirty_ago,
                                end_date=today_str,
                            )
                            fetched = result.get("dates_fetched", 0)
                            cached = result.get("dates_cached_already", 0)
                            if fetched > 0:
                                logger.info(
                                    f"Historical backfill {bs}: "
                                    f"{fetched} new dates, {cached} cached"
                                )
                        except Exception as e:
                            logger.debug(f"Historical backfill {bs}: {e}")
            else:
                logger.info(
                    f"Research: skipping historical backfill — "
                    f"odds-api.io budget low ({remaining})"
                )
        except Exception as e:
            logger.debug(f"Historical odds backfill: {e}")

    logger.info(f"Research: collecting post-game data (last {lookback_days} days)")

    today = datetime.now(timezone.utc)
    dates = [today - timedelta(days=d) for d in range(lookback_days)]

    for sport in RESEARCH_SPORTS:
        try:
            for dt in dates:
                date_str = dt.strftime("%Y%m%d")
                scores = await self.data_collector.collect_scores(sport, date_str)
                if scores.get("completed", 0) > 0:
                    await self.data_collector.collect_box_scores(sport, date_str)
                    # Enrich with play-by-play and win probability data
                    await self.data_collector.collect_play_by_play(sport, date_str)

            # Resolve pending paper trades for the same window
            for dt in dates:
                date_fmt = dt.strftime("%Y-%m-%d")
                await self.data_collector.resolve_prop_outcomes(sport, date_fmt)
                await self.data_collector.resolve_game_level_outcomes(sport, date_fmt)

            # Update learned correlations from completed game data
            try:
                from tools.correlation import get_learned_store
                lcs = get_learned_store()
                if lcs is not None and self.data_collector._db is not None:
                    for dt in dates:
                        date_fmt = dt.strftime("%Y-%m-%d")
                        await lcs.update_from_game_data(
                            self.data_collector._db, sport, date_fmt,
                        )
            except Exception as e:
                logger.debug(f"Learned correlation update failed for {sport}: {e}")

            # TCI enrichment for women's basketball (identity/cohesion thesis)
            if sport in ("basketball_ncaaw", "basketball_wnba"):
                try:
                    from tools.tci_scraper import build_tci_for_tournament
                    tci_data = await build_tci_for_tournament(sport=sport)
                    if tci_data:
                        db = self.data_collector._db
                        for team_name, tci in tci_data.items():
                            await db.execute(
                                "INSERT OR REPLACE INTO tci_scores "
                                "(team, sport, tci_score, task_cohesion, social_cohesion, "
                                "experience_ratio, coaching_stability, computed_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                                (
                                    team_name, sport,
                                    tci.get("tci_score", 0),
                                    tci.get("task_cohesion", 0),
                                    tci.get("social_cohesion", 0),
                                    tci.get("experience_ratio", 0),
                                    tci.get("coaching_stability", 0),
                                ),
                            )
                        await db.commit()
                        logger.info(f"TCI: enriched {len(tci_data)} teams for {sport}")
                except Exception as e:
                    logger.debug(f"TCI enrichment failed for {sport}: {e}")

            self._data_collections += 1
        except Exception as e:
            logger.warning(f"Data collection failed for {sport}: {e}")

    # Statcast pitch-level data for MLB (free from Baseball Savant).
    # Each call stores the full pitch timeline in statcast_pitches
    # (one row per pitch, 40 fields of physics + location + outcome).
    if "baseball_mlb" in RESEARCH_SPORTS:
        try:
            for dt in dates[:3]:  # Last 3 days only (Statcast is dense)
                date_fmt = dt.strftime("%Y-%m-%d")
                await self.data_collector.collect_statcast(date_fmt)
        except Exception as e:
            logger.warning(f"Statcast collection failed: {e}")

        # MLB player metadata (height, weight, bats, throws, debut, team).
        # Refresh at most once per day — roster moves are sparse, and the
        # endpoint takes ~30 HTTP calls. Anchored on a module-level ts.
        try:
            import time as _t
            last = getattr(self, "_last_mlb_player_refresh", 0.0)
            if _t.time() - last > 86400:  # 24h
                await self.data_collector.collect_mlb_players()
                self._last_mlb_player_refresh = _t.time()
        except Exception as e:
            logger.warning(f"MLB player metadata refresh failed: {e}")

    # ── NHL: shot-level play-by-play + player metadata ──
    # Per-shot events land in nhl_shot_events (coords, shot type, situation,
    # shooter/goalie); player metadata lands in nhl_players (height,
    # weight, shoots, position, birth, draft). Free api-web.nhle.com.
    if "icehockey_nhl" in RESEARCH_SPORTS:
        try:
            for dt in dates[:3]:
                await self.data_collector.collect_nhl_shots(dt.strftime("%Y-%m-%d"))
        except Exception as e:
            logger.warning(f"NHL shot collection failed: {e}")
        try:
            import time as _t
            last = getattr(self, "_last_nhl_player_refresh", 0.0)
            if _t.time() - last > 86400:
                await self.data_collector.collect_nhl_players()
                self._last_nhl_player_refresh = _t.time()
        except Exception as e:
            logger.warning(f"NHL player metadata refresh failed: {e}")

    # ── NFL: play-by-play + roster + combine ──
    # Per-season CSV fetches from nflverse. Season-active cadence: PBP
    # refreshes daily during season (new plays land as weekly games
    # complete); rosters refresh daily; combine is yearly so we gate on
    # 7d cadence to stay polite to GitHub.
    if "americanfootball_nfl" in RESEARCH_SPORTS:
        try:
            import time as _t
            last_pbp = getattr(self, "_last_nfl_pbp_refresh", 0.0)
            if _t.time() - last_pbp > 86400:
                await self.data_collector.collect_nfl_plays()
                self._last_nfl_pbp_refresh = _t.time()
        except Exception as e:
            logger.warning(f"NFL PBP collection failed: {e}")
        try:
            import time as _t
            last_roster = getattr(self, "_last_nfl_roster_refresh", 0.0)
            if _t.time() - last_roster > 86400:
                await self.data_collector.collect_nfl_players()
                self._last_nfl_roster_refresh = _t.time()
        except Exception as e:
            logger.warning(f"NFL roster refresh failed: {e}")
        try:
            import time as _t
            last_combine = getattr(self, "_last_nfl_combine_refresh", 0.0)
            if _t.time() - last_combine > 7 * 86400:
                await self.data_collector.collect_nfl_combine()
                self._last_nfl_combine_refresh = _t.time()
        except Exception as e:
            logger.warning(f"NFL combine refresh failed: {e}")

    # ── NBA: shot chart + player metadata ──
    # stats.nba.com throttles hard under burst load, so we pace with a
    # 0.6s inter-request delay inside the collector and only fetch the
    # last 3 days' shots. Player metadata refresh once per day.
    if "basketball_nba" in RESEARCH_SPORTS:
        try:
            for dt in dates[:3]:
                await self.data_collector.collect_nba_shots(dt.strftime("%Y-%m-%d"))
        except Exception as e:
            logger.warning(f"NBA shot collection failed: {e}")
        try:
            import time as _t
            last = getattr(self, "_last_nba_player_refresh", 0.0)
            if _t.time() - last > 86400:
                await self.data_collector.collect_nba_players()
                self._last_nba_player_refresh = _t.time()
        except Exception as e:
            logger.warning(f"NBA player metadata refresh failed: {e}")

    # ── NCAA MBB + WBB: player metadata + per-game box stats ──
    for ncaa_sport in ("basketball_ncaab", "basketball_ncaaw"):
        if ncaa_sport not in RESEARCH_SPORTS:
            continue
        try:
            for dt in dates[:3]:
                await self.data_collector.collect_ncaa_basketball_game_stats(
                    ncaa_sport, dt.strftime("%Y%m%d")
                )
        except Exception as e:
            logger.warning(f"{ncaa_sport} box stats failed: {e}")
        try:
            import time as _t
            last_key = f"_last_{ncaa_sport}_player_refresh"
            last = getattr(self, last_key, 0.0)
            if _t.time() - last > 7 * 86400:  # rosters rarely change mid-season
                await self.data_collector.collect_ncaa_basketball_players(ncaa_sport)
                setattr(self, last_key, _t.time())
        except Exception as e:
            logger.warning(f"{ncaa_sport} roster refresh failed: {e}")

    # ── PGA GOLF: per-round strokes-gained + core stats ──
    if "golf_pga" in RESEARCH_SPORTS:
        try:
            import time as _t
            last = getattr(self, "_last_golf_rounds_refresh", 0.0)
            if _t.time() - last > 86400:
                await self.data_collector.collect_golf_player_rounds()
                self._last_golf_rounds_refresh = _t.time()
        except Exception as e:
            logger.warning(f"Golf rounds collection failed: {e}")

    # Collect pre-calculated value bets from Odds-API.io Pro
    # These are updated every 5 seconds with EV computed from consensus
    try:
        from tools.odds_api_io import get_value_bets
        for book in ["DraftKings", "Fanatics"]:
            vb = await get_value_bets(book)
            if vb.get("count", 0) > 0:
                logger.info(
                    f"Research: {vb['count']} value bets from {book} "
                    f"(top EV: {max(b['ev_pct'] for b in vb['bets']):.1%})"
                )
                # Store in ev_opportunities table for edge scanner.
                # NOTE 2026-04-18: column names map onto line_monitor's canonical
                # schema (game_id/bookmaker/team/edge/detected_at). `source` is
                # 'odds_api_io_pro' so downstream consumers can distinguish
                # provider-fed value bets from on-box line-movement EV scans.
                try:
                    db = self.data_collector._db
                    if db:
                        for bet in vb["bets"]:
                            if bet["ev_pct"] >= 0.01:  # Only store 1%+ EV
                                await db.execute(
                                    "INSERT INTO ev_opportunities "
                                    "(detected_at, sport, game_id, team, market, "
                                    "bookmaker, edge, expected_value, source) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'odds_api_io_pro')",
                                    (
                                        bet.get("updated_at", ""),
                                        bet.get("sport", ""),
                                        bet.get("event_id", ""),
                                        bet.get("side", ""),
                                        bet.get("market", ""),
                                        bet.get("bookmaker", ""),
                                        bet.get("ev_pct", 0.0),
                                        bet.get("ev_pct", 0.0),
                                    ),
                                )
                        await db.commit()
                except Exception as e:
                    logger.debug(f"Value bet storage: {e}")
    except Exception as e:
        logger.warning(f"Value bets collection failed: {e}")

    # Collect pre-calculated arbitrage opportunities from Odds-API.io Pro
    try:
        from tools.odds_api_io import get_arbitrage_bets
        arb = await get_arbitrage_bets()
        if arb.get("count", 0) > 0:
            logger.info(
                f"Research: {arb['count']} arbitrage opportunities found "
                f"(guaranteed profit regardless of outcome)"
            )
            # Store for analysis — arbs indicate book disagreement.
            # Same canonical-schema mapping as value-bet path above; source
            # 'arbitrage' lets downstream consumers filter arb signals.
            try:
                db = self.data_collector._db
                if db:
                    for bet in arb.get("bets", []):
                        await db.execute(
                            "INSERT INTO ev_opportunities "
                            "(detected_at, sport, game_id, team, market, "
                            "bookmaker, edge, expected_value, source) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'arbitrage')",
                            (
                                bet.get("updated_at", ""),
                                bet.get("sport", ""),
                                bet.get("event_id", ""),
                                bet.get("side", "arb"),
                                bet.get("market", ""),
                                bet.get("bookmakers", "multi"),
                                bet.get("profit_pct", 0),
                                bet.get("profit_pct", 0),
                            ),
                        )
                    await db.commit()
            except Exception as e:
                logger.debug(f"Arbitrage storage: {e}")
    except Exception as e:
        logger.debug(f"Arbitrage collection: {e}")


async def phase_embed_data(loop) -> None:
    self = loop
    """Embed new game contexts into the vector store."""
    from tools.embeddings import embed_game_context

    contexts = await self.data_collector.get_unembedded_contexts(limit=50)
    if not contexts:
        return

    logger.info(f"Research: embedding {len(contexts)} game contexts")

    for ctx in contexts:
        try:
            await embed_game_context(
                store=self.vector_store,
                sport=ctx["sport"],
                game_date=ctx["game_date"],
                home_team=ctx["home_team"],
                away_team=ctx["away_team"],
                context=ctx["context"],
            )
            await self.data_collector.mark_embedded(ctx["id"])
        except Exception as e:
            logger.warning(f"Embedding failed for context {ctx['id']}: {e}")


async def phase_evaluate(loop) -> None:
    self = loop
    """Evaluate backtesting hypotheses for promotion or rejection.

    Enforces temporal isolation: a hypothesis can only be promoted if
    its backtest period does NOT overlap its training period. This
    prevents circular testing from ever reaching paper trading or live.
    """
    # First, resolve unresolved backtest events from game_results.
    # MEMORY FIX: resolve per-sport for active hypotheses only, not the
    # entire 38K+ backtest_events table. The unbounded query was loading
    # all rows every 60s → 1643 MB/hr leak (CPython pymalloc never frees).
    try:
        active_sports = set()
        cursor = await self.backtest_engine._db.execute(
            "SELECT DISTINCT sport FROM hypotheses WHERE status IN ('backtesting', 'paper_trading')"
        )
        for row in await cursor.fetchall():
            active_sports.add(row[0])
        total_resolved = 0
        for sport in active_sports:
            resolution = await self.backtest_engine.resolve_from_game_results(sport=sport)
            total_resolved += resolution.get("resolved", 0)
        if total_resolved > 0:
            logger.info(
                f"Research: resolved {total_resolved} backtest events "
                f"from game_results ({len(active_sports)} sports)"
            )
    except Exception as e:
        logger.warning(f"Backtest resolution failed: {e}")

    # ── Paper trading evaluation FIRST ──
    # Paper_trading hypotheses are closest to live and there are only a handful.
    # Evaluate them before backtesting so they always get processed even if the
    # backtesting loop (which can have 15+ hypotheses × 60s each) times out the
    # phase. Previously this block was at the END of _phase_evaluate and never
    # ran because backtesting evaluation consumed the entire 600s budget.
    paper = await self.hypothesis_manager.list_hypotheses(status="paper_trading")
    for h in paper:
        try:
            model_config = h.get("model_config", {})
            if isinstance(model_config, str):
                try:
                    model_config = json.loads(model_config)
                except (json.JSONDecodeError, TypeError):
                    model_config = {}

            has_temporal = bool(model_config.get("training_period_end"))
            has_backtest = bool(model_config.get("temporal_isolation"))

            if not has_temporal and not has_backtest:
                logger.warning(
                    f"Research: hypothesis {h['hypothesis_id']} lacks temporal "
                    f"isolation metadata — allowing paper trade eval but flagging"
                )

            result = await self.hypothesis_manager.auto_promote(h["hypothesis_id"])
            action = result.get("action", "held")
            if action == "promoted":
                self._promotions += 1
                logger.info(
                    f"Research: hypothesis {h['hypothesis_id']} PROMOTED TO LIVE"
                )
                try:
                    await telegram.alert_system(
                        f"HYPOTHESIS PROVEN: {h['name']}\n"
                        f"Thesis: {h['thesis'][:200]}\n"
                        f"Status: LIVE — ready for real money\n"
                        f"Temporal isolation: {'YES' if has_temporal else 'LEGACY (no metadata)'}"
                    )
                except Exception as e:
                    logger.warning(f"Telegram notification failed for proven hypothesis {h['name']}: {e}")
            else:
                checks = result.get("checks", [])
                reason = result.get("reason", "")
                logger.info(
                    f"Research: paper_trading {h.get('name', h['hypothesis_id'])} "
                    f"{action.upper()} — reason={reason[:200] if reason else 'N/A'}, "
                    f"gates={checks}"
                )
        except Exception as e:
            logger.warning(f"Paper trade eval failed for {h['hypothesis_id']}: {e}")

    backtesting = await self.hypothesis_manager.list_hypotheses(status="backtesting")

    # ── Recovery: promote stuck drafts with completed backtests ──
    # If the system restarts after a backtest completes but before the
    # draft→backtesting promotion, the hypothesis stays in draft forever.
    # This sweep catches those orphans and promotes them.
    try:
        db = self.hypothesis_manager._db
        cursor = await db.execute(
            "SELECT DISTINCT h.hypothesis_id, h.name "
            "FROM hypotheses h "
            "JOIN backtest_runs br ON h.hypothesis_id = br.hypothesis_id "
            "WHERE h.status = 'draft' "
            "AND br.total_events > 0 "
            "AND br.completed_at IS NOT NULL"
        )
        stuck_drafts = await cursor.fetchall()
        for hid, hname in stuck_drafts:
            await self.hypothesis_manager.update_status(
                hid, "backtesting",
                "auto:recovery — draft had completed backtests, promoting"
            )
            logger.info(
                f"Research: recovered stuck draft {hname} → backtesting"
            )
            # Add to current evaluation batch
            h_data = await self.hypothesis_manager.get_hypothesis(hid)
            if h_data:
                backtesting.append(h_data)
    except Exception as e:
        logger.warning(f"Stuck draft recovery failed: {e}")

    # ── Batch-limit: evaluate top N by signal count per cycle ──
    # IMPORTANT: batch selection happens BEFORE stats recalculation so we
    # only recalculate the hypotheses we're actually evaluating (not all 40+).
    # With 60s/hyp timeout and 600s phase timeout, 8 fits safely
    # (8 × 60s = 480s worst-case, leaves 120s margin).
    MAX_EVALUATE_PER_CYCLE = 8
    if len(backtesting) > MAX_EVALUATE_PER_CYCLE:
        try:
            db = self.hypothesis_manager._db
            cursor = await db.execute(
                "SELECT hypothesis_id, "
                "SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END) as signals "
                "FROM backtest_events "
                "WHERE hypothesis_id IN ({}) "
                "GROUP BY hypothesis_id "
                "ORDER BY signals DESC "
                "LIMIT ?".format(
                    ",".join("?" for _ in backtesting)
                ),
                [h["hypothesis_id"] for h in backtesting] + [MAX_EVALUATE_PER_CYCLE],
            )
            top_ids = {row[0] for row in await cursor.fetchall()}
            # Always include hypotheses with no backtest events (need initial eval)
            no_data_ids = {
                h["hypothesis_id"] for h in backtesting
                if h["hypothesis_id"] not in top_ids
            }
            # Limit no-data to 5 per cycle
            no_data_sample = set(list(no_data_ids)[:5])
            priority_ids = top_ids | no_data_sample
            backtesting = [h for h in backtesting if h["hypothesis_id"] in priority_ids]
            logger.info(
                f"Research: evaluating {len(backtesting)} hypotheses "
                f"(top {MAX_EVALUATE_PER_CYCLE} by signals + {len(no_data_sample)} new)"
            )
        except Exception as e:
            logger.warning(f"Batch-limit query failed, evaluating all: {e}")

    # Recompute backtest_runs stats from backtest_events — scoped to the
    # batch being evaluated. This fixes the stale stats problem: retroactive
    # signal updates and game resolution change backtest_events AFTER the run
    # completes, but backtest_runs keeps the original stats. The promotion
    # gate checks backtest_runs, so stale data blocks promotion.
    # Previously recalculated ALL runs in the batch every cycle (even unchanged
    # ones), causing 10-15 min stalls. Now uses a lightweight fingerprint cache
    # inside recalculate_all_active_runs: only runs with new/changed
    # backtest_events (new events, signal flips, result resolution) get the
    # expensive scipy/numpy recompute. Unchanged runs are skipped in O(1).
    try:
        batch_ids = [h["hypothesis_id"] for h in backtesting]
        paper_ids = [
            h["hypothesis_id"]
            for h in await self.hypothesis_manager.list_hypotheses(status="paper_trading")
        ]
        all_recompute_ids = batch_ids + paper_ids
        # Expensive recalculation (scipy/numpy) only for the batch
        updated = await self.backtest_engine.recalculate_all_active_runs(
            hypothesis_ids=all_recompute_ids
        )
        # Sync hypothesis_stats for ALL backtesting hypotheses (not just
        # the batch). The sync itself is cheap (reads from backtest_runs),
        # only the recalculation above is expensive. Without this, hypotheses
        # outside the top-8 batch have perpetually stale hypothesis_stats,
        # which breaks auto-reject tiers and promotion gate evaluation.
        all_backtesting_ids = [
            h["hypothesis_id"]
            for h in await self.hypothesis_manager.list_hypotheses(status="backtesting")
        ]
        all_sync_ids = list(set(all_backtesting_ids + paper_ids))
        if updated > 0:
            logger.info(f"Research: recomputed stats for {updated} backtest runs (batch of {len(all_recompute_ids)}, incl {len(paper_ids)} paper_trading)")
        # ── Always sync hypothesis_stats from backtest_runs ──
        # Must run even when updated==0: after a restart the fingerprint
        # cache is rebuilt but backtest_runs may already be correct, so
        # recalculate returns 0.  Meanwhile hypothesis_stats can be stale
        # from the previous session (e.g. paper_trading hypothesis promoted
        # but stats still show old stage/p_value).  The sync is cheap
        # (one query + N deletes + N inserts) so always running it is safe.
        if all_sync_ids:
            try:
                from tools.db_utils import execute_with_retry, commit_with_retry
                db = self.backtest_engine._db
                now = datetime.now(timezone.utc).isoformat()
                hs_placeholders = ",".join("?" for _ in all_sync_ids)
                # Get the latest run per hypothesis (most recent run_id)
                hs_cursor = await db.execute(
                    f"SELECT br.hypothesis_id, "
                    f"  br.total_events, br.signals_generated, "
                    f"  br.actual_win, br.actual_loss, br.actual_push, "
                    f"  br.hit_rate, br.avg_edge, br.avg_ev, br.avg_clv, "
                    f"  br.roi_pct, br.sharpe_ratio, br.p_value_binomial, "
                    f"  br.sortino_ratio_val, br.brier_score, br.information_coefficient, "
                    f"  h.significance_level, h.min_sample_size, h.status "
                    f"FROM backtest_runs br "
                    f"JOIN hypotheses h ON br.hypothesis_id = h.hypothesis_id "
                    f"WHERE br.hypothesis_id IN ({hs_placeholders}) "
                    f"ORDER BY br.run_id DESC",
                    all_sync_ids,
                )
                rows = await hs_cursor.fetchall()
                # Keep only the latest run per hypothesis
                seen = set()
                synced = 0
                for row in rows:
                    hid = row[0]
                    if hid in seen:
                        continue
                    seen.add(hid)
                    (total_n, signals_n, wins, losses, pushes,
                     hit_rate, avg_edge, avg_ev, avg_clv,
                     roi_pct, sharpe, p_value,
                     sortino, brier, ic,
                     sig_level, min_sample, status) = row[1:]
                    # Determine stage from hypothesis status
                    stage = "paper_trade" if status == "paper_trading" else "backtest"
                    decided = (wins or 0) + (losses or 0)
                    sig_level = sig_level or 0.05
                    min_sample = min_sample or 50
                    is_significant = (
                        p_value is not None
                        and p_value < sig_level
                        and decided >= min_sample
                    )
                    # Delete ALL stages for this hypothesis — when promoted
                    # from backtesting→paper_trading the old row has
                    # stage='backtest' but we'd be inserting stage='paper_trade'.
                    # Without clearing all stages the stale row persists and
                    # the promotion gate reads the wrong p_value.
                    await execute_with_retry(
                        db,
                        "DELETE FROM hypothesis_stats "
                        "WHERE hypothesis_id = ?",
                        (hid,),
                        operation="sync hypothesis_stats delete",
                    )
                    await execute_with_retry(
                        db,
                        "INSERT INTO hypothesis_stats "
                        "(hypothesis_id, stage, computed_at, total_n, signals_n, "
                        "win, loss, push_, hit_rate, avg_edge, avg_ev, avg_clv, "
                        "positive_clv_rate, roi_pct, sharpe, max_drawdown, p_value, "
                        "is_significant, sortino, brier_score, information_coefficient) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (hid, stage, now, total_n or 0, signals_n or 0,
                         wins or 0, losses or 0, pushes or 0,
                         hit_rate, avg_edge, avg_ev, avg_clv,
                         None, roi_pct, sharpe, None, p_value,
                         is_significant,
                         sortino, brier, ic),
                        operation="sync hypothesis_stats insert",
                    )
                    synced += 1
                if synced > 0:
                    await commit_with_retry(db, operation="sync hypothesis_stats")
                    logger.info(f"Research: synced hypothesis_stats for {synced} hypotheses from backtest_runs")
            except Exception as e:
                logger.warning(f"hypothesis_stats sync from backtest_runs failed: {e}")
    except Exception as e:
        logger.warning(f"Backtest stats recompute failed: {e}")

    for h in backtesting:
        try:
            # ── Temporal isolation gate ──
            model_config = h.get("model_config", {})
            if isinstance(model_config, str):
                try:
                    model_config = json.loads(model_config)
                except (json.JSONDecodeError, TypeError):
                    model_config = {}

            overlap_err = self._check_temporal_overlap(model_config)
            if overlap_err:
                logger.error(
                    f"Research: REJECTING {h['hypothesis_id']} — {overlap_err}"
                )
                await self.hypothesis_manager.update_status(
                    h["hypothesis_id"], "rejected",
                    f"auto:temporal_overlap — {overlap_err}",
                    expected_status=h.get("status", "backtesting"),
                )
                self._rejections += 1
                continue

            # ── Context coverage gate ──
            # If a hypothesis was backtested before the context coverage check
            # was added, its results are noise. Move back to draft so it can
            # be properly evaluated when game context enrichment is available.
            from tools.backtest import BacktestEngine
            ctx_coverage = BacktestEngine.compute_context_coverage(model_config)
            has_struct = BacktestEngine.has_structured_filters(model_config)

            # Also infer context needs from thesis/name (same logic as
            # run_backtest). Without this, hypotheses with empty
            # context_factors appear "fully filterable" (coverage=1.0)
            # even when their name implies unfilterable conditions.
            if ctx_coverage >= 0.5 and not model_config.get("context_factors"):
                thesis = h.get("thesis", "")
                h_name = h.get("name", "")
                inferred = BacktestEngine._infer_context_needs(thesis, h_name)
                if inferred and not has_struct:
                    ctx_coverage = 0.0
                    logger.info(
                        f"Research: {h['hypothesis_id']} ({h_name}) — inferred "
                        f"unfilterable context needs: {inferred}"
                    )
                elif inferred and has_struct:
                    logger.info(
                        f"Research: {h['hypothesis_id']} ({h_name}) — inferred "
                        f"unfilterable needs {inferred} but structured filters present — proceeding"
                    )

            # Also check needs_unique_data flag from self-repair
            if model_config.get("needs_unique_data"):
                logger.warning(
                    f"Research: demoting {h['hypothesis_id']} to draft — "
                    f"flagged as needs_unique_data (duplicate event set)"
                )
                await self.hypothesis_manager.update_status(
                    h["hypothesis_id"], "draft",
                    "auto:needs_unique_data — stale backtest with duplicate event set",
                    expected_status=h.get("status", "backtesting"),
                )
                continue

            if ctx_coverage < 0.5:
                ctx_factors = model_config.get("context_factors", [])
                # Count how many times this hypothesis has been demoted.
                # After 2 demotions, reject instead of creating a circular loop.
                demotion_count = model_config.get("demotion_count", 0) + 1
                model_config["demotion_count"] = demotion_count
                await self.hypothesis_manager._db.execute(
                    "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                    (json.dumps(model_config), h["hypothesis_id"]),
                )
                await self.hypothesis_manager._db.commit()

                if demotion_count >= 2:
                    logger.info(
                        f"Research: rejecting {h['hypothesis_id']} — demoted "
                        f"{demotion_count}x for ctx_coverage={ctx_coverage:.0%}. "
                        f"Hypothesis is untestable with available data."
                    )
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "rejected",
                        f"auto:untestable_context — demoted {demotion_count}x, "
                        f"ctx_coverage={ctx_coverage:.0%}",
                        expected_status=h.get("status", "backtesting"),
                    )
                    self._rejections += 1
                else:
                    logger.warning(
                        f"Research: demoting {h['hypothesis_id']} to draft — "
                        f"context_coverage={ctx_coverage:.0%} ({len(ctx_factors)} "
                        f"factors, most unfilterable). Attempt {demotion_count}/2."
                    )
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "draft",
                        f"auto:low_context_coverage ({ctx_coverage:.0%}) — "
                        f"needs game context enrichment (demotion {demotion_count}/2)",
                        expected_status=h.get("status", "backtesting"),
                    )
                continue

            # Per-hypothesis timeout: prevent a single slow auto_promote
            # from consuming the entire 600s phase budget.
            _eval_t0 = time.time()
            try:
                result = await asyncio.wait_for(
                    self.hypothesis_manager.auto_promote(h["hypothesis_id"]),
                    timeout=60,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"Evaluation TIMEOUT (60s) for {h['hypothesis_id']} "
                    f"({h.get('name', '?')})"
                )
                continue
            _eval_elapsed = time.time() - _eval_t0
            if _eval_elapsed > 10:
                logger.warning(
                    f"Slow eval: {h.get('name', h['hypothesis_id'])} "
                    f"took {_eval_elapsed:.1f}s"
                )
            action = result.get("action", "held")

            if action == "promoted":
                self._promotions += 1
                logger.info(
                    f"Research: hypothesis {h['hypothesis_id']} PROMOTED to "
                    f"{result.get('new_status')}"
                )
            elif action == "rejected":
                self._rejections += 1
                logger.info(
                    f"Research: hypothesis {h['hypothesis_id']} REJECTED — "
                    f"data disproves thesis"
                )
            else:
                # Log gate checks for "held" hypotheses so we can diagnose
                # why promotion isn't happening.
                checks = result.get("checks", [])
                reason = result.get("reason", "")
                if checks or reason:
                    logger.info(
                        f"Research: {h.get('name', h['hypothesis_id'])} HELD — "
                        f"reason={reason[:120] if reason else 'N/A'}, "
                        f"gates={checks}"
                    )
        except Exception as e:
            logger.warning(
                f"Evaluation failed for {h['hypothesis_id']}: {e}"
            )

    # ── Draft-level auto-rejection ──
    # Hypotheses that were backtested but reverted to draft (or never left it)
    # may have definitive negative-edge data. Reject them instead of letting
    # them clog the queue forever.
    #
    # CRITICAL: Only consider SIGNAL events for edge quality. Non-signal events
    # having negative edge is EXPECTED — the hypothesis correctly didn't fire on
    # those. A hypothesis with 16W-1L signals but negative all-event edge is GOOD.
    MIN_EVENTS_FOR_REJECTION = 30
    MAX_SIGNAL_EDGE_FOR_REJECTION = -0.005  # -0.5% avg edge on SIGNAL events
    MIN_SIGNAL_WIN_RATE_PROTECT = 0.60  # Never reject if signals win 60%+
    try:
        db = self.hypothesis_manager._db
        cursor = await db.execute(
            "SELECT h.hypothesis_id, h.name, h.market_type, "
            "COUNT(DISTINCT be.event_id) as events, "
            "COALESCE(AVG(CASE WHEN be.signal_generated = 1 THEN be.edge END), 0) as signal_avg_edge, "
            "COUNT(DISTINCT CASE WHEN be.signal_generated = 1 THEN be.event_id END) as signals, "
            "SUM(CASE WHEN be.signal_generated = 1 AND be.actual_result = 'won' THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN be.signal_generated = 1 AND be.actual_result = 'lost' THEN 1 ELSE 0 END) as losses "
            "FROM hypotheses h "
            "JOIN backtest_events be ON h.hypothesis_id = be.hypothesis_id "
            "WHERE h.status IN ('draft', 'backtesting') "
            "GROUP BY h.hypothesis_id "
            "HAVING events >= ? AND signal_avg_edge < ?",
            (MIN_EVENTS_FOR_REJECTION, MAX_SIGNAL_EDGE_FOR_REJECTION),
        )
        draft_rejects = await cursor.fetchall()
        for row in draft_rejects:
            hid, hname, mtype, events, signal_edge, signals, wins, losses = row
            total_decided = (wins or 0) + (losses or 0)
            win_rate = (wins or 0) / max(total_decided, 1)

            # PROTECT: never reject hypotheses with strong signal win rate
            if total_decided >= 5 and win_rate >= MIN_SIGNAL_WIN_RATE_PROTECT:
                logger.info(
                    f"Research: PROTECTED {hid[:12]} ({hname}) from rejection — "
                    f"signal WR={win_rate:.0%} ({wins}W-{losses}L) despite "
                    f"signal_edge={signal_edge:.2%}"
                )
                continue

            reason = (
                f"auto:negative_edge_disproven — {events} events, "
                f"signal_avg_edge={signal_edge:.2%}, signals={signals}. "
                f"Signal data disproves thesis."
            )
            await self.hypothesis_manager.update_status(hid, "rejected", reason)
            self._rejections += 1
            logger.info(
                f"Research: REJECTED zombie {hid[:12]} ({hname}) — "
                f"{events} events, signal_edge={signal_edge:.2%}, "
                f"{signals} signals, {wins}W-{losses}L"
            )
        if draft_rejects:
            logger.info(
                f"Research: processed {len(draft_rejects)} zombie candidates"
            )
    except Exception as e:
        logger.warning(f"Zombie auto-rejection failed: {e}")

    # ── Untestable draft sweep ──
    # Drafts with ctx_coverage < 0.5 are skipped during backtesting selection
    # (lines 3655-3676) but never rejected — they accumulate forever and
    # trigger spinning detection. Bulk-reject drafts older than 48h that
    # are provably untestable with available data.
    try:
        from tools.backtest import BacktestEngine
        db = self.hypothesis_manager._db
        cursor = await db.execute(
            "SELECT hypothesis_id, name, thesis, model_config, created_at "
            "FROM hypotheses WHERE status = 'draft' "
            "AND created_at < datetime('now', '-48 hours')"
        )
        old_drafts = await cursor.fetchall()
        untestable_rejected = 0
        for row in old_drafts:
            hid, hname, thesis, mc_raw, created = row
            try:
                mc = json.loads(mc_raw) if isinstance(mc_raw, str) else (mc_raw or {})
            except (json.JSONDecodeError, TypeError):
                mc = {}
            ctx_cov = BacktestEngine.compute_context_coverage(mc)
            has_struct = BacktestEngine.has_structured_filters(mc)
            # Also check inferred context needs
            if ctx_cov >= 0.5 and not mc.get("context_factors"):
                inferred = BacktestEngine._infer_context_needs(thesis or "", hname or "")
                if inferred and not has_struct:
                    ctx_cov = 0.0
            if ctx_cov < 0.5 and not has_struct:
                await self.hypothesis_manager.update_status(
                    hid, "rejected",
                    f"auto:untestable_draft — ctx_coverage={ctx_cov:.0%}, "
                    f"stuck in draft >48h. Untestable with available context data."
                )
                untestable_rejected += 1
        if untestable_rejected:
            self._rejections += untestable_rejected
            logger.info(
                f"Research: auto-rejected {untestable_rejected} untestable drafts "
                f"(ctx_coverage < 0.5, >48h old)"
            )
    except Exception as e:
        logger.warning(f"Untestable draft sweep failed: {e}")

    # Anti-predictive sweep: reject hypotheses with strongly negative IC
    # (runs each cycle, not just at startup, to catch newly anti-predictive ones)
    try:
        await self._reject_anti_predictive()
    except Exception as e:
        logger.warning(f"Anti-predictive sweep failed: {e}")
    # Low signal rate sweep: reject hypotheses with 100+ events but <2% signal rate
    try:
        await self._reject_low_signal_rate()
    except Exception as e:
        logger.warning(f"Low-signal-rate sweep failed: {e}")


async def phase_interpret_backtests(loop) -> None:
    self = loop
    """Claude interprets backtest results — signal vs noise, modifications.

    Sends the top 10 hypotheses by signal count with their win/loss/edge
    stats to Claude for interpretation. Claude identifies genuine signals,
    rejects noise, and suggests threshold modifications.

    When Claude is unavailable: defers the prompt to the work queue AND
    runs a local rules-based interpretation as fallback.
    """
    from inference import escalate_with_ladder

    db = self.data_collector._db
    if not db:
        return

    # Get top 10 hypotheses by signal count with stats
    try:
        cursor = await db.execute("""
            SELECT h.hypothesis_id, h.name, h.thesis, h.sport, h.market_type,
                   h.edge_threshold, h.status,
                   COUNT(CASE WHEN be.signal_generated=1 THEN 1 END) as sigs,
                   COUNT(*) as events,
                   SUM(CASE WHEN be.signal_generated=1 AND be.actual_result='won' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN be.signal_generated=1 AND be.actual_result='lost' THEN 1 ELSE 0 END) as losses,
                   SUM(CASE WHEN be.signal_generated=1 AND be.actual_result='push' THEN 1 ELSE 0 END) as pushes,
                   AVG(CASE WHEN be.signal_generated=1 THEN be.edge END) as avg_edge,
                   AVG(CASE WHEN be.signal_generated=1 THEN be.ev_pct END) as avg_ev
            FROM hypotheses h
            LEFT JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
            WHERE h.status IN ('backtesting', 'paper_trading')
            GROUP BY h.hypothesis_id
            HAVING events > 0
            ORDER BY sigs DESC, events DESC
            LIMIT 10
        """)
        rows = await cursor.fetchall()
    except Exception as e:
        logger.warning(f"Failed to query backtest stats for interpretation: {e}")
        return

    if not rows:
        logger.info("Research: no hypotheses with backtest data for interpretation")
        return

    # Format hypothesis data for Claude — pre-compute significance locally
    # using local_significance_test to save Claude tokens on basic math
    hypo_data = []
    for r in rows:
        h_id, name, thesis, sport, mkt, thresh, status = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
        sigs, events, wins, losses, pushes = r[7] or 0, r[8] or 0, r[9] or 0, r[10] or 0, r[11] or 0
        avg_edge, avg_ev = r[12] or 0, r[13] or 0
        resolved = wins + losses + pushes
        hit_rate = wins / max(resolved, 1)

        entry = {
            "id": h_id, "name": name, "thesis": thesis[:200],
            "sport": sport, "market": mkt, "threshold": thresh,
            "status": status, "signals": sigs, "events": events,
            "wins": wins, "losses": losses, "pushes": pushes,
            "hit_rate": round(hit_rate, 4),
            "avg_edge": round(avg_edge, 5),
            "avg_ev": round(avg_ev, 5),
        }

        # Local significance test — pre-compute p-value and z-score
        # so Claude can focus on interpretation, not basic math
        if resolved >= 2:
            try:
                from tools.local_compute import local_significance_test
                sig_events = [
                    {"edge": avg_edge, "won": i < wins}
                    for i in range(resolved)
                ]
                sig_result = await local_significance_test(sig_events)
                entry["z_score"] = sig_result.get("z_score", 0)
                entry["p_value"] = sig_result.get("p_value", 1.0)
                entry["significant"] = sig_result.get("significant", False)
            except Exception:
                pass

        hypo_data.append(entry)

    # Load error patterns for institutional memory
    error_patterns = ""
    try:
        with open("memory/error_patterns.md", "r") as f:
            error_patterns = f.read()[:1500]  # Cap at 1500 chars to save context
    except Exception:
        pass

    prompt = (
        f"CALLISTO BACKTEST INTERPRETATION — Cycle #{self._cycles}\n\n"
        f"You are a statistician reviewing backtest results. Your bias is toward "
        f"skepticism: most patterns are noise, and you must prove otherwise.\n\n"
        + (f"KNOWN ERROR PATTERNS (avoid repeating these mistakes):\n{error_patterns}\n\n" if error_patterns else "")
        + f"Before evaluating any hypothesis, ask: was this a FAIR test?\n"
        f"- If events=15 and signals=0, that is NOT enough data to reject — hold it.\n"
        f"- If avg_edge is computed from 1 book, the entire edge is an artifact.\n"
        f"- If all hypotheses show similar event counts, the backtest filter is broken.\n\n"
        f"HYPOTHESIS BACKTEST RESULTS (top 10 by signal count):\n"
        f"{json.dumps(hypo_data, indent=2)}\n\n"
        f"STATISTICAL CONTEXT:\n"
        f"- A fair coin has ~50% hit rate. Signal needs to beat that consistently.\n"
        f"- With <30 resolved bets, results are noise. DO NOT reject on thin data.\n"
        f"- avg_edge > 0.03 with hit_rate > 0.53 over 50+ resolved is promising.\n"
        f"- 0 signals after 50+ events means the hypothesis never fires — reject it.\n"
        f"- Low signal rate (<5%) with poor hit rate: lower the threshold, don't kill it.\n"
        f"- Before rejecting: steelman the hypothesis. What is the strongest case it's real?\n"
        f"  Only reject if you can refute that case with the data.\n\n"
        f"RESPOND WITH EXACTLY THIS JSON (no other text):\n"
        f'{{"data_quality_assessment": "honest 1-sentence verdict on whether these backtests are reliable", '
        f'"reject": ["hypothesis_id — ONLY with 50+ events AND fair test conditions"], '
        f'"modify": [{{"id": "hypothesis_id", "new_threshold": 0.025, "reason": "..."}}], '
        f'"insights": "What patterns are working, what isn\'t, and what the pipeline should change"}}\n\n'
        f"RULES:\n"
        f"- data_quality_assessment FIRST: are these results trustworthy?\n"
        f"- reject: ONLY hypotheses with clear disproof (0 signals after 50+ events with 3+ books)\n"
        f"- modify: lower thresholds on promising hypotheses rather than killing them\n"
        f"- If data quality is poor, say so and recommend holding rather than rejecting\n"
    )

    if not self._claude_ok():
        await self._work_queue.enqueue("interpret_backtests", prompt, priority=2)
        self._downtime_tracker.item_queued()
        logger.info("Research: backtest interpretation deferred to work queue (Claude unavailable)")

        # Run local rules-based interpretation as fallback
        try:
            from tools.work_queue import local_fallback_interpret
            local_actions = await local_fallback_interpret(hypo_data)
            rejected = 0
            for hid in local_actions.get("reject", []):
                try:
                    await self.hypothesis_manager.update_status(
                        hid, "rejected", "local_fallback_interpret"
                    )
                    rejected += 1
                    self._rejections += 1
                except Exception:
                    pass
            if rejected:
                logger.info(
                    f"Research: local fallback interpretation rejected {rejected} "
                    f"noise hypotheses"
                )
            insights = local_actions.get("insights", "")
            if insights:
                logger.info(f"Research: local interpretation — {insights[:300]}")
        except Exception as e:
            logger.debug(f"Local fallback interpretation failed: {e}")
        return

    remaining = CLAUDE_ESCALATION_COOLDOWN - (time.time() - self._last_claude_call)
    if remaining > 0:
        logger.debug(f"Interpret backtests: cooldown active ({remaining:.0f}s left), deferring to next cycle")
        return

    try:
        result = await escalate_with_ladder(
            prompt,
            task_type="deep_work",
            hermes_caller="deep_work",
        )
        self._last_claude_call = time.time()
        self._claude_escalations += 1

        if result.get("content") and not result.get("error"):
            content = result["content"]
            try:
                json_str = content
                if "```" in json_str:
                    parts = json_str.split("```")
                    for part in parts:
                        stripped = part.strip()
                        if stripped.startswith("json"):
                            stripped = stripped[4:].strip()
                        if stripped.startswith("{"):
                            json_str = stripped
                            break
                elif "{" in json_str:
                    start = json_str.index("{")
                    end = json_str.rindex("}") + 1
                    json_str = json_str[start:end]

                actions = json.loads(json_str)

                # Act: Reject noise hypotheses
                rejected = 0
                for hid in actions.get("reject", []):
                    try:
                        await self.hypothesis_manager.update_status(
                            hid, "rejected", "claude_interpret_backtests"
                        )
                        rejected += 1
                        self._rejections += 1
                    except Exception as e:
                        logger.warning(f"Failed to reject hypothesis {hid}: {e}")
                if rejected:
                    logger.info(
                        f"Research: Claude interpretation rejected {rejected} "
                        f"noise hypotheses"
                    )

                # Act: Modify thresholds for promising hypotheses
                # GATE POLICY (mirrors tools/self_repair.py): an automated
                # actor may STRENGTHEN a gate but never WEAKEN it.
                #   - new_threshold >= current  → applied (gate tightened/unchanged)
                #   - new_threshold <  current  → recorded for human review, NOT applied
                #   - out-of-range values clamped to [MIN_EDGE_THRESHOLD_FLOOR,
                #     MAX_EDGE_THRESHOLD_CEILING] before comparison
                modified = 0
                refused = 0
                for mod in actions.get("modify", []):
                    try:
                        hid = mod.get("id")
                        new_thresh = mod.get("new_threshold")
                        reason = mod.get("reason", "claude_threshold_adjust")
                        if hid and new_thresh is not None:
                            new_thresh = max(MIN_EDGE_THRESHOLD_FLOOR,
                                             min(MAX_EDGE_THRESHOLD_CEILING,
                                                 float(new_thresh)))
                            cur = await db.execute(
                                "SELECT edge_threshold FROM hypotheses WHERE hypothesis_id = ?",
                                (hid,),
                            )
                            row = await cur.fetchone()
                            current = float(row[0]) if row and row[0] is not None else None
                            if current is None:
                                continue
                            if new_thresh < current:
                                refused += 1
                                logger.warning(
                                    "GATE POLICY REFUSED threshold LOWERING hyp=%s "
                                    "%s -> %s (reason=%s) — recorded for human review",
                                    hid, current, new_thresh, str(reason)[:120],
                                )
                                await db.execute(
                                    "UPDATE hypotheses SET "
                                    "notes = COALESCE(notes, '') || ? "
                                    "WHERE hypothesis_id = ?",
                                    (
                                        f"\n[cycle {self._cycles}] REFUSED threshold "
                                        f"lowering {current} -> {new_thresh}: {reason} "
                                        f"(gate policy; human decision required)",
                                        hid,
                                    ),
                                )
                                await db.commit()
                                continue
                            await db.execute(
                                "UPDATE hypotheses SET edge_threshold = ?, "
                                "notes = COALESCE(notes, '') || ? "
                                "WHERE hypothesis_id = ?",
                                (
                                    new_thresh,
                                    f"\n[cycle {self._cycles}] threshold raised "
                                    f"{current} -> {new_thresh}: {reason}",
                                    hid,
                                ),
                            )
                            await db.commit()
                            modified += 1
                    except Exception as e:
                        logger.warning(f"Failed to modify threshold for hypothesis {mod.get('id', '?')}: {e}")
                if modified:
                    logger.info(
                        f"Research: Claude raised thresholds on {modified} hypotheses"
                    )
                if refused:
                    logger.info(
                        f"Research: {refused} threshold-lowering suggestions refused by "
                        f"gate policy and logged to hypothesis notes for human review"
                    )

                # Log insights
                insights = actions.get("insights", "")
                if insights:
                    logger.info(f"Research: Claude backtest insights — {insights[:300]}")

                # ── Wiki write-back: file backtest stats as lessons ──
                # (feat/wiki-in-the-loop 2026-04-22) — replaces the prior
                # "read memory/error_patterns.md only" pattern. For each
                # hypothesis with sufficient data: file success article if
                # significant, null-result article if n>=30 and not
                # significant. Future hypothesis gen retrieves these.
                if _wiki_in_loop_enabled():
                    try:
                        from tools.knowledge_wiki import get_wiki
                        wiki = get_wiki()
                        for entry in hypo_data:
                            try:
                                hid = entry.get("id")
                                n = int(entry.get("events", 0) or 0)
                                is_sig = bool(entry.get("significant"))
                                if is_sig and n >= 15:
                                    topic = f"{hid}_backtest_success"
                                    title = f"Backtest success: {entry.get('name', hid)}"
                                    content = (
                                        f"Hypothesis {entry.get('name', hid)} "
                                        f"({hid}) shows statistically significant "
                                        f"edge in backtest.\n\n"
                                        f"Stats: n={n}, wins={entry.get('wins')}, "
                                        f"losses={entry.get('losses')}, "
                                        f"hit_rate={entry.get('hit_rate')}, "
                                        f"avg_edge={entry.get('avg_edge')}, "
                                        f"avg_ev={entry.get('avg_ev')}, "
                                        f"p_value={entry.get('p_value')}, "
                                        f"z_score={entry.get('z_score')}.\n"
                                        f"Sport: {entry.get('sport')}, "
                                        f"Market: {entry.get('market')}.\n"
                                        f"Thesis: {entry.get('thesis')}"
                                    )
                                    await wiki.write_lesson_article(
                                        db, topic=topic, title=title,
                                        content=content, domain="SIGNAL",
                                        related_topics=[
                                            "backtest_success",
                                            f"sport:{entry.get('sport')}",
                                            f"market:{entry.get('market')}",
                                        ],
                                        confidence=0.75,
                                    )
                                elif (not is_sig) and n >= 30:
                                    topic = f"{hid}_backtest_null_result"
                                    title = f"Backtest null: {entry.get('name', hid)}"
                                    content = (
                                        f"Hypothesis {entry.get('name', hid)} "
                                        f"({hid}) produced no significant edge "
                                        f"after {n} events — treat as dead.\n\n"
                                        f"Stats: wins={entry.get('wins')}, "
                                        f"losses={entry.get('losses')}, "
                                        f"hit_rate={entry.get('hit_rate')}, "
                                        f"avg_edge={entry.get('avg_edge')}, "
                                        f"avg_ev={entry.get('avg_ev')}, "
                                        f"p_value={entry.get('p_value')}.\n"
                                        f"Sport: {entry.get('sport')}, "
                                        f"Market: {entry.get('market')}.\n"
                                        f"Thesis: {entry.get('thesis')}\n\n"
                                        f"Do not re-propose structurally identical "
                                        f"variants — this pattern has been tested."
                                    )
                                    await wiki.write_lesson_article(
                                        db, topic=topic, title=title,
                                        content=content, domain="SIGNAL",
                                        related_topics=[
                                            "backtest_null_result",
                                            "dead_pattern",
                                            f"sport:{entry.get('sport')}",
                                            f"market:{entry.get('market')}",
                                        ],
                                        confidence=0.65,
                                    )
                            except Exception as e:
                                logger.debug(
                                    f"Wiki write-back skipped for "
                                    f"{entry.get('id')}: {e}"
                                )
                    except Exception as e:
                        logger.warning(f"Backtest wiki write-back failed: {e}")

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Claude interpretation response not valid JSON: {e}")

        elif result.get("rate_limited"):
            logger.info("Research: Claude rate-limited during backtest interpretation")
    except Exception as e:
        logger.warning(f"Claude backtest interpretation failed: {e}")


async def phase_paper_trade(loop) -> None:
    self = loop
    """Generate paper trade signals for promoted hypotheses.

    Uses DK scraper (free) as primary source for the target book's
    current lines, with Odds API as enrichment for cross-book data.
    This saves API credits while keeping paper trades accurate.
    """
    from datetime import datetime, timezone

    paper = await self.hypothesis_manager.list_hypotheses(status="paper_trading")

    if not paper:
        return

    # ── Auto-reject anti-predictive paper_trading hypotheses ──
    # IC < -0.10 means the model is inversely correlated with outcomes.
    # Don't waste paper trading cycles on these.
    # BUT: at n<20 IC is statistically meaningless (variance ~1/sqrt(n-3)),
    # so waive the gate for small samples — same logic as promotion gate.
    clean_paper = []
    for h in paper:
        try:
            db = self.data_collector._db
            # AUDIT FIX 2026-04-21 (autonomous.py:5820 stale read):
            # Previously fetched the FIRST row for a hypothesis with no
            # stage filter and no ORDER BY — non-deterministic, sometimes
            # returning stale backtest stats even when fresh paper_trade
            # stats existed. Pin to latest paper_trade row.
            cursor = await db.execute(
                "SELECT information_coefficient, signals_n "
                "FROM hypothesis_stats "
                "WHERE hypothesis_id = ? AND stage = 'paper_trade' "
                "ORDER BY computed_at DESC LIMIT 1",
                (h["hypothesis_id"],),
            )
            row = await cursor.fetchone()
            if not row:
                # No paper_trade stats yet — fall back to backtest stats so
                # anti-predictive gate still has a signal to work with.
                cursor = await db.execute(
                    "SELECT information_coefficient, signals_n "
                    "FROM hypothesis_stats "
                    "WHERE hypothesis_id = ? AND stage = 'backtest' "
                    "ORDER BY computed_at DESC LIMIT 1",
                    (h["hypothesis_id"],),
                )
                row = await cursor.fetchone()
            ic = row[0] if row else None
            n_signals = row[1] if row else 0
        except Exception:
            ic = None
            n_signals = 0
        if ic is not None and ic < -0.10 and n_signals >= 20:
            logger.warning(
                f"Paper trade: rejecting {h['name']} (IC={ic:.3f}, n={n_signals}, anti-predictive)"
            )
            await self.hypothesis_manager.update_status(
                h["hypothesis_id"], "rejected",
                f"auto:anti_predictive_paper_trading — IC={ic:.3f} < -0.10 (n={n_signals})",
                expected_status=h.get("status", "paper_trading"),
            )
            self._rejections += 1
        elif ic is not None and ic < -0.10 and n_signals < 20:
            logger.info(
                f"Paper trade: waiving anti-predictive gate for {h['name']} "
                f"(IC={ic:.3f}, n={n_signals}<20, statistically unreliable)"
            )
            clean_paper.append(h)
        else:
            clean_paper.append(h)
    paper = clean_paper

    if not paper:
        return

    logger.info(f"Research: paper trading {len(paper)} hypotheses")

    # Cache live odds per sport to avoid redundant API calls
    odds_cache: dict[str, dict] = {}

    for h in paper:
        if not self._running:
            break

        try:
            sport = h["sport"]
            market = h.get("market_type", "")

            # For player props: use Odds API prop scanner (DK scraper has no props)
            if market.startswith("player_"):
                from tools.prop_scanner import scan_props_ev
                from tools.odds_api_io import get_odds
                import uuid as _uuid
                # Get upcoming games for this sport
                if sport not in odds_cache:
                    live_odds = await get_odds(
                        sport=sport, regions="us", markets="h2h",
                    )
                    if live_odds.get("error"):
                        logger.warning(
                            f"Paper trade: Odds API failed for {sport} props: "
                            f"{live_odds.get('error')} — skipping prop hypotheses"
                        )
                    elif not live_odds.get("games"):
                        logger.warning(
                            f"Paper trade: Odds API returned 0 games for {sport}"
                        )
                    else:
                        odds_cache[sport] = live_odds
                games = odds_cache.get(sport, {}).get("games", [])
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                now_iso = datetime.now(timezone.utc).isoformat()
                for game in games[:3]:  # Limit to 3 games to conserve credits
                    event_id = game.get("id")
                    if not event_id:
                        continue
                    try:
                        result = await scan_props_ev(
                            sport=sport,
                            event_id=event_id,
                            target_book="draftkings",
                            edge_threshold=h["edge_threshold"],
                            prop_markets=market,
                        )
                        edges = result.get("edges", [])
                        if edges:
                            logger.info(
                                f"Research: {len(edges)} prop edges for "
                                f"{h['hypothesis_id']} in game {event_id}"
                            )
                            # Record each edge as a paper trade
                            db = self.data_collector._db
                            for edge_info in edges:
                                trade_id = str(_uuid.uuid4())[:12]
                                await db.execute(
                                    "INSERT OR IGNORE INTO paper_trades "
                                    "(trade_id, hypothesis_id, event_id, sport, player, market, "
                                    "line, side, book, signal_time, signal_odds_american, "
                                    "signal_implied_prob, model_fair_prob, edge, ev_pct, "
                                    "kelly_fraction, game_date) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (
                                        trade_id,
                                        h["hypothesis_id"],
                                        event_id,
                                        sport,
                                        edge_info.get("player"),
                                        market,
                                        edge_info.get("line"),
                                        edge_info.get("side", ""),
                                        "draftkings",
                                        now_iso,
                                        edge_info.get("target_price", 0),
                                        edge_info.get("target_implied", 0),
                                        edge_info.get("fair_probability", 0),
                                        round(edge_info.get("edge_pct", 0) / 100, 6),
                                        round(edge_info.get("ev_per_100", 0) / 100, 6),
                                        edge_info.get("kelly_fraction", 0),
                                        today,
                                    ),
                                )
                                # Also insert into signals table
                                edge_val = round(edge_info.get("edge_pct", 0) / 100, 6)
                                sig_confidence = _signal_confidence(edge_val)
                                await db.execute(
                                    "INSERT INTO signals "
                                    "(event_id, sport, signal_type, team, market, book, "
                                    "odds_american, fair_probability, fair_prob_source, "
                                    "edge_pct, ev_pct, confidence, kelly_fraction, "
                                    "recommended_stake, status, notes) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (
                                        event_id,
                                        sport,
                                        "paper_trade",
                                        edge_info.get("side", ""),
                                        market,
                                        "draftkings",
                                        edge_info.get("target_price", 0),
                                        edge_info.get("fair_probability", 0),
                                        "cross_book_devig",
                                        edge_val,
                                        round(edge_info.get("ev_per_100", 0) / 100, 6),
                                        sig_confidence,
                                        edge_info.get("kelly_fraction", 0),
                                        None,
                                        "paper",
                                        f"hypothesis_id={h['hypothesis_id']}, trade_id={trade_id}",
                                    ),
                                )
                            await db.commit()
                    except Exception as e:
                        logger.warning(f"Prop scan failed for {event_id}: {e}", exc_info=True)
                continue

            # For game-level markets: line_monitor cache (instant, free) first,
            # then DK scraper (free but slow), then Odds API (costs credits)
            if sport not in odds_cache:
                live_odds = {}

                # Try line_monitor cache first — instant, no network call
                if self.line_monitor:
                    snap = self.line_monitor._snapshots.get(sport, {})
                    if snap and not snap.get("error") and snap.get("games"):
                        live_odds = snap
                        logger.info(
                            f"Paper trade: using line_monitor cache for {sport} "
                            f"({len(snap.get('games', []))} games)"
                        )

                # Fallback: DK scraper (free but slow — was causing 120s timeouts)
                if not live_odds.get("games"):
                    from tools.dk_scraper import scrape_dk_odds
                    live_odds = await scrape_dk_odds(sport)

                # DK scraper returns only 1 book (draftkings). Paper trading
                # needs multi-book data for devigging to compute fair probs.
                # Check if we have sufficient books, otherwise fall through.
                _needs_multibook = True
                if live_odds.get("games") and not live_odds.get("error"):
                    _sample_books = len(live_odds["games"][0].get("bookmakers", []))
                    if _sample_books < 2:
                        logger.info(
                            f"Paper trade: {sport} has only {_sample_books} book(s) "
                            f"(need ≥2 for devig) — enriching with Odds API"
                        )
                        _needs_multibook = True
                    else:
                        _needs_multibook = False

                # Odds API: needed when no games OR single-book data
                if live_odds.get("error") or not live_odds.get("games") or _needs_multibook:
                    from tools.odds_api_io import get_odds
                    _fallback_odds = live_odds
                    live_odds = await get_odds(
                        sport=sport,
                        regions="us",
                        markets="h2h,spreads,totals",
                    )
                    # If Odds API failed but we had line_monitor data, keep it
                    if (live_odds.get("error") or not live_odds.get("games")) and _fallback_odds.get("games"):
                        live_odds = _fallback_odds
                        logger.info(
                            f"Paper trade: Odds API failed for {sport}, "
                            f"using line_monitor data ({len(_fallback_odds.get('games', []))} games, single-book)"
                        )

                if live_odds.get("error") or not live_odds.get("games"):
                    logger.warning(
                        f"Paper trade: no odds available for {sport} — "
                        f"line_monitor, DK scraper, and Odds API all failed"
                    )
                else:
                    odds_cache[sport] = live_odds

            live_odds = odds_cache.get(sport)
            if not live_odds:
                continue

            signals = await self.backtest_engine.generate_paper_trade_signal(
                hypothesis_id=h["hypothesis_id"],
                live_odds=live_odds,
            )

            if signals:
                logger.info(
                    f"Research: {len(signals)} paper trade signals for "
                    f"hypothesis {h['hypothesis_id']}"
                )
        except Exception as e:
            logger.warning(
                f"Paper trading failed for {h['hypothesis_id']}: {e}"
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
