"""Source-contract + behavior tests for the slice-6 api.py split.

Pins that:
  * The auth/security primitives (loopback check, auth-denial audit log,
    Bearer gates, default-secure write-gate core) live in
    tools/api/security.py; api.py keeps Depends(...) targets and aliases.
  * Global exception handlers live in tools/api/errors.py and are
    registered on the app; HTTPException passes through, unknown errors
    become structured JSON 500s, validation errors become compact 422s.
  * Lifespan startup phases live in tools/api/lifecycle.py in a fixed,
    documented order; api.py's lifespan keeps the phase ordering, the
    yield, and the FULL inline shutdown sequence whose ordering contract
    is pinned by test_event_bus_lifecycle.
  * The __main__ port-wait + serve loop lives in tools/api/serve.py.
  * /health, /health/livez, /health/readyz stay PUBLIC (no admin dep) and
    /health/livez still awaits _system_routes.health_livez().
  * Every require_admin / require_admin_or_loopback gate survives.
  * Gated dumps (/health/detailed, /health/deep, admin/writer) stay gated.
  * The paper-trade/live seal is untouched — no extracted module widens
    generate_paper_trade_signal to 'live' and nothing arms executor.enable.

Network-free: no lifespan is entered, no live endpoint is contacted.
"""

from __future__ import annotations

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


SECURITY_SOURCE = _read(os.path.join("tools", "api", "security.py"))
ERRORS_SOURCE = _read(os.path.join("tools", "api", "errors.py"))
LIFECYCLE_SOURCE = _read(os.path.join("tools", "api", "lifecycle.py"))
SERVE_SOURCE = _read(os.path.join("tools", "api", "serve.py"))

ALL_SLICE6_SOURCES = {
    "security": SECURITY_SOURCE,
    "errors": ERRORS_SOURCE,
    "lifecycle": LIFECYCLE_SOURCE,
    "serve": SERVE_SOURCE,
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
# Module layout: bodies moved out of api.py into tools/api.
# ---------------------------------------------------------------------------


def test_security_module_defines_gate_bodies():
    assert "async def require_admin(" in SECURITY_SOURCE
    assert "async def require_admin_or_loopback(" in SECURITY_SOURCE
    assert "async def enforce_default_secure(" in SECURITY_SOURCE


@pytest.mark.parametrize(
    "marker",
    [
        "_client_is_loopback",
        "_log_auth_denied",
        "require_admin",
        "require_admin_or_loopback",
        "_default_secure_middleware",
        "public_endpoint",
        "_WRITE_METHODS",
        "_PUBLIC_WRITE_ENDPOINTS",
    ],
)
def test_api_keeps_security_surface(marker):
    """api.py keeps every historical security symbol as wrapper/alias."""
    pattern = rf"\b{re.escape(marker)}\b"
    assert re.search(pattern, API_SOURCE), f"api.py lost {marker}"


def test_api_aliases_delegate_to_security_module():
    assert api_mod._client_is_loopback is api_mod._security.client_is_loopback
    assert api_mod._log_auth_denied is api_mod._security.log_auth_denied


def test_errors_module_defines_handlers():
    assert "async def global_exception_handler(" in ERRORS_SOURCE
    assert "async def validation_exception_handler(" in ERRORS_SOURCE


def test_lifecycle_module_defines_phases():
    for fn in [
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
        "stop_live_state_collector",
        "close_http_clients",
    ]:
        assert f"def {fn}(" in LIFECYCLE_SOURCE, f"lifecycle lost {fn}"


def test_serve_module_defines_entrypoint():
    assert "def wait_for_port_free(" in SERVE_SOURCE
    assert "def serve(" in SERVE_SOURCE
    # api's __main__ block delegates instead of inlining the loop.
    assert "for attempt in range(30)" not in API_SOURCE
    assert "_serve.serve(" in API_SOURCE


# ---------------------------------------------------------------------------
# Auth behaviour through the extracted module (no lifespan).
# ---------------------------------------------------------------------------


def _fake_request(client_host: str, method: str = "GET", path: str = "/x",
                  headers: dict | None = None):
    from fastapi import Request

    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": hdrs,
        "client": (client_host, 55555),
        "query_string": b"",
    }
    return Request(scope)


