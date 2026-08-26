"""Source-contract + behavior tests for the slice-7 api.py split.

Pins that:
  * The lifespan glue (research-stack unpacking, background-task binding,
    cancel-and-await sweeps, research-stack close tail) lives in
    tools/api/lifecycle.py; api.py's lifespan keeps the phase ORDER, the
    yield, and the producers-before-writers shutdown block whose ordering
    contract is pinned by tests/test_event_bus_lifecycle.py.
  * The new lifecycle helpers preserve api.py's historical semantics:
    unpack order, task binding order, per-task cancel/await, close order.
  * /health, /health/livez, /health/readyz stay PUBLIC (no admin dep) and
    /health/livez still awaits _system_routes.health_livez().
  * Every require_admin / require_admin_or_loopback gate survives.
  * Gated dumps (/health/detailed, /health/deep, /admin/writer) stay gated.
  * The paper-trade/live seal is untouched — no extracted module widens
    generate_paper_trade_signal to 'live' and nothing arms executor.enable.

Network-free: no lifespan is entered, no live endpoint is contacted.
"""

from __future__ import annotations

import ast
import asyncio
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


LIFECYCLE_SOURCE = _read(os.path.join("tools", "api", "lifecycle.py"))

ALL_SLICE7_SOURCES = {
    "lifecycle": LIFECYCLE_SOURCE,
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
# Module layout: lifespan glue moved into tools/api/lifecycle.py
# ---------------------------------------------------------------------------


def test_lifecycle_defines_slice7_helpers():
    for fn in (
        "unpack_research_stack",
        "bind_background_tasks",
        "cancel_tasks",
        "close_research_stack",
    ):
        assert f"def {fn}(" in LIFECYCLE_SOURCE, f"lifecycle lost {fn}"


def test_lifecycle_still_defines_slice6_phases():
    for fn in (
        "startup_tracemalloc",
        "startup_write_coordinator",
        "startup_migrations",
        "startup_research_stack",
        "spawn_background_tasks",
        "close_http_clients",
        "stop_live_state_collector",
    ):
        assert f"def {fn}(" in LIFECYCLE_SOURCE, f"lifecycle lost {fn}"


@pytest.mark.parametrize(
    "marker",
    [
        "unpack_research_stack",
        "bind_background_tasks",
        "cancel_tasks",
        "close_research_stack",
    ],
)
def test_api_delegates_via_slice7_helpers(marker):
    assert marker in API_SOURCE, f"api.py no longer references {marker}"


def test_api_py_shrank_again():
    n = len(API_SOURCE.splitlines())
    assert n < 1395, f"api.py grew back to {n} lines"


def test_api_py_is_smaller_than_slice6_baseline():
    """Slice 6 ended at 1406 lines; slice 7 must be strictly smaller."""
    n = len(API_SOURCE.splitlines())
    assert n < 1406, f"slice 7 did not shrink api.py ({n} >= 1406)"


# ---------------------------------------------------------------------------
# Lifespan facade invariants preserved (pinned contracts).
# ---------------------------------------------------------------------------


def _lifespan_source() -> str:
    tree = ast.parse(API_SOURCE)
    lifespan = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan"
    )
    return ast.get_source_segment(API_SOURCE, lifespan)


LIFESPAN_SRC = None  # populated lazily below (module scope keeps pytest happy)


@pytest.fixture(scope="module")
def lifespan_src() -> str:
    return _lifespan_source()


LIFECYCLE_PHASES = [
    "startup_tracemalloc",
    "startup_write_coordinator",
    "startup_migrations",
    "startup_model_warmup",
    "startup_correlation_store",
    "startup_core_services",
    "startup_line_monitor",
    "startup_research_stack",
    "startup_watchdogs",
    "startup_telegram_listener",
    "startup_game_scheduler",
    "startup_event_bus_drain",
    "startup_live_state_collector",
    "spawn_background_tasks",
    "announce_startup",
]


