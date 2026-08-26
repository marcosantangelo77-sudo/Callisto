"""Shared ResearchLoop cadence constants, regime cache, and wiki helpers.

Extracted from :mod:`tools.loop.phases_impl` so that facade can keep only
``phase_live_execute`` (CALLISTO_ALLOW_LIVE_EXECUTE gate) plus re-exports.

This module must never import :mod:`tools.autonomous` or
:mod:`tools.loop.phases_impl` — hypgen/post_live bind helpers from
``phases_impl as _impl`` after those names exist. Importing either would
cycle. Also must not import sibling ``phases.*`` modules that load
``phases_impl``.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

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
