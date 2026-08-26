"""
Curated thesis-space seed library for Callisto's hypothesis generator.

The autonomous generator was producing junk at a 91% rejection rate — largely
because it kept re-discovering the same shallow theses ("rest advantage",
"back-to-back unders", "weather totals") across every cycle. This library
seeds ~50 deliberately *underexplored* thesis spaces, each expressed as a
template that parameterizes over live DB data rather than inventing facts.

Seed schema
-----------
Each seed is a dict:

    {
      "seed_id":            str   # globally unique (e.g. "mlb_umpire_zone_totals")
      "category":           str   # market family: props | totals | spreads | h2h | live | parlay
      "sport":              str   # canonical sport key (baseball_mlb, etc.)
      "market_type":        str   # line-level market (totals, player_strikeouts, ...)
      "thesis_template":    str   # human-readable hypothesis statement, may include {vars}
      "cohort_filter_sql":  str   # SQL WHERE-fragment over game_contexts/player_stats that
                                  # defines which events belong to the cohort. Must be
                                  # specific enough to produce a testable subset.
      "signal_logic":       str   # brief description of the expected market signal
      "min_sample_heuristic": int # rough minimum cohort size needed for an IC estimate
      "ic_prior_estimate":  float # weakly-informed prior on edge magnitude (absolute,
                                  # units of probability, e.g. 0.03 = 3 pp)
      "variance_justification": str  # why this edge is *not* a duplicate of others
      "exploration_status": str   # "unexplored" | "partial" | "exhausted" (runtime updated)
    }

A seed produces at most ONE hypothesis per invocation — the generator will
use a seed as a scaffold, then ask the LLM to specialize it into a concrete
testable hypothesis (concrete umpire, concrete park, concrete lineup
configuration) using DB-observed facts.

The seeds deliberately cover axes the existing template library ignores:
  - Official/referee-specific effects (MLB umpires, NBA refs, NHL officials)
  - Micro-schedule effects (bullpen handoff innings, travel+altitude combos)
  - Identity / cohesion factors in thin women's markets
  - Live / in-game markets (overreaction after leverage swings)
  - Parlay correlation structure (stacks the book doesn't model)
  - Prop/derivative markets booked with sparse data

Layout
------
The seed dicts themselves live in ``tools/thesis/``, grouped by sport:

    tools/thesis/mlb.py    — MLB_SEEDS
    tools/thesis/nba.py    — NBA_SEEDS (NBA + WNBA)
    tools/thesis/nhl.py    — NHL_SEEDS
    tools/thesis/nfl.py    — NFL_SEEDS
    tools/thesis/misc.py   — MISC_SEEDS (NCAAB/NCAAW, golf, soccer, parlays,
                             market microstructure, boosts)

Schema constants and validation live in ``tools/thesis/_schema.py``; the
runtime query helpers live in ``tools/thesis/runtime.py``. This module is a
facade that re-exports the public seed API so existing importers are
unaffected.
"""

from __future__ import annotations

from tools.thesis import (
    MISC_SEEDS,
    MLB_SEEDS,
    NBA_SEEDS,
    NFL_SEEDS,
    NHL_SEEDS,
    REQUIRED_SEED_KEYS,
    THESIS_SEEDS,
    VALID_CATEGORIES,
    VALID_EXPLORATION,
    get_seed,
    list_seeds,
    pick_unexplored_seeds,
    seed_category_coverage,
    seed_sport_coverage,
    validate_seed,
)

__all__ = [
    "THESIS_SEEDS",
    "MLB_SEEDS",
    "NBA_SEEDS",
    "NHL_SEEDS",
    "NFL_SEEDS",
    "MISC_SEEDS",
    "REQUIRED_SEED_KEYS",
    "VALID_CATEGORIES",
    "VALID_EXPLORATION",
    "validate_seed",
    "list_seeds",
    "get_seed",
    "seed_category_coverage",
    "seed_sport_coverage",
    "pick_unexplored_seeds",
]