_NO_CREDS = None  # explicit no-credentials for direct gate invocation


class TestSecurityModuleBehavior:
    def test_loopback_detection(self):
        f = api_mod._security.client_is_loopback
        assert f(_fake_request("127.0.0.1")) is True
        assert f(_fake_request("::1")) is True
        assert f(_fake_request("localhost")) is True
        assert f(_fake_request("10.0.0.5")) is False
        scope = {"type": "http", "method": "GET", "path": "/", "headers": [],
                 "client": None, "query_string": b""}
        from fastapi import Request as _Req
        assert f(_Req(scope)) is False

    def test_require_admin_fails_closed_without_token(self):
        old = api_mod.CALLISTO_ADMIN_TOKEN
        try:
            api_mod.CALLISTO_ADMIN_TOKEN = ""
            with pytest.raises(Exception) as ei:
                asyncio.run(api_mod._security.require_admin(_fake_request("127.0.0.1")))
            assert getattr(ei.value, "status_code", None) == 503
        finally:
            api_mod.CALLISTO_ADMIN_TOKEN = old

    def test_require_admin_accepts_good_token_rejects_bad(self):
        from fastapi.security import HTTPAuthorizationCredentials

        old = api_mod.CALLISTO_ADMIN_TOKEN
        try:
            api_mod.CALLISTO_ADMIN_TOKEN = "unit-token-6"
            good = HTTPAuthorizationCredentials(scheme="bearer", credentials="unit-token-6")
            bad = HTTPAuthorizationCredentials(scheme="bearer", credentials="nope")
            asyncio.run(api_mod._security.require_admin(
                _fake_request("127.0.0.1"), good))
            with pytest.raises(Exception) as ei:
                asyncio.run(api_mod._security.require_admin(
                    _fake_request("10.0.0.5"), bad))
            assert getattr(ei.value, "status_code", None) == 403
            with pytest.raises(Exception) as ei:
                asyncio.run(api_mod._security.require_admin(
                    _fake_request("10.0.0.5"), None))
            assert getattr(ei.value, "status_code", None) == 401
        finally:
            api_mod.CALLISTO_ADMIN_TOKEN = old

    def test_soft_gate_allows_loopback_even_with_token_set(self):
        """MCP server + local research loop send no Authorization header."""
        old = api_mod.CALLISTO_ADMIN_TOKEN
        try:
            api_mod.CALLISTO_ADMIN_TOKEN = "unit-token-6"
            asyncio.run(api_mod.require_admin_or_loopback(
                _fake_request("127.0.0.1"), None))
            with pytest.raises(Exception) as ei:
                asyncio.run(api_mod.require_admin_or_loopback(
                    _fake_request("10.0.0.5"), None))
            assert getattr(ei.value, "status_code", None) == 401
        finally:
            api_mod.CALLISTO_ADMIN_TOKEN = old

    def test_write_gate_core_matrix(self):
        sec = api_mod._security
        old = api_mod.CALLISTO_ADMIN_TOKEN
        try:
            api_mod.CALLISTO_ADMIN_TOKEN = ""
            # GET never gated.
            assert asyncio.run(sec.enforce_default_secure(
                _fake_request("10.0.0.5"))) is None
            # Non-loopback write without token -> 403 JSON response.
            resp = asyncio.run(sec.enforce_default_secure(
                _fake_request("10.0.0.5", "POST", "/task2")))
            assert resp is not None and resp.status_code == 403
            # Loopback write allowed.
            assert asyncio.run(sec.enforce_default_secure(
                _fake_request("127.0.0.1", "POST", "/task2"))) is None

            api_mod.CALLISTO_ADMIN_TOKEN = "unit-token-6"
            # Missing bearer -> 401.
            resp = asyncio.run(sec.enforce_default_secure(
                _fake_request("10.0.0.5", "POST", "/task2")))
            assert resp.status_code == 401
            # Bad token -> 403.
            resp = asyncio.run(sec.enforce_default_secure(
                _fake_request("10.0.0.5", "POST", "/task2"),
            )) if False else asyncio.run(sec.enforce_default_secure(
                _fake_request("10.0.0.5", "POST", "/task2",
                              {"authorization": "Bearer wrong"})))
            assert resp.status_code == 403
            # Good token -> allowed (None).
            assert asyncio.run(sec.enforce_default_secure(
                _fake_request("10.0.0.5", "POST", "/task2",
                              {"authorization": "Bearer unit-token-6"}))) is None
        finally:
            api_mod.CALLISTO_ADMIN_TOKEN = old

    def test_public_allowlist_bypasses_write_gate(self):
        sec = api_mod._security
        old = api_mod.CALLISTO_ADMIN_TOKEN
        try:
            api_mod.CALLISTO_ADMIN_TOKEN = ""
            # POST /task IS on the public allowlist -> no gate even non-loopback.
            assert asyncio.run(sec.enforce_default_secure(
                _fake_request("10.0.0.5", "POST", "/task"))) is None
        finally:
            api_mod.CALLISTO_ADMIN_TOKEN = old


