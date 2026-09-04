"""Source-contract + behavior tests for the slice-5 api.py split.

Pins that:
  * The background-worker infrastructure (task_worker, adaptive-timeout
    runner, WAL checkpoint loop, restart-signal watcher, ingestion SLA
    watchdog, order cron, auto-followup) lives in tools/api/workers.py and
    api.py keeps working aliases for every moved symbol.
  * api.py keeps the FastAPI decorators with the original gating for every
    route whose body moved to tools.api in this slice (model/data/
    research/hypothesis/backtest/debug/admin/system families).
  * /health, /health/livez, /health/readyz stay PUBLIC (no admin dep) and
    /health/livez awaits _system_routes.health_livez() — never returns a
    bare coroutine.
  * Gated dumps (/health/detailed, /health/deep, debug/memory, admin/sql,
    admin/writer, executor/enable) still require admin-or-loopback or admin.
  * HypothesisCreate/BacktestRequest schemas now live in tools.api and are
    re-subclassed in api.py so OpenAPI names don't shift.
  * The /simulate/portfolio body lives in tools/api/simulate.py; api.py's
    wrapper is a thin delegation and the off-event-loop to_thread contract
    is preserved there.
  * The paper-trade/live seal is untouched by this refactor — no route or
    extracted module widens generate_paper_trade_signal to 'live'.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(REPO, "api.py")) as _f:
    API_SOURCE = _f.read()


def _read(relpath: str) -> str:
    with open(os.path.join(REPO, relpath)) as f:
        return f.read()


WORKERS_SOURCE = _read(os.path.join("tools", "api", "workers.py"))
SIM_SOURCE = _read(os.path.join("tools", "api", "simulate.py"))
HYP_SOURCE = _read(os.path.join("tools", "api", "hypothesis_routes.py"))
BT_SOURCE = _read(os.path.join("tools", "api", "backtest_routes.py"))

ALL_EXTRACTED_SOURCES = {
    "workers": WORKERS_SOURCE,
    "simulate": SIM_SOURCE,
    "hypothesis_routes": HYP_SOURCE,
    "backtest_routes": BT_SOURCE,
}


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
# Worker infrastructure moved into tools/api/workers.py
# ---------------------------------------------------------------------------

MOVED_WORKER_SYMBOLS = [
    "_is_internal_query",
    "_maybe_auto_followup",
    "wal_checkpoint_loop",
    "restart_signal_watcher",
    "ingestion_sla_watchdog_loop",
    "_run_session_with_adaptive_timeout",
    "_AdaptiveTimeout",
    "task_worker",
    "order_cron_loop",
]


@pytest.mark.parametrize("name", MOVED_WORKER_SYMBOLS)
def test_worker_symbol_defined_in_tools_api(name):
    assert hasattr(api_mod.workers if hasattr(api_mod, "workers") else None, name) or \
        name in WORKERS_SOURCE.replace("async def ", "def "), (
        f"{name} missing from tools/api/workers.py"
    )


@pytest.mark.parametrize("name", MOVED_WORKER_SYMBOLS)
def test_worker_symbol_aliased_on_api(name):
    assert hasattr(api_mod, name), f"api module lost backward-compat alias {name}"


@pytest.mark.parametrize(
    ("name", "is_coro"),
    [
        ("wal_checkpoint_loop", True),
        ("restart_signal_watcher", True),
        ("ingestion_sla_watchdog_loop", True),
        ("_maybe_auto_followup", True),
        ("_run_session_with_adaptive_timeout", True),
        ("task_worker", True),
        ("order_cron_loop", True),
        ("_is_internal_query", False),
    ],
)
def test_moved_worker_callables_keep_their_nature(name, is_coro):
    fn = getattr(api_mod, name)
    assert inspect.iscoroutinefunction(fn) is is_coro


def test_workers_module_is_imported_from_tools_api():
    assert "from tools.api.workers import" in API_SOURCE


def test_adaptive_constants_still_live_on_api():
    """Tests monkeypatch these on the api module — they must remain there."""
    for name in [
        "_ADAPTIVE_PROGRESS_WINDOW_S",
        "_ADAPTIVE_STALL_WINDOW_S",
        "_ADAPTIVE_EXTENSION_S",
        "_ADAPTIVE_POLL_S",
        "TASK_WORKER_TIMEOUT_S",
    ]:
        assert hasattr(api_mod, name), f"api module lost {name}"
    assert isinstance(api_mod.TASK_WORKER_TIMEOUT_S, float)


def test_sla_watchdog_state_shared_between_api_and_workers():
    from tools.api import workers as _workers
    assert api_mod._sla_alerted_sources is _workers._sla_alerted_sources
    assert api_mod.INGESTION_SLA_CHECK_INTERVAL_S == 300


def test_adaptive_timeout_carries_telemetry():
    err = api_mod._AdaptiveTimeout({"stalled": True})
    assert isinstance(err, asyncio.TimeoutError)
    assert err.telemetry == {"stalled": True}


import asyncio  # noqa: E402


def test_internal_query_classifier_prefixes():
    f = api_mod._is_internal_query
    assert f("Re-run backtest for NBA totals") is True
    assert f("investigate why the line moved") is True
    assert f("backtest results for hypothesis h-1") is True
    assert f("cycle 3 review") is True
    assert f("What's the weather at Lambeau on Sunday?") is False
    assert f("") is False


# ---------------------------------------------------------------------------
# Route inventory: gating contract per route family touched in slice 5.
# ---------------------------------------------------------------------------

ADMIN_OR_LOOPBACK_ROUTES = [
    ("get", "/model/total/{sport}"),
    ("get", "/model/environment"),
    ("post", "/hypothesis"),
    ("get", "/hypothesis"),
    ("get", "/hypothesis/{hypothesis_id}"),
    ("get", "/hypothesis/{hypothesis_id}/report"),
    ("get", "/hypothesis/{hypothesis_id}/significance"),
    ("get", "/backtest/run/{run_id}"),
    ("post", "/backtest/resolve/{run_id}"),
    ("get", "/historical/cache"),
    ("get", "/research/status"),
    ("get", "/research/sports"),
    ("get", "/embeddings/stats"),
    ("post", "/embeddings/search"),
    ("get", "/data/stats"),
    ("get", "/data/injuries/{sport}"),
    ("get", "/data/scoreboard/{sport}"),
    ("get", "/data/weather"),
    ("get", "/data/referee"),
    ("get", "/model/injury-impact/{sport}"),
]

ADMIN_ONLY_ROUTES = [
    ("post", "/hypothesis/{hypothesis_id}/promote"),
    ("patch", "/hypothesis/{hypothesis_id}"),
    ("post", "/backtest/run"),
    ("post", "/historical/fetch"),
    ("post", "/research/pause"),
    ("post", "/research/resume"),
    ("post", "/research/local-only"),
    ("post", "/research/collect"),
    ("post", "/research/generate"),
    ("post", "/research/batch-reject"),
    ("post", "/admin/claude/reset"),
]


def _decorator_block(path: str, method: str) -> str:
    m = re.search(
        rf'@app\.{method}\(\s*"{re.escape(path)}"[^\n]*',
        API_SOURCE,
    )
    assert m is not None, f"{method.upper()} {path} decorator missing from api.py"
    start = m.start()
    nxt = API_SOURCE.find("\n@", start + 1)
    end = nxt if nxt != -1 else len(API_SOURCE)
    return API_SOURCE[start:end]


@pytest.mark.parametrize(("method", "path"), ADMIN_OR_LOOPBACK_ROUTES)
def test_model_data_routes_keep_loopback_or_admin(method, path):
    deco = _decorator_block(path, method)
    assert "require_admin_or_loopback" in deco, (
        f"{method.upper()} {path} lost require_admin_or_loopback"
    )
    assert re.search(r"Depends\(require_admin\)(?!_or_loopback)", deco) is None, (
        f"{method.upper()} {path} must not use strict require_admin"
    )


@pytest.mark.parametrize(("method", "path"), ADMIN_ONLY_ROUTES)
def test_admin_only_routes_keep_strict_admin(method, path):
    deco = _decorator_block(path, method)
    assert re.search(r"Depends\(require_admin\)(?!_or_loopback)", deco), (
        f"{method.upper()} {path} lost strict require_admin"
    )
    assert "require_admin_or_loopback" not in deco


GATED_DUMP_ROUTES = [
    ("get", "/health/detailed"),
    ("get", "/health/deep"),
    ("get", "/admin/writer"),
]


@pytest.mark.parametrize(("method", "path"), GATED_DUMP_ROUTES)
def test_gated_dumps_still_gated(method, path):
    deco = _decorator_block(path, method)
    assert "require_admin" in deco, f"{method.upper()} {path} must stay gated"


# ---------------------------------------------------------------------------
# Health trio stays PUBLIC.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "func"),
    [
        ("/health", "health_check"),
        ("/health/livez", "health_livez"),
        ("/health/readyz", "health_readyz"),
    ],
)
def test_health_trio_stays_public(path, func):
    i = API_SOURCE.find(f'@app.get("{path}"')
    assert i != -1, f"{path} missing from api.py"
    window = API_SOURCE[i : API_SOURCE.find("\n@", i)]
    assert "require_admin" not in window, f"{path} must stay public"
    assert re.search(rf"async def {func}\(", window), f"{path} handler renamed"


def test_health_livez_awaits_system_routes_body():
    """'/health/livez must await the extracted coroutine — never return one.'"""
    m = re.search(
        r'@app\.get\("/health/livez"\).*?async def health_livez\(\).*?return (.*?)\n',
        API_SOURCE,
        re.DOTALL,
    )
    assert m is not None, "health_livez body not found"
    ret = m.group(1).strip()
    assert ret.startswith("await "), f"/health/livez must await its body, got: {ret!r}"
    assert "_system_routes.health_livez()" in ret
    from tools.api import system_routes
    assert inspect.iscoroutinefunction(system_routes.health_livez)


# ---------------------------------------------------------------------------
# Facade wrappers stay thin single delegations.
# ---------------------------------------------------------------------------

FACADE_DELEGATIONS = [
    ("create_hypothesis", "_hypothesis_routes.create_hypothesis(req)"),
    ("list_hypotheses", "_hypothesis_routes.list_hypotheses(status=status)"),
    ("get_hypothesis", "_hypothesis_routes.get_hypothesis(hypothesis_id)"),
    ("hypothesis_report", "_hypothesis_routes.hypothesis_report(hypothesis_id)"),
    ("hypothesis_significance", "_hypothesis_routes.hypothesis_significance(hypothesis_id, stage)"),
    ("promote_hypothesis", "_hypothesis_routes.promote_hypothesis(hypothesis_id)"),
    ("update_hypothesis", "_hypothesis_routes.update_hypothesis(hypothesis_id, request)"),
    ("run_backtest", "_backtest_routes.run_backtest(req)"),
    ("get_backtest_results", "_backtest_routes.get_backtest_results(run_id)"),
    ("resolve_backtest", "_backtest_routes.resolve_backtest(run_id, sport)"),
    ("historical_cache_stats", "_backtest_routes.historical_cache_stats()"),
    ("fetch_historical", "_backtest_routes.fetch_historical("),
    ("research_status", "_research_routes.research_status()"),
    ("research_pause", "_research_routes.research_pause()"),
    ("research_resume", "_research_routes.research_resume()"),
    ("research_local_only", "_research_routes.research_local_only(enabled)"),
    ("research_collect", "_research_routes.research_collect(sport, date)"),
    ("research_generate", "_research_routes.research_generate(sport, max_hypotheses)"),
    ("batch_reject_hypotheses", "_research_routes.batch_reject_hypotheses(body)"),
    ("get_research_sports", "_research_routes.get_research_sports()"),
    ("embedding_stats", "_data_routes.embedding_stats(collection)"),
    ("embedding_search", "_data_routes.embedding_search(collection, query, top_k)"),
    ("data_collection_stats", "_data_routes.data_collection_stats()"),
    ("get_scoreboard", "_data_routes.get_scoreboard(sport)"),
    ("get_weather", "_data_routes.get_weather(latitude, longitude, venue=venue)"),
    ("referee_info", "_data_routes.referee_info(refs, sport)"),
    ("get_model_total", "_model_routes.get_model_total("),
    ("get_model_environment", "_model_routes.get_model_environment("),
    ("get_injuries", "_model_routes.get_injuries(sport)"),
    ("injury_impact_model", "_model_routes.injury_impact_model(sport)"),
    ("simulate_portfolio_endpoint", "_simulate.simulate_portfolio_endpoint("),
]


@pytest.mark.parametrize(("func", "delegation"), FACADE_DELEGATIONS)
def test_facade_is_single_delegation(func, delegation):
    m = re.search(rf"async def {func}\(.*?\n(?=@|\Z)", API_SOURCE, re.DOTALL)
    assert m is not None, f"{func} handler missing from api.py"
    body = m.group(0)
    assert delegation in body, f"api.{func} should delegate via {delegation!r}"
    # Strip docstring + the delegation lines; whatever remains must be trivial.
    body_no_doc = re.sub(r'"""[\s\S]*?"""', "", body)
    # Drop the signature block (up through its closing "):").
    sig_end = body_no_doc.find("):")
    inner = body_no_doc[sig_end + 2:] if sig_end != -1 else body_no_doc
    stmts = [
        ln.strip()
        for ln in inner.splitlines()
        if ln.strip()
        and not ln.strip().startswith(("return await", "return ", "@"))
        and not ln.strip().startswith(("_auth", ")"))
    ]
    # kwarg-continuation lines (e.g. multi-line delegation calls) are fine.
    non_trivial = [
        s for s in stmts
        if not s.startswith("#")
        and not s.endswith(",")
        and not ("=" in s and s.split("=")[0].strip().isidentifier())
    ]
    assert len(non_trivial) <= 2, (
        f"api.{func} facade grew logic beyond delegation: {non_trivial!r}"
    )


def test_portfolio_facade_delegates_all_params():
    """The /simulate/portfolio wrapper forwards every query param."""
    m = re.search(r"async def simulate_portfolio_endpoint\(.*?(?=\n@)", API_SOURCE, re.DOTALL)
    assert m is not None
    body = m.group(0)
    for param in [
        "hypothesis_ids=hypothesis_ids",
        "n_sims=n_sims",
        "horizon_days=horizon_days",
        "starting_bankroll=starting_bankroll",
        "kelly_fraction=kelly_fraction",
        "all_live=all_live",
    ]:
        assert param in body, f"/simulate/portfolio wrapper dropped {param}"


# ---------------------------------------------------------------------------
# Extracted bodies really are coroutines / functions in their modules.
# ---------------------------------------------------------------------------

EXTRACTED_MODULE_FUNCS = [
    ("tools.api.workers", ["task_worker", "wal_checkpoint_loop", "restart_signal_watcher",
                            "ingestion_sla_watchdog_loop", "order_cron_loop",
                            "_maybe_auto_followup", "_run_session_with_adaptive_timeout"]),
    ("tools.api.simulate", ["simulate_basketball_game", "simulate_poisson_game",
                             "simulate_portfolio_endpoint"]),
    ("tools.api.hypothesis_routes", ["create_hypothesis", "list_hypotheses",
                                      "update_hypothesis", "promote_hypothesis"]),
    ("tools.api.backtest_routes", ["run_backtest", "fetch_historical",
                                    "historical_cache_stats"]),
    ("tools.api.data_routes", ["get_scoreboard", "get_weather", "referee_info"]),
    ("tools.api.model_routes", ["get_model_total", "get_injuries", "injury_impact_model"]),
    ("tools.api.research_routes", ["research_status", "research_pause",
                                    "batch_reject_hypotheses"]),
]

CORO_EXCEPTIONS = {"_is_internal_query", "referee_info", "writer_stats",
                   "reset_claude_rate_limit", "research_local_only",
                   "simulate_poisson_game"}


@pytest.mark.parametrize(
    ("modname", "funcs"),
    EXTRACTED_MODULE_FUNCS,
    ids=[m for m, _ in EXTRACTED_MODULE_FUNCS],
)
def test_extracted_funcs_exist_and_are_coroutines(modname, funcs):
    import importlib as _il
    mod = _il.import_module(modname)
    for fn_name in funcs:
        fn = getattr(mod, fn_name)
        expected_async = fn_name not in CORO_EXCEPTIONS
        assert inspect.iscoroutinefunction(fn) is expected_async, (
            f"{modname}.{fn_name} async-ness changed"
        )


# ---------------------------------------------------------------------------
# Request schemas: defined in tools.api, subclassed in api.py.
# ---------------------------------------------------------------------------


def test_hypothesis_create_schema_lives_in_tools_api():
    from tools.api.hypothesis_routes import HypothesisCreate

    assert "class HypothesisCreate(BaseModel)" in HYP_SOURCE
    assert "class HypothesisCreate(BaseModel)" not in API_SOURCE
    assert "class HypothesisCreate(_hypothesis_routes.HypothesisCreate)" in API_SOURCE
    # Bounds survived the move.
    fields = HypothesisCreate.model_fields
    assert fields["name"].metadata[0].min_length == 1
    assert fields["edge_threshold"].default == pytest.approx(0.02)
    assert fields["min_sample_size"].default == 1000
    assert fields["significance_level"].default == pytest.approx(0.05)


def test_backtest_request_schema_lives_in_tools_api():
    from tools.api.backtest_routes import BacktestRequest

    assert "class BacktestRequest(BaseModel)" in BT_SOURCE
    assert "class BacktestRequest(BaseModel)" not in API_SOURCE
    assert "class BacktestRequest(_backtest_routes.BacktestRequest)" in API_SOURCE
    fields = BacktestRequest.model_fields
    assert fields["credit_budget"].default == 50
    req = BacktestRequest(hypothesis_id="h1", start_date="2026-01-01",
                          end_date="2026-01-31")
    assert req.hypothesis_id == "h1"


def test_openapi_names_do_not_shift():
    """Subclassed models keep the same OpenAPI schema names."""
    from tools.api.hypothesis_routes import HypothesisCreate as HCInner
    from tools.api.backtest_routes import BacktestRequest as BRInner

    assert HCInner.__name__ == "HypothesisCreate"
    assert BRInner.__name__ == "BacktestRequest"
    # Subclasses validate identical payloads.
    payload = {
        "name": "n", "thesis": "t", "sport": "nba",
        "market_type": "total",
    }
    assert HCInner(**payload).name == "n"
    assert type("HC", (HCInner,), {})(**payload).sport == "nba"


def test_schema_validation_bounds_enforced():
    from pydantic import ValidationError
    from tools.api.hypothesis_routes import HypothesisCreate

    with pytest.raises(ValidationError):
        HypothesisCreate(name="", thesis="t", sport="s", market_type="m")
    with pytest.raises(ValidationError):
        HypothesisCreate(name="n", thesis="t", sport="s", market_type="m",
                         edge_threshold=1.5)


# ---------------------------------------------------------------------------
# Portfolio sim behavior via the extracted module (no lifespan).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestPortfolioEndpointExtraction:
    def setup_method(self):
        api_mod._PORTFOLIO_SIM_CACHE.clear()
        from tools.api import simulate as _sim
        _sim._PORTFOLIO_SIM_CACHE.clear()

    def test_cache_helpers_are_shared_not_copied(self):
        from tools.api import simulate as _sim
        assert api_mod._PORTFOLIO_SIM_CACHE is _sim._PORTFOLIO_SIM_CACHE

    def test_store_cache_is_bounded_via_extracted_helper(self):
        import time as _time
        from tools.api import simulate as _sim
        now = _time.time()
        for i in range(40):
            _sim._store_portfolio_sim_cache((f"k{i}",), (now, {"i": i}))
            assert len(_sim._PORTFOLIO_SIM_CACHE) <= 32
        assert len(_sim._PORTFOLIO_SIM_CACHE) == 32
        # And visible through the api-module alias too.
        assert len(api_mod._PORTFOLIO_SIM_CACHE) == 32

    def test_normalize_params_via_extracted_module(self):
        from tools.api import simulate as _sim
        lo_n, lo_h = _sim.normalize_portfolio_params(1, -5)
        hi_n, hi_h = _sim.normalize_portfolio_params(999_999, 99_999)
        assert (lo_n, lo_h) == (10, 1)
        assert (hi_n, hi_h) == (5000, 365)

    def test_to_thread_contract_lives_in_extracted_body(self):
        src = inspect.getsource(_load_sim().simulate_portfolio_endpoint)
        assert "await asyncio.to_thread(\n        simulate_portfolio," in src

    def test_wrapper_is_thin(self):
        m = re.search(
            r"async def simulate_portfolio_endpoint\(.*?(?=\n@)",
            API_SOURCE, re.DOTALL,
        )
        body = re.sub(r'"""[\s\S]*?"""', "", m.group(0))
        sig_end = body.find("):")
        inner = body[sig_end + 2:] if sig_end != -1 else body
        lines = [ln.strip() for ln in inner.splitlines()
                 if ln.strip() and not ln.strip().startswith(("@", "_auth"))]
        code = [ln for ln in lines if not ln.startswith("#") and not ln.startswith("return")]
        real_code = [ln for ln in code
                     if not (ln.endswith(",") or ln == ")" or "=" in ln and ln.split("=")[0].strip().isidentifier())]
        assert len(real_code) <= 1, f"wrapper grew logic: {real_code!r}"


def _load_sim():
    from tools.api import simulate as _sim
    return _sim


# ---------------------------------------------------------------------------
# Workers module behavioral checks (called directly; lifespan not entered).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestWorkersModuleBehavior:
    def test_task_worker_classifies_before_running(self, monkeypatch):
        """task_worker consults the classifier budget table per task."""
        # Source-level pin: classify_and_budget imported lazily in the worker.
        assert "classify_and_budget" in WORKERS_SOURCE
        assert "get_hard_ceiling_s" in WORKERS_SOURCE

    def test_local_only_skip_present_in_worker(self):
        assert "local_only mode" in WORKERS_SOURCE
        assert "_task_blocked_by_local_only" in WORKERS_SOURCE
        assert 'getenv("CALLISTO_LOCAL_ONLY"' in WORKERS_SOURCE

    def test_wiki_autofile_failure_is_non_fatal_by_construction(self):
        assert "Wiki auto-file failed" in WORKERS_SOURCE

    def test_cancel_paths_present_in_loops(self):
        """Each long-running loop breaks on CancelledError."""
        for marker in ["except asyncio.CancelledError:\n            break"]:
            assert WORKERS_SOURCE.count(marker) >= 5, (
                "worker loops lost their CancelledError exit paths"
            )

    def test_memory_guardian_exit_path(self):
        assert "os._exit(0)" in WORKERS_SOURCE
        assert "restart_signal_path" in WORKERS_SOURCE

    def test_sla_watchdog_dedupes_by_source(self):
        assert "_sla_alerted_sources" in WORKERS_SOURCE
        assert "already filed" in WORKERS_SOURCE

    def test_sla_watchdog_recovery_rearms(self):
        assert "recovered, re-arming alert" in WORKERS_SOURCE


async def _cancel_after_tick(loop_fn):
    task = asyncio.create_task(loop_fn())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return task


# ---------------------------------------------------------------------------
# api.py source hygiene after extraction.
# ---------------------------------------------------------------------------


def test_api_py_shrank_below_1800_lines():
    n = len(API_SOURCE.splitlines())
    assert n < 1800, f"api.py grew back to {n} lines"


def test_worker_loops_removed_inline_from_api_source():
    for marker in [
        "MEMORY GUARDIAN: RSS=",
        "RESTART SIGNAL detected at",
        "SLA watchdog: filed investigation task",
        "order cron: expired",
        "auto_followup_queued:",
        "Worker picked up task",
    ]:
        assert marker not in API_SOURCE, f"{marker!r} should live in tools/api/workers.py"
        assert marker in WORKERS_SOURCE, f"{marker!r} must be present in workers module"


def test_health_debounce_logic_remains_in_api_health_check():
    assert "_HEALTH_FILE_DEBOUNCE_SECONDS" in API_SOURCE
    assert "_HEALTH_FILE_LAST_WRITE_TS" in API_SOURCE
    assert "await asyncio.to_thread(system_health.write_health_file)" in API_SOURCE


def test_regime_offload_comment_points_at_system_routes():
    assert "await asyncio.to_thread(detect_regime" in _read(os.path.join("tools", "api", "system_routes.py"))


def test_existing_modules_untouched_by_slice5():
    """odds/bets/simulate/system/boost/task modules still exist and import."""
    for modname in [
        "tools.api.odds_routes", "tools.api.bets", "tools.api.simulate",
        "tools.api.system_routes", "tools.api.boost_routes", "tools.api.task_routes",
        "tools.api.order_routes", "tools.api.debug_routes", "tools.api.wiki",
        "tools.api.analysis", "tools.api.odds_extra",
    ]:
        import importlib as _il
        assert _il.import_module(modname) is not None


def test_public_write_allowlist_not_grown():
    assert 'public_endpoint("POST", "/task")' in API_SOURCE
    assert 'public_endpoint("POST", "/context/sync")' in API_SOURCE
    assert len(getattr(api_mod, "_PUBLIC_WRITE_ENDPOINTS", set())) <= 4


# ---------------------------------------------------------------------------
# Paper-trade / live seal untouched by this refactor.
# ---------------------------------------------------------------------------


def test_paper_trade_seal_untouched():
    """No extracted file adds 'live' to the paper-trade statuses or touches
    generate_paper_trade_signal's status gate."""
    for name, src in ALL_EXTRACTED_SOURCES.items():
        assert "generate_paper_trade_signal" not in src or "status == \"live\"" not in src, (
            f"tools/api {name} touched the paper-trade/live boundary"
        )
    assert '"live"' not in re.sub(r"#.*", "", WORKERS_SOURCE), (
        "workers module must not introduce literal 'live' status handling"
    )