def test_lifespan_calls_phases_in_documented_order(lifespan_src):
    last = -1
    for phase in LIFECYCLE_PHASES:
        i = lifespan_src.find(f"_lifecycle.{phase}(")
        assert i != -1, f"lifespan does not call _lifecycle.{phase}"
        assert i > last, f"phase {phase} called out of documented order"
        last = i


def test_lifespan_keeps_yield(lifespan_src):
    assert "\n    yield\n" in lifespan_src, "lifespan lost its yield"


def test_lifespan_shutdown_order_producers_before_writers(lifespan_src):
    yield_idx = lifespan_src.index("yield")
    shutdown = lifespan_src[yield_idx:]
    writers_idx = shutdown.index("_stop_writers()")
    assert shutdown.index("game_scheduler.stop()") < writers_idx
    assert shutdown.index("get_event_bus().stop()") < writers_idx
    assert shutdown.index("heartbeat.stop()") < writers_idx
    for pat in (
        "await game_scheduler.stop()",
        "await get_event_bus().stop()",
        "await heartbeat.stop()",
        "await _stop_writers()",
    ):
        assert pat in shutdown, f"missing awaited stop: {pat}"


def test_lifespan_restart_task_cancellation_survived(lifespan_src):
    yield_idx = lifespan_src.index("yield")
    shutdown = lifespan_src[yield_idx:]
    restart_idx = shutdown.index("_restart_task.cancel()")
    live_state_idx = shutdown.index("stop_live_state_collector")
    assert live_state_idx < restart_idx, (
        "live-state stop must precede the H-14 orphaned-restart cancellation"
    )


def test_lifespan_single_owner_comment_survives(lifespan_src):
    assert "Sole owner" in lifespan_src


def test_lifespan_closes_http_clients_via_lifecycle(lifespan_src):
    src = inspect.getsource(api_mod.lifespan) if api_mod else lifespan_src
    assert "close_http_clients" in src
    for client in (
        "close_odds_client", "close_ctx_client", "close_embed_client",
        "close_dc_client", "close_dk_client", "close_all_clients",
    ):
        assert client not in src, f"{client} should be encapsulated in lifecycle"


def test_no_odds_stream_helpers_in_api_source():
    assert "from tools.odds_ws import" not in API_SOURCE
    assert "await start_odds_stream" not in API_SOURCE


def test_worker_loops_not_back_in_api_source():
    for marker in (
        "MEMORY GUARDIAN: RSS=",
        "RESTART SIGNAL detected at",
        "SLA watchdog: filed investigation task",
        "order cron: expired",
        "auto_followup_queued:",
        "Worker picked up task",
    ):
        assert marker not in API_SOURCE, f"{marker!r} leaked back into api.py"


# ---------------------------------------------------------------------------
# Slice-7 helper semantics (behavioral, with fake tasks/components).
# ---------------------------------------------------------------------------


