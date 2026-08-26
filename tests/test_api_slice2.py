"""Source-contract + behavior tests for the slice-2 api.py split.

Pins that:
  * api.py still owns the FastAPI decorators with the original gating
    (require_admin / require_admin_or_loopback) for every odds dump,
    bets/bankroll, and simulate route moved to tools/api/.
  * The moved handler logic (unique docstrings/strings) now lives in
    tools/api/odds_routes.py, tools/api/bets.py, and tools/api/simulate.py,
    not in api.py.
  * /health, /health/livez, /health/readyz stay public (no admin dep).
  * The executor-enable seal is untouched: no "live" appears in
    _PAPER_TRADE_SIGNAL_STATUSES semantics via these routes.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import re
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(REPO, "api.py")) as _f:
    API_SOURCE = _f.read()


def _read(relpath: str) -> str:
    with open(os.path.join(REPO, relpath)) as f:
        return f.read()


ODDS_ROUTES_SOURCE = _read(os.path.join("tools", "api", "odds_routes.py"))
BETS_SOURCE = _read(os.path.join("tools", "api", "bets.py"))
SIMULATE_SOURCE = _read(os.path.join("tools", "api", "simulate.py"))


def _load_api_module():
    if "api" in sys.modules:
        return sys.modules["api"]
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    return importlib.import_module("api")


try:
    api_mod = _load_api_module()
except Exception as _import_err:
    api_mod = None
    _import_err_msg = str(_import_err)
else:
    _import_err_msg = ""


# ---------------------------------------------------------------------------
# Route inventory: every route that was moved in this slice.
# ---------------------------------------------------------------------------

ADMIN_OR_LOOPBACK_GETS = [
    "/odds/movements",
    "/odds/opportunities",
    "/odds/snapshots/{sport}",
    "/odds/edges",
    "/odds/narrative-edges",
    "/odds/kl-metrics",
    "/odds/sgp-analysis/{sport}",
    "/odds/props/{sport}/{event_id}",
    "/odds/dk-props/{sport}",
    "/odds/status",
    "/odds/learned-correlations",
    "/odds/market-analysis/{sport}",
    "/odds/stale-lines/{sport}",
    "/odds/psychology/{sport}",
    "/odds/psychology",
    "/odds/dead-numbers/{sport}",
    "/odds/line-gaps/{sport}",
    "/odds/prop-gaps/{sport}",
    "/bets/clv-report",
    "/bets/clv-forecast",
    "/simulate/portfolio",
]

ADMIN_OR_LOOPBACK_POSTS = [
    "/odds/snapshot/{sport}",
    "/odds/parlay-scan/{sport}",
    "/simulate/basketball",
    "/simulate/poisson",
]

ADMIN_ONLY = [
    ("/bets/record", "post"),
    ("/bets/{bet_id}/resolve", "post"),
    ("/bets/bankroll/init", "post"),
]


def _decorator_block(path: str, method: str) -> str:
    m = re.search(
        rf'@app\.{method}\(\s*"{re.escape(path)}"[^\n]*', API_SOURCE
    )
    assert m is not None, f"{method.upper()} {path} decorator missing from api.py"
    return m.group(0)


@pytest.mark.parametrize("path", ADMIN_OR_LOOPBACK_GETS)
def test_get_routes_keep_loopback_or_admin_gating(path):
    deco = _decorator_block(path, "get")
    assert "dependencies=[Depends(require_admin_or_loopback)]" in deco, (
        f"GET {path} lost require_admin_or_loopback"
    )


@pytest.mark.parametrize("path", ADMIN_OR_LOOPBACK_POSTS)
def test_post_routes_keep_loopback_or_admin_gating(path):
    deco = _decorator_block(path, "post")
    assert "dependencies=[Depends(require_admin_or_loopback)]" in deco, (
        f"POST {path} lost require_admin_or_loopback"
    )


@pytest.mark.parametrize(("path", "method"), ADMIN_ONLY)
def test_write_routes_require_full_admin(path, method):
    """Money-mutating bet routes must keep the stricter require_admin gate."""
    deco = _decorator_block(path, method)
    assert "Depends(require_admin)" in deco, f"{method.upper()} {path} lost require_admin"
    # And must NOT have been downgraded to loopback-allowing.
    assert "require_admin_or_loopback" not in deco


def test_edges_live_gated_via_signature_auth_param():
    """/edges/live gates via an _auth signature param — pin it stays."""
    m = re.search(r'@app\.get\("/edges/live"\).*?def get_live_edges\(.*?\):', API_SOURCE, re.DOTALL)
    assert m is not None, "/edges/live route missing from api.py"
    assert "require_admin_or_loopback" in m.group(0)


@pytest.mark.parametrize(
    ("path", "func"),
    [
        ("/health", "health_check"),
        ("/health/livez", "livez"),
        ("/health/readyz", "readyz"),
    ],
)
def test_health_routes_stay_public(path, func):
    """The liveness/readiness surface must never gain an admin dependency."""
    i = API_SOURCE.find(f'@app.get("{path}"')
    assert i != -1, f"{path} missing from api.py"
    window = API_SOURCE[i : API_SOURCE.find("\n@", i)]
    assert "require_admin" not in window, f"{path} must stay public"


# ---------------------------------------------------------------------------
# Logic lives in tools.api modules, not api.py.
# ---------------------------------------------------------------------------

def test_odds_logic_lives_in_tools_api_odds_routes():
    unique_strings = [
        "await line_monitor.get_recent_movements(sport=sport, limit=limit)",
        "SELECT MAX(computed_at) FROM live_edge_surface",
        "FROM kl_metrics ORDER BY computed_at DESC LIMIT ?",
        "find_correlated_parlay_edges(game, alt_data)",
        "detect_anti_correlation(available_props[:15], sport)",
        "scrape_dk_odds(sport)",
        '"Learned correlation store not initialized"',
        "full_market_analysis(odds_data.get(\"games\", []), sport)",
        'scan_line_gaps(alt_data.get("bookmakers", []), market_key=market)',
        'scan_prop_gaps(prop_data)',
    ]
    for s in unique_strings:
        assert s in ODDS_ROUTES_SOURCE, f"expected {s!r} in tools/api/odds_routes.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_bets_logic_lives_in_tools_api_bets():
    unique_strings = [
        "await clv_tracker.record_bet(",
        "await clv_tracker.resolve_bet(bet_id, resolution.result, resolution.payout)",
        "balance out of range (0..100M)",
        "await clv_tracker.forecast_clv(sport=sport)",
        'result: str = Field(..., pattern="^(won|lost|push)$")',
    ]
    for s in unique_strings:
        assert s in BETS_SOURCE, f"expected {s!r} in tools/api/bets.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_simulate_logic_lives_in_tools_api_simulate():
    unique_strings = [
        "from tools.simulation import simulate_basketball, compare_to_market, TeamProfile",
        "return simulate_poisson(req.home_expected, req.away_expected)",
        "_PORTFOLIO_SIM_CACHE_MAX_ENTRIES = 32",
        "_PORTFOLIO_SIM_CACHE_TTL = 3600  # 1 hour",
        "SELECT hypothesis_id FROM hypotheses WHERE status = 'live'",
        "No hypothesis_ids supplied (pass hypothesis_ids=a,b,c or all_live=1)",
    ]
    for s in unique_strings:
        assert s in SIMULATE_SOURCE, f"expected {s!r} in tools/api/simulate.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_api_py_delegates_to_new_modules():
    for marker in [
        "_odds_routes.get_movements(sport=sport, limit=limit)",
        "_odds_routes.parlay_scan(sport)",
        "_bets.record_bet(bet)",
        "_bets.init_bankroll(balance)",
        "_simulate.simulate_basketball_game(req)",
        "_simulate.simulate_poisson_game(req)",
    ]:
        assert marker in API_SOURCE, f"api.py should delegate via {marker}"


def test_moved_models_importable_from_both_places():
    """Pydantic request models are re-exported so OpenAPI names don't shift."""
    from tools.api.bets import BetResolution, BetSubmission
    from tools.api.simulate import PoissonRequest, SimulationRequest

    assert BetSubmission is api_mod._bets.BetSubmission
    assert BetResolution is api_mod._bets.BetResolution
    assert SimulationRequest is api_mod._simulate.SimulationRequest
    assert PoissonRequest is api_mod._simulate.PoissonRequest


