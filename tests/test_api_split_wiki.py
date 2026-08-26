"""Source-contract tests for the api.py -> tools.api split.

Pins that:
  * api.py still owns the FastAPI decorators with require_admin_or_loopback
    for every wiki/analysis/odds-extra route moved to tools/api/.
  * The moved handler logic (unique docstrings/strings) now lives in the
    tools/api modules, not in api.py.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API_SOURCE = (REPO / "api.py").read_text()
WIKI_SOURCE = (REPO / "tools" / "api" / "wiki.py").read_text()
ANALYSIS_SOURCE = (REPO / "tools" / "api" / "analysis.py").read_text()
ODDS_EXTRA_SOURCE = (REPO / "tools" / "api" / "odds_extra.py").read_text()

ROUTES = [
    "/wiki/stats",
    "/wiki/articles",
    "/wiki/article/{topic}",
    "/wiki/search",
    "/wiki/contradictions",
    "/odds/psychology/{sport}",
    "/odds/psychology",
    "/odds/dead-numbers/{sport}",
    "/analysis/futures-efficiency",
    "/analysis/half-market/{sport}",
    "/analysis/cross-tabulate/{sport}",
    "/odds/line-analysis/{sport}",
]


def test_decorators_still_in_api_py_with_gating():
    for route in ROUTES:
        marker = f'@app.get("{route}"'
        idx = API_SOURCE.find(marker)
        assert idx != -1, f"decorator for {route} missing from api.py"
        window = API_SOURCE[idx : idx + 200]
        assert 'dependencies=[Depends(require_admin_or_loopback)]' in window, (
            f"{route} decorator lost require_admin_or_loopback"
        )


def test_wiki_logic_lives_in_tools_api_wiki():
    unique_strings = [
        "await wiki.get_stats(db)",
        "get_contradictions(db, unresolved_only=unresolved_only)",
        'f"Article \'{topic}\' not found"',
        "await wiki.list_articles(db, domain=domain, limit=limit)",
        "await wiki.search(db, q, limit=limit)",
    ]
    for s in unique_strings:
        assert s in WIKI_SOURCE, f"expected {s!r} in tools/api/wiki.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_analysis_logic_lives_in_tools_api_analysis():
    unique_strings = [
        "from tools.market_psychology import futures_efficiency",
        "half_market_adjustment(",
        "load_game_results(db_path, sport=sport)",
        "cross_tabulate(df, min_sample=min_sample).to_dicts()",
    ]
    for s in unique_strings:
        assert s in ANALYSIS_SOURCE, f"expected {s!r} in tools/api/analysis.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_odds_extra_logic_lives_in_tools_api_odds_extra():
    unique_strings = [
        "full_market_psychology(",
        "find_dead_number_steals(lines_for_dn, sport)",
        "rank_line_shopping_opportunities(lines_for_dn, sport)",
        "estimate_public_side as la_estimate_public",
        "contrarian_value as la_contrarian",
        "optimal_bet_timing as la_timing",
        "detect_steam as la_detect_steam",
        "contrarian_picks.sort(key=lambda x: x.get(\"adjusted_roi\", 0), reverse=True)",
        "all_steals.sort(key=lambda x: x.get(\"prob_difference\", 0), reverse=True)",
    ]
    for s in unique_strings:
        assert s in ODDS_EXTRA_SOURCE, f"expected {s!r} in tools/api/odds_extra.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_api_py_delegates_to_modules():
    assert "_wiki.wiki_stats()" in API_SOURCE
    assert "_analysis.cross_tabulate_endpoint(sport" in API_SOURCE
    assert "_odds_extra.dead_numbers_endpoint(sport)" in API_SOURCE