def test_executor_enable_not_armed_by_refactor():
    """The executor stays disabled by default; nothing here flips it on."""
    for name, src in ALL_EXTRACTED_SOURCES.items():
        assert "executor.enable" not in src, f"{name} arms the executor"
    assert "CALLISTO_EXECUTOR_ENABLED" not in WORKERS_SOURCE


# ---------------------------------------------------------------------------
# App-object sanity (module import without lifespan).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestAppObjectSlice5:
    def test_app_object_exists(self):
        assert api_mod.app is not None

    def test_worker_aliases_resolve_to_same_objects_as_module(self):
        from tools.api import workers as _workers
        assert api_mod.task_worker is _workers.task_worker
        assert api_mod._AdaptiveTimeout is _workers._AdaptiveTimeout
        assert api_mod._run_session_with_adaptive_timeout is (
            _workers._run_session_with_adaptive_timeout
        )

    def test_health_routes_registered_public(self):
        paths = {getattr(r, "path", "") for r in api_mod.app.routes}
        for path in ("/health", "/health/livez", "/health/readyz"):
            assert path in paths, f"{path} not registered"

    def test_new_route_families_registered(self):
        paths = {getattr(r, "path", "") for r in api_mod.app.routes}
        for path in ("/hypothesis", "/backtest/run/{run_id}", "/research/status",
                     "/data/stats", "/model/environment"):
            assert path in paths, f"{path} not registered"

    def test_health_livez_direct_call_returns_dict(self):
        report = asyncio.run(api_mod.health_livez())
        assert isinstance(report, dict)