def test_portfolio_cache_helpers_reexported_on_api():
    """Existing tests/operators poke these on the api module directly."""
    for name in [
        "_fetch_live_hypothesis_ids",
        "_get_portfolio_sim_cache",
        "_store_portfolio_sim_cache",
        "_PORTFOLIO_SIM_CACHE",
        "_PORTFOLIO_SIM_CACHE_MAX_ENTRIES",
        "_PORTFOLIO_SIM_CACHE_TTL",
    ]:
        assert hasattr(api_mod, name), f"api module lost backward-compat attr {name}"
    assert inspect.iscoroutinefunction(api_mod._fetch_live_hypothesis_ids)


# ---------------------------------------------------------------------------
# Behavioral checks on moved helpers (called directly; lifespan not entered).
# ---------------------------------------------------------------------------

@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestSimulateModuleBehavior:
    def setup_method(self):
        api_mod._PORTFOLIO_SIM_CACHE.clear()

    def test_normalize_params_clamps(self):
        lo_n, lo_h = api_mod._simulate.normalize_portfolio_params(1, -5)
        hi_n, hi_h = api_mod._simulate.normalize_portfolio_params(999_999, 99_999)
        assert (lo_n, lo_h) == (10, 1)
        assert (hi_n, hi_h) == (5000, 365)

    def test_resolve_ids_splits_csv_and_dedupes_whitespace(self):
        ids = asyncio.run(
            api_mod._simulate.resolve_portfolio_ids(hypothesis_ids=" a , b ,,c ", all_live=False)
        )
        assert ids == ["a", "b", "c"]

    def test_resolve_ids_empty_raises_http_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(api_mod._simulate.resolve_portfolio_ids(hypothesis_ids="", all_live=False))
        assert excinfo.value.status_code == 400

    def test_resolve_ids_all_live_reads_sqlite(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "h.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE hypotheses (hypothesis_id TEXT PRIMARY KEY, status TEXT)"
        )
        conn.executemany(
            "INSERT INTO hypotheses VALUES (?, ?)",
            [("H1", "live"), ("H2", "paper"), ("H3", "live")],
        )
        conn.commit()
        conn.close()

        orig = os.environ.get("CALLISTO_DB_PATH")
        try:
            os.environ["CALLISTO_DB_PATH"] = db_path
            ids = asyncio.run(api_mod._simulate.resolve_portfolio_ids(all_live=True))
            assert sorted(ids) == ["H1", "H3"]
        finally:
            if orig is None:
                os.environ.pop("CALLISTO_DB_PATH", None)
            else:
                os.environ["CALLISTO_DB_PATH"] = orig

    def test_build_cache_key_order_insensitive_on_ids(self):
        k1 = api_mod._simulate.build_portfolio_cache_key(["a", "b"], 10, 30, 100.0, 0.25)
        k2 = api_mod._simulate.build_portfolio_cache_key(["b", "a"], 10, 30, 100.0, 0.25)
        assert k1 == k2
        k3 = api_mod._simulate.build_portfolio_cache_key(["a", "b"], 11, 30, 100.0, 0.25)
        assert k3 != k1

    def test_portfolio_endpoint_runs_sims_off_event_loop_thread(self, monkeypatch):
        calls = {}

        class _FakeResult:
            def to_dict(self, include_paths=False):
                return {"ok": True}

        main_thread_id = threading.get_ident()

        def fake_simulate(**kwargs):
            calls["thread_id"] = threading.get_ident()
            time.sleep(0.05)
            return _FakeResult()

        monkeypatch.setattr(
            "tools.bankroll_sim.simulate_portfolio", fake_simulate, raising=False
        )
        resp = asyncio.run(
            api_mod.simulate_portfolio_endpoint(
                hypothesis_ids="x,y", n_sims=10, horizon_days=1,
                starting_bankroll=1000.0, kelly_fraction=0.25, all_live=False,
            )
        )
        assert resp.get("cached") is False and resp.get("ok") is True
        assert calls["thread_id"] != main_thread_id

        # Identical input signature hits the LRU cache.
        resp2 = asyncio.run(
            api_mod.simulate_portfolio_endpoint(
                hypothesis_ids="y,x", n_sims=10, horizon_days=1,
                starting_bankroll=1000.0, kelly_fraction=0.25, all_live=False,
            )
        )
        assert resp2.get("cached") is True

    def test_portfolio_cache_is_bounded_at_32(self):
        now = time.time()
        for i in range(40):
            api_mod._store_portfolio_sim_cache((f"k{i}",), (now, {"i": i}))
            assert len(api_mod._PORTFOLIO_SIM_CACHE) <= 32
        assert len(api_mod._PORTFOLIO_SIM_CACHE) == 32