def _make_task(coro=None):
    if coro is None:
        async def _spin():
            while True:
                await asyncio.sleep(3600)
        coro = _spin()
    return asyncio.create_task(coro)


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestUnpackResearchStack:
    def test_returns_tuple_in_pinned_global_order(self):
        from tools.api.lifecycle import RESEARCH_STACK_KEYS, unpack_research_stack

        sentinel = {k: object() for k in RESEARCH_STACK_KEYS}
        out = unpack_research_stack(sentinel)
        expected = (
            "clv_tracker", "order_manager", "hypothesis_manager",
            "historical_fetcher", "backtest_engine", "vector_store",
            "hypothesis_generator", "data_collector", "research_loop",
        )
        assert tuple(sentinel[k] for k in expected) == out

    def test_missing_key_raises_key_error(self):
        from tools.api.lifecycle import unpack_research_stack

        with pytest.raises(KeyError):
            unpack_research_stack({"clv_tracker": object()})


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestBindBackgroundTasks:
    def test_returns_tuple_in_pinned_task_order(self):
        from tools.api.lifecycle import BACKGROUND_TASK_KEYS, bind_background_tasks

        sentinel = {k: ("task-" + k) for k in BACKGROUND_TASK_KEYS}
        out = bind_background_tasks(sentinel)
        expected = (
            "worker", "wal_checkpoint", "restart_signal",
            "sla_watchdog", "order_cron", "prop_resolver",
        )
        assert out == tuple(sentinel[k] for k in expected)


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestCancelTasks:
    @pytest.mark.asyncio
    async def test_cancels_and_awaits_each_task_in_order(self):
        from tools.api.lifecycle import cancel_tasks

        cancelled = []

        class FakeTask:
            def __init__(self, name):
                self.name = name
                self._cancelled = asyncio.Event()

            def cancel(self):
                cancelled.append(self.name)
                self._cancelled.set()

            def __await__(self):
                # Awaitable no-op: mimics a task that finishes on cancel.
                async def _wait():
                    await self._cancelled.wait()
                return _wait().__await__()

        tasks = [FakeTask(f"t{i}") for i in range(3)]
        await cancel_tasks(*tasks)
        assert cancelled == ["t0", "t1", "t2"]

    @pytest.mark.asyncio
    async def test_real_pending_task_is_cancelled(self):
        from tools.api.lifecycle import cancel_tasks

        async def spin():
            while True:
                await asyncio.sleep(3600)

        task = asyncio.create_task(spin())
        await cancel_tasks(task)
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_none_entries_are_skipped(self):
        from tools.api.lifecycle import cancel_tasks

        await cancel_tasks(None, None)

    @pytest.mark.asyncio
    async def test_already_done_task_does_not_raise(self):
        from tools.api.lifecycle import cancel_tasks

        async def quick():
            return 42

        task = asyncio.create_task(quick())
        await asyncio.sleep(0)
        await cancel_tasks(task)


class _Closeable:
    def __init__(self, log, name, fail=False):
        self._log = log
        self._name = name
        self._fail = fail

    async def close(self):
        self._log.append(self._name)
        if self._fail:
            raise RuntimeError("boom")


class _Stoppable(_Closeable):
    async def stop(self):
        self._log.append(self._name + ":stop")


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestCloseResearchStack:
    @pytest.mark.asyncio
    async def test_close_order_matches_historical_inline_sequence(self):
        from tools.api.lifecycle import close_research_stack

        log = []
        await close_research_stack(
            data_collector=_Closeable(log, "data_collector"),
            hypothesis_generator=_Closeable(log, "hypothesis_generator"),
            vector_store=_Closeable(log, "vector_store"),
            backtest_engine=_Closeable(log, "backtest_engine"),
            historical_fetcher=_Closeable(log, "historical_fetcher"),
            hypothesis_manager=_Closeable(log, "hypothesis_manager"),
            clv_tracker=_Closeable(log, "clv_tracker"),
            learned_correlation_store=_Closeable(log, "learned_correlation_store"),
        )
        assert log == [
            "data_collector",
            "hypothesis_generator",
            "vector_store",
            "backtest_engine",
            "historical_fetcher",
            "hypothesis_manager",
            "clv_tracker",
            "learned_correlation_store",
        ]

    @pytest.mark.asyncio
    async def test_optional_components_tolerate_none(self):
        from tools.api.lifecycle import close_research_stack

        log = []
        # Matches pre-split inline behavior: only the four core components
        # are closed unconditionally; the rest may be None.
        await close_research_stack(
            backtest_engine=_Stoppable(log, "backtest_engine"),
            historical_fetcher=_Closeable(log, "historical_fetcher"),
            hypothesis_manager=_Closeable(log, "hypothesis_manager"),
            clv_tracker=_Closeable(log, "clv_tracker"),
        )
        assert log == [
            "backtest_engine",
            "historical_fetcher",
            "hypothesis_manager",
            "clv_tracker",
        ]

    @pytest.mark.asyncio
    async def test_learned_correlation_store_none_ok(self):
        from tools.api.lifecycle import close_research_stack

        log = []
        await close_research_stack(
            data_collector=None,
            hypothesis_generator=None,
            vector_store=None,
            backtest_engine=_Closeable(log, "backtest_engine"),
            historical_fetcher=_Closeable(log, "historical_fetcher"),
            hypothesis_manager=_Closeable(log, "hypothesis_manager"),
            clv_tracker=_Closeable(log, "clv_tracker"),
            learned_correlation_store=None,
        )
        assert len(log) == 4