# ---------------------------------------------------------------------------
# Exception handlers via the extracted module.
# ---------------------------------------------------------------------------


def _fake_exc_request(path: str = "/boom"):
    return _fake_request("127.0.0.1", path=path)


class TestExceptionHandlers:
    def test_http_exception_passthrough_shape(self):
        from fastapi import HTTPException
        from tools.api.errors import global_exception_handler

        resp = asyncio.run(global_exception_handler(
            _fake_exc_request(), HTTPException(status_code=418, detail="teapot")))
        assert resp.status_code == 418

    def test_unknown_error_structured_500(self):
        from fastapi.responses import JSONResponse
        from tools.api.errors import global_exception_handler

        try:
            raise ValueError("kaboom")
        except ValueError as e:
            exc = e
        resp = asyncio.run(global_exception_handler(_fake_exc_request("/p/x"), exc))
        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 500

    def test_validation_error_compact_422(self):
        from fastapi.exceptions import RequestValidationError
        from tools.api.errors import validation_exception_handler

        exc = RequestValidationError([])
        resp = asyncio.run(validation_exception_handler(_fake_exc_request(), exc))
        assert resp.status_code == 422

    def test_handlers_registered_on_app(self):
        # FastAPI stores registered handlers on app.exception_handlers.
        from fastapi.exceptions import RequestValidationError

        assert RequestValidationError in api_mod.app.exception_handlers
        assert Exception in api_mod.app.exception_handlers


# ---------------------------------------------------------------------------
# Lifespan phases: order + api.py wiring.
# ---------------------------------------------------------------------------


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
]


def test_lifespan_calls_phases_in_documented_order():
    """Each phase appears at most once in api.lifespan and in module order."""
    src = inspect.getsource(api_mod.lifespan)
    last = -1
    for phase in LIFECYCLE_PHASES:
        i = src.find(f"_lifecycle.{phase}(")
        assert i != -1, f"lifespan does not call _lifecycle.{phase}"
        assert i > last, f"phase {phase} called out of documented order"
        last = i


def test_lifespan_keeps_yield_and_inline_shutdown():
    src = inspect.getsource(api_mod.lifespan)
    yield_idx = src.index("yield")
    shutdown = src[yield_idx:]
    writers_idx = shutdown.index("_stop_writers()")
    # Producers/drains stop BEFORE the WriteCoordinator (pinned ordering).
    assert shutdown.index("game_scheduler.stop()") < writers_idx
    assert shutdown.index("get_event_bus().stop()") < writers_idx
    assert shutdown.index("heartbeat.stop()") < writers_idx
    for pat in ("await game_scheduler.stop()", "await get_event_bus().stop()",
                "await heartbeat.stop()", "await _stop_writers()"):
        assert pat in shutdown, f"missing awaited stop: {pat}"
    # Restart-task orphan cancellation survived the split (audit H-14).
    assert "_restart_task.cancel()" in shutdown


def test_lifespan_phase_functions_are_coroutines():
    lc = api_mod._lifecycle
    async_fns = [p for p in LIFECYCLE_PHASES if p != "startup_tracemalloc"]
    for name in async_fns:
        fn = getattr(lc, name)
        assert inspect.iscoroutinefunction(fn), f"{name} must be async"
    assert not inspect.iscoroutinefunction(lc.startup_tracemalloc)