# ---------------------------------------------------------------------------
# Bets module validation behavior.
# ---------------------------------------------------------------------------

class TestBetsModels:
    def test_bet_submission_rejects_out_of_range_odds(self):
        import pydantic
        from tools.api.bets import BetSubmission

        with pytest.raises(pydantic.ValidationError):
            BetSubmission(
                sport="basketball_nba",
                game_description="LAL @ BOS",
                team="LAL",
                market="h2h",
                bookmaker="draftkings",
                placement_odds=999_999,
            )

    def test_bet_resolution_rejects_bad_result(self):
        import pydantic
        from tools.api.bets import BetResolution

        with pytest.raises(pydantic.ValidationError):
            BetResolution(result="refunded")

    def test_init_bankroll_range_guard(self):
        from fastapi import HTTPException
        from tools.api import bets as bets_mod

        class _FakeTracker:
            def __init__(self):
                self.balances = []

            async def set_initial_bankroll(self, balance):
                self.balances.append(balance)

        fake = _FakeTracker()
        orig = api_mod.clv_tracker
        api_mod.clv_tracker = fake
        try:
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(bets_mod.init_bankroll(-1))
            assert excinfo.value.status_code == 422
            with pytest.raises(HTTPException):
                asyncio.run(bets_mod.init_bankroll(200_000_000))
            out = asyncio.run(bets_mod.init_bankroll(5_000))
            assert out == {"balance": 5_000}
            assert fake.balances == [5_000]
        finally:
            api_mod.clv_tracker = orig


# ---------------------------------------------------------------------------
# Safety pins: paper-trade/live semantics must be untouched by this slice.
# ---------------------------------------------------------------------------

def test_no_executor_enable_in_extracted_modules():
    """Extracted route modules must never arm the executor."""
    for src in (ODDS_ROUTES_SOURCE, BETS_SOURCE, SIMULATE_SOURCE):
        assert "executor.enable" not in src
        assert "enable_executor" not in src


def test_paper_trade_statuses_not_touched_by_slice_modules():
    """The moved modules must not reference or redefine signal statuses."""
    for src in (ODDS_ROUTES_SOURCE, BETS_SOURCE, SIMULATE_SOURCE):
        assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src