# ---------------------------------------------------------------------------
# Health trio stays PUBLIC and delegated.
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
    m = re.search(
        r'@app\.get\("/health/livez"\).*?async def health_livez\(\).*?return (.*?)\n',
        API_SOURCE,
        re.DOTALL,
    )
    assert m is not None
    ret = m.group(1).strip()
    assert ret.startswith("await ")
    assert "_system_routes.health_livez()" in ret


GATED_DUMP_ROUTES = [
    ("get", "/health/detailed"),
    ("get", "/health/deep"),
    ("get", "/admin/writer"),
]


@pytest.mark.parametrize(("method", "path"), GATED_DUMP_ROUTES)
def test_gated_dumps_still_gated(method, path):
    m = re.search(rf'@app\.{method}\(\s*"{re.escape(path)}"[^\n]*', API_SOURCE)
    assert m is not None, f"{method.upper()} {path} missing"
    assert "require_admin" in m.group(0)


# ---------------------------------------------------------------------------
# Representative gating spot-checks across route families (defense in depth).
# ---------------------------------------------------------------------------


STRICT_ADMIN_SPOT_CHECKS = [
    ("post", "/bets/record"),
    ("post", "/bets/{bet_id}/resolve"),
    ("post", "/executor/enable"),
    ("post", "/orders/{order_id}/approve"),
    ("patch", "/hypothesis/{hypothesis_id}"),
]


@pytest.mark.parametrize(("method", "path"), STRICT_ADMIN_SPOT_CHECKS)
def test_strict_admin_routes_keep_strict_gate(method, path):
    m = re.search(rf'@app\.{method}\(\s*"{re.escape(path)}"[^\n]*', API_SOURCE)
    assert m is not None, f"{method.upper()} {path} decorator missing"
    deco = m.group(0)
    assert re.search(r"Depends\(require_admin\)(?!_or_loopback)", deco), (
        f"{method.upper()} {path} lost strict require_admin"
    )
    assert "require_admin_or_loopback" not in deco


LOOPBACK_OR_ADMIN_SPOT_CHECKS = [
    ("get", "/odds/movements"),
    ("get", "/wiki/stats"),
    ("get", "/simulate/portfolio"),
    ("get", "/system/full-status"),
    ("post", "/orders/reconcile"),
    ("get", "/tasks"),
]


@pytest.mark.parametrize(("method", "path"), LOOPBACK_OR_ADMIN_SPOT_CHECKS)
def test_loopback_or_admin_routes_keep_soft_gate(method, path):
    m = re.search(rf'@app\.{method}\(\s*"{re.escape(path)}"[^\n]*', API_SOURCE)
    assert m is not None, f"{method.upper()} {path} decorator missing"
    deco = m.group(0)
    assert "require_admin_or_loopback" in deco
    assert re.search(r"Depends\(require_admin\)(?!_or_loopback)", deco) is None


def test_public_write_allowlist_unchanged():
    assert 'public_endpoint("POST", "/task")' in API_SOURCE
    assert 'public_endpoint("POST", "/context/sync")' in API_SOURCE
    if api_mod is not None:
        assert isinstance(api_mod._PUBLIC_WRITE_ENDPOINTS, set)
        assert len(api_mod._PUBLIC_WRITE_ENDPOINTS) <= 32