def test_tracemalloc_phase_respects_env(monkeypatch):
    monkeypatch.delenv("CALLISTO_TRACEMALLOC", raising=False)
    assert api_mod._lifecycle.startup_tracemalloc() is False


def test_spawn_background_tasks_returns_expected_keys():
    keys = re.findall(r'"(\w+)":\s+asyncio\.create_task', LIFECYCLE_SOURCE)
    assert set(keys) >= {"worker", "wal_checkpoint", "restart_signal",
                         "sla_watchdog", "order_cron"}
    assert '"prop_resolver"' in LIFECYCLE_SOURCE


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


def test_health_file_debounce_logic_remains_in_api():
    assert "_HEALTH_FILE_DEBOUNCE_SECONDS" in API_SOURCE
    assert "_HEALTH_FILE_LAST_WRITE_TS" in API_SOURCE
    assert "await asyncio.to_thread(system_health.write_health_file)" in API_SOURCE


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
# Representative gating spot-checks across route families (defense in depth
# against accidental relaxation during extraction).
# ---------------------------------------------------------------------------


STRICT_ADMIN_SPOT_CHECKS = [
    ("post", "/bets/record"),
    ("post", "/bets/{bet_id}/resolve"),
    ("post", "/hypothesis/{hypothesis_id}/promote"),
    ("patch", "/hypothesis/{hypothesis_id}"),
    ("post", "/research/pause"),
    ("post", "/executor/enable"),
    ("post", "/orders/{order_id}/approve"),
]

