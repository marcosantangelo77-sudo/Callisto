"""tools.btio — implementation package behind the tools.backtest_io facade.

Modules:
    registries    — context-factor registry helpers (canonical tables are
                    pinned in tools/backtest_io.py; this package delegates
                    to them via a lazy module __getattr__)
    filters       — hypothesis line-filter parsing and condition matching
    schedule      — DB-backed schedule-context computation
    context_match — game-vs-context matching and needs-context checks
    resolution    — bet resolution (resolve_line)
    teams         — team-name normalization and fuzzy matching

The canonical context-factor registries (UNFILTERABLE_CONTEXT_FACTORS,
FILTERABLE_CONTEXT_FACTORS, _CONTEXT_KEYWORD_MAP) live in tools.backtest_io
(characterization-pinned there by tests/test_backtest_split.py). Accessing
them through tools.btio resolves lazily to the same objects.
"""

_LAZY_PROXY_NAMES = frozenset(
    {
        "FILTERABLE_CONTEXT_FACTORS",
        "UNFILTERABLE_CONTEXT_FACTORS",
        "_CONTEXT_KEYWORD_MAP",
    }
)


def __getattr__(name):
    # PEP 562: lazily proxy the canonical registry tables so that
    # tools.btio.<table> is the *same object* as tools.backtest_io.<table>,
    # without creating an import cycle at package-init time.
    if name in _LAZY_PROXY_NAMES:
        from tools import backtest_io

        return getattr(backtest_io, name)
    raise AttributeError(f"module 'tools.btio' has no attribute {name!r}")


from tools.btio.context_match import (
    _game_matches_context_filter,
    _needs_context_filter,
)
from tools.btio.filters import (
    _infer_context_needs,
    _log_unfilterable_context_factors,
    _parse_hypothesis_filters,
    compute_context_coverage,
    has_structured_filters,
    matches_hypothesis_conditions,
)
from tools.btio.resolution import resolve_line
from tools.btio.schedule import build_schedule_context
from tools.btio.teams import (
    _TEAM_ALIASES,
    _build_alias_map,
    _normalize_team,
    _team_matches,
)

__all__ = [
    "FILTERABLE_CONTEXT_FACTORS",
    "UNFILTERABLE_CONTEXT_FACTORS",
    "_CONTEXT_KEYWORD_MAP",
    "_TEAM_ALIASES",
    "_build_alias_map",
    "_game_matches_context_filter",
    "_infer_context_needs",
    "_log_unfilterable_context_factors",
    "_needs_context_filter",
    "_normalize_team",
    "_parse_hypothesis_filters",
    "_team_matches",
    "build_schedule_context",
    "compute_context_coverage",
    "has_structured_filters",
    "matches_hypothesis_conditions",
    "resolve_line",
]