# ---------------------------------------------------------------------------
# App-object sanity (module import without lifespan).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestAppObjectSlice7:
    def test_app_object_exists(self):
        assert api_mod.app is not None

    def test_route_count_preserved(self):
        paths = {getattr(r, "path", "") for r in api_mod.app.routes}
        for path in (
            "/task", "/health", "/health/livez", "/health/readyz",
            "/odds/movements", "/bets", "/simulate/portfolio",
            "/hypothesis", "/research/status", "/data/stats",
            "/orders", "/executor/status", "/wiki/stats",
            "/boosts/devig", "/model/environment", "/debug/memory",
        ):
            assert path in paths, f"{path} not registered"

    def test_lifecycle_helper_imports_resolve_to_same_module(self):
        from tools.api import lifecycle as lc
        assert api_mod.unpack_research_stack is lc.unpack_research_stack
        assert api_mod.bind_background_tasks is lc.bind_background_tasks
        assert api_mod.cancel_tasks is lc.cancel_tasks
        assert api_mod.close_research_stack is lc.close_research_stack

    def test_lifespan_remains_a_context_manager(self):
        # @asynccontextmanager wraps an async-generator function.
        assert hasattr(api_mod.lifespan, "__wrapped__")
        inner = api_mod.lifespan.__wrapped__
        assert inspect.isasyncgenfunction(inner) or asyncio.iscoroutinefunction(inner)

    def test_backward_compat_aliases_survive(self):
        for name in (
            "_client_is_loopback", "_log_auth_denied", "require_admin",
            "require_admin_or_loopback", "_default_secure_middleware",
            "public_endpoint", "_WRITE_METHODS", "_PUBLIC_WRITE_ENDPOINTS",
        ):
            assert hasattr(api_mod, name), f"api module lost {name}"

    def test_worker_aliases_survive(self):
        for name in (
            "task_worker", "wal_checkpoint_loop", "restart_signal_watcher",
            "order_cron_loop", "_run_session_with_adaptive_timeout",
            "_AdaptiveTimeout", "ingestion_sla_watchdog_loop",
            "INGESTION_SLA_CHECK_INTERVAL_S", "TASK_WORKER_TIMEOUT_S",
            "_sla_alerted_sources", "_is_internal_query", "_maybe_auto_followup",
        ):
            assert hasattr(api_mod, name), f"api module lost worker alias {name}"

    def test_portfolio_cache_aliases_survive(self):
        for name in (
            "_fetch_live_hypothesis_ids", "_get_portfolio_sim_cache",
            "_store_portfolio_sim_cache", "_PORTFOLIO_SIM_CACHE",
            "_PORTFOLIO_SIM_CACHE_MAX_ENTRIES", "_PORTFOLIO_SIM_CACHE_TTL",
        ):
            assert hasattr(api_mod, name), f"api module lost cache alias {name}"

    def test_health_report_aliases_survive(self):
        assert callable(api_mod._build_health_report)
        assert callable(api_mod._evaluate_health_signals)

    def test_restart_sink_and_alias_survive(self):
        assert hasattr(api_mod, "_set_restart_task")
        assert "global _restart_task" in API_SOURCE


# ---------------------------------------------------------------------------
# Paper-trade / live seal + executor seal untouched by this refactor.
# ---------------------------------------------------------------------------


def test_paper_trade_seal_untouched():
    for name, src in ALL_SLICE7_SOURCES.items():
        clean = re.sub(r"#.*", "", src)
        assert "generate_paper_trade_signal" not in clean, (
            f"tools/api/{name}.py references the paper-trade generator"
        )
        assert '"live"' not in clean, (
            f"tools/api/{name}.py introduces literal 'live' status handling"
        )


def test_executor_enable_not_armed_by_refactor():
    for name, src in ALL_SLICE7_SOURCES.items():
        assert "executor.enable" not in src, f"{name} arms the executor"
        assert "CALLISTO_EXECUTOR_ENABLED" not in src


def test_route_decorators_not_duplicated_between_api_and_lifecycle():
    assert "@app.get(" not in LIFECYCLE_SOURCE
    assert "@app.post(" not in LIFECYCLE_SOURCE