# These two carry require_admin via an _auth signature param instead of the
# decorator line — pin them by handler signature.
SIGNATURE_ADMIN_ROUTES = [
    ("post", "/admin/sql", "admin_sql"),
    ("post", "/debug/memory/gc", "debug_gc"),
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


@pytest.mark.parametrize(("method", "path", "func"), SIGNATURE_ADMIN_ROUTES)
def test_signature_admin_routes_keep_strict_gate(method, path, func):
    m = re.search(
        rf'@app\.{method}\(\s*"{re.escape(path)}".*?def {func}\(.*?\):',
        API_SOURCE, re.DOTALL,
    )
    assert m is not None, f"{method.upper()} {path} missing"
    assert re.search(r"Depends\(require_admin\)(?!_or_loopback)", m.group(0)), (
        f"{func} lost strict require_admin via signature param"
    )


LOOPBACK_OR_ADMIN_SPOT_CHECKS = [
    ("get", "/odds/movements"),
    ("get", "/wiki/stats"),
    ("get", "/simulate/portfolio"),
    ("get", "/system/full-status"),
    ("post", "/odds/snapshot/{sport}"),
    ("get", "/executor/status"),
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


def test_edges_live_gated_via_signature_auth_param():
    m = re.search(
        r'@app\.get\("/edges/live"\).*?def get_live_edges\(.*?\):',
        API_SOURCE, re.DOTALL,
    )
    assert m is not None
    assert "require_admin_or_loopback" in m.group(0)


def test_public_write_allowlist_unchanged():
    assert 'public_endpoint("POST", "/task")' in API_SOURCE
    assert 'public_endpoint("POST", "/context/sync")' in API_SOURCE
    if api_mod is not None:
        assert len(api_mod._PUBLIC_WRITE_ENDPOINTS) <= 4


# ---------------------------------------------------------------------------
# App-object sanity (module import without lifespan).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestAppObjectSlice6:
    def test_app_object_exists(self):
        assert api_mod.app is not None

    def test_route_count_preserved(self):
        paths = {getattr(r, "path", "") for r in api_mod.app.routes}
        for path in ("/task", "/health", "/health/livez", "/health/readyz",
                     "/odds/movements", "/bets", "/simulate/portfolio",
                     "/hypothesis", "/research/status", "/data/stats",
                     "/orders", "/executor/status", "/wiki/stats",
                     "/boosts/devig", "/model/environment", "/debug/memory"):
            assert path in paths, f"{path} not registered"

    def test_security_module_importable(self):
        from tools.api import security
        assert security is api_mod._security

    def test_new_modules_importable(self):
        for modname in ("tools.api.security", "tools.api.errors",
                        "tools.api.lifecycle", "tools.api.serve"):
            assert importlib.import_module(modname) is not None

    def test_existing_slice_modules_still_importable(self):
        for modname in (
            "tools.api.odds_routes", "tools.api.bets", "tools.api.simulate",
            "tools.api.system_routes", "tools.api.boost_routes",
            "tools.api.task_routes", "tools.api.order_routes",
            "tools.api.debug_routes", "tools.api.wiki", "tools.api.analysis",
            "tools.api.odds_extra", "tools.api.workers", "tools.api.model_routes",
            "tools.api.data_routes", "tools.api.hypothesis_routes",
            "tools.api.backtest_routes", "tools.api.research_routes",
        ):
            assert importlib.import_module(modname) is not None

    def test_middleware_registered(self):
        names = [getattr(m.cls, "__name__", "") for m in api_mod.app.user_middleware]
        assert any("Middleware" in n or "Custom" in n or n for n in names)


# ---------------------------------------------------------------------------
# api.py source hygiene after slice 6.
# ---------------------------------------------------------------------------


def test_api_py_shrank_again():
    n = len(API_SOURCE.splitlines())
    assert n < 1450, f"api.py grew back to {n} lines"


def test_port_wait_loop_removed_inline_from_api_source():
    assert "TIME_WAIT" not in API_SOURCE
    assert "TIME_WAIT" in SERVE_SOURCE
    assert "uvicorn.run" in SERVE_SOURCE
    assert "uvicorn.run" not in API_SOURCE


def test_startup_heaviness_removed_from_api_source():
    """Startup implementation details now live in tools/api/lifecycle.py."""
    for marker in [
        "install_aiosqlite_routing",
        "apply_pending_migrations",
        "ensure_followup_columns",
        "warmup_models",
        "seed_from_priors",
        "start_audit_drain",
        "_start_live_collector",
        "prop_resolution_loop",
        "close_dk_client",
    ]:
        assert marker not in API_SOURCE, (
            f"{marker!r} should live in tools/api/lifecycle.py"
        )
        assert marker in LIFECYCLE_SOURCE, (
            f"{marker!r} must be present in lifecycle module"
        )


def test_single_owner_comment_survives_split():
    tree = __import__("ast").parse(API_SOURCE)
    lifespan = next(
        n for n in __import__("ast").walk(tree)
        if isinstance(n, __import__("ast").AsyncFunctionDef) and n.name == "lifespan"
    )
    import ast as _ast
    src = _ast.get_source_segment(API_SOURCE, lifespan)
    assert "Sole owner" in src
    assert "from tools.odds_ws import" not in API_SOURCE
    assert "await start_odds_stream" not in API_SOURCE


def test_shutdown_closes_every_shared_http_client_via_lifecycle():
    src = inspect.getsource(api_mod.lifespan)
    assert "close_http_clients" in src
    for client in ("close_odds_client", "close_ctx_client", "close_embed_client",
                   "close_dc_client", "close_dk_client", "close_all_clients"):
        assert client not in src, f"{client} should be encapsulated in lifecycle"
        assert client in LIFECYCLE_SOURCE


# ---------------------------------------------------------------------------
# Paper-trade / live seal + executor seal untouched by this refactor.
# ---------------------------------------------------------------------------


def test_paper_trade_seal_untouched():
    for name, src in ALL_SLICE6_SOURCES.items():
        clean = re.sub(r"#.*", "", src)
        assert "generate_paper_trade_signal" not in clean, (
            f"slice-6 module {name} references the paper-trade generator"
        )
        assert '"live"' not in clean, (
            f"slice-6 module {name} introduces literal 'live' status handling"
        )
    statuses = getattr(api_mod, "_PAPER_TRADE_SIGNAL_STATUSES", None)
    if statuses is not None:
        assert "live" not in statuses


def test_executor_enable_not_armed_by_refactor():
    for name, src in ALL_SLICE6_SOURCES.items():
        assert "executor.enable" not in src, f"{name} arms the executor"
        assert "CALLISTO_EXECUTOR_ENABLED" not in src


def test_route_decorators_not_duplicated_between_api_and_modules():
    """No @app.* decorators leaked into the new modules — only api.py owns routes."""
    for name, src in ALL_SLICE6_SOURCES.items():
        assert "@app.get(" not in src and "@app.post(" not in src, (
            f"tools/api/{name}.py defines its own routes"
        )
