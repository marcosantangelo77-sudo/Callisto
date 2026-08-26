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
