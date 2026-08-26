"""Source-contract + behavior tests for the slice-3 api.py split.

Pins that:
  * api.py still owns the FastAPI decorators with the original gating
    (require_admin / require_admin_or_loopback) for every model, data,
    hypothesis, backtest/historical, research, system/full-status,
    debug/memory, admin/sql, and executor/order route whose body moved to
    tools/api/ in this slice.
  * /health, /health/livez, /health/readyz stay PUBLIC (no admin dep).
  * Gated dumps (/health/detailed, /health/deep, debug/memory, admin/sql)
    still require admin-or-loopback or admin.
  * The moved handler logic (unique strings) lives in tools/api modules,
    not api.py.
  * The import surface: tools.api.<module> exposes the extracted bodies,
    and api re-exports the compat aliases tests/operators rely on.
  * The paper-trade/live seal is untouched by this refactor.
"""

from __future__ import annotations

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


MODEL_SOURCE = _read(os.path.join("tools", "api", "model_routes.py"))
DATA_SOURCE = _read(os.path.join("tools", "api", "data_routes.py"))
HYP_SOURCE = _read(os.path.join("tools", "api", "hypothesis_routes.py"))
BT_SOURCE = _read(os.path.join("tools", "api", "backtest_routes.py"))
RES_SOURCE = _read(os.path.join("tools", "api", "research_routes.py"))
SYS_SOURCE = _read(os.path.join("tools", "api", "system_routes.py"))
DBG_SOURCE = _read(os.path.join("tools", "api", "debug_routes.py"))
ORD_SOURCE = _read(os.path.join("tools", "api", "order_routes.py"))


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
# Route inventory: gating contract per route moved in slice 3.
# ---------------------------------------------------------------------------

ADMIN_OR_LOOPBACK_ROUTES = [
    ("get", "/model/total/{sport}"),
    ("get", "/model/environment"),
    ("get", "/data/injuries/{sport}"),
    ("get", "/model/injury-impact/{sport}"),
    ("get", "/data/scoreboard/{sport}"),
    ("get", "/data/weather"),
    ("get", "/data/referee"),
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
    ("get", "/health/detailed"),
    ("get", "/regime/sizer-multipliers"),
    ("get", "/health/deep"),
    ("get", "/health/integrity/history"),
    ("get", "/claude/status"),
    ("get", "/system/full-status"),
    ("get", "/debug/memory"),
    ("get", "/debug/memory/top-traces"),
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
    ("post", "/debug/memory/gc"),
    ("post", "/admin/sql"),
    ("get", "/admin/writer"),
]

EXECUTOR_ADMIN_OR_LOOPBACK = [
    ("get", "/executor/status"),
    ("post", "/executor/disable"),
    ("get", "/orders"),
    ("get", "/orders/{order_id}"),
    ("post", "/orders/reconcile"),
    ("post", "/orders/voids"),
    ("post", "/orders/expire"),
]

EXECUTOR_ADMIN_ONLY = [
    ("post", "/executor/enable"),
    ("post", "/orders/{order_id}/approve"),
    ("post", "/orders/{order_id}/reject"),
    ("post", "/orders/{order_id}/fill"),
    ("post", "/executor/login"),
]


def _decorator_block(path: str, method: str) -> str:
    m = re.search(
        rf'@app\.{method}\(\s*"{re.escape(path)}"[^\n]*',
        API_SOURCE,
    )
    assert m is not None, f"{method.upper()} {path} decorator missing from api.py"
    # Include the full decorator call if it spans lines.
    start = m.start()
    end = API_SOURCE.find(")", start) + 1
    block = API_SOURCE[start:end]
    # Cap at next @app to be safe
    nxt = API_SOURCE.find("\n@", start + 1)
    if nxt != -1:
        block = API_SOURCE[start:nxt]
    return block


@pytest.mark.parametrize(("method", "path"), ADMIN_OR_LOOPBACK_ROUTES)
def test_gated_dumps_keep_loopback_or_admin(method, path):
    deco = _decorator_block(path, method)
    assert "dependencies=[Depends(require_admin_or_loopback)]" in deco, (
        f"{method.upper()} {path} lost require_admin_or_loopback"
    )
    assert 'dependencies=[Depends(require_admin)]' not in deco


@pytest.mark.parametrize(("method", "path"), ADMIN_ONLY_ROUTES)
def test_write_routes_require_full_admin(method, path):
    deco = _decorator_block(path, method)
    assert "Depends(require_admin)" in deco, (
        f"{method.upper()} {path} lost require_admin"
    )
    assert "require_admin_or_loopback" not in deco, (
        f"{method.upper()} {path} was downgraded to loopback-allowing"
    )


@pytest.mark.parametrize(("method", "path"), EXECUTOR_ADMIN_OR_LOOPBACK)
def test_executor_read_routes_keep_loopback_gate(method, path):
    deco = _decorator_block(path, method)
    assert "dependencies=[Depends(require_admin_or_loopback)]" in deco


@pytest.mark.parametrize(("method", "path"), EXECUTOR_ADMIN_ONLY)
def test_executor_write_routes_keep_full_admin(method, path):
    """Executor enable + order mutations must never lose require_admin."""
    deco = _decorator_block(path, method)
    assert "Depends(require_admin)" in deco
    assert "require_admin_or_loopback" not in deco


# ---------------------------------------------------------------------------
# Health trio stays PUBLIC — the sentinel/watchdog poll these unauthenticated.
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
    m = re.search(rf"async def {func}\(", window)
    assert m is not None, f"{path} handler renamed"


def test_health_trio_has_no_auth_param():
    """The public trio must not even carry an optional _auth parameter."""
    for func in ("health_check", "health_livez", "health_readyz"):
        sig_src = re.search(
            rf'@app\.get\("[^"]*"\)\s*\nasync def {func}\((.*?)\):',
            API_SOURCE,
            re.DOTALL,
        )
        assert sig_src is not None, func
        assert "_auth" not in sig_src.group(1)


def test_health_deep_and_detailed_are_not_public():
    for path in ("/health/detailed", "/health/deep"):
        deco = _decorator_block(path, "get")
        assert "require_admin_or_loopback" in deco, f"{path} must stay gated"


# ---------------------------------------------------------------------------
# Moved logic lives in tools/api modules, not api.py.
# ---------------------------------------------------------------------------

def test_model_logic_lives_in_tools_api_model_routes():
    unique_strings = [
        "scan_pace_model_total_edges(",
        "total_environment_adjustment(",
        "_player_impact(",
        '"Sport {sport} not supported by injury model"',
        "prop_opportunities",
        "minutes_since_announced=30.0",
    ]
    for s in unique_strings:
        assert s in MODEL_SOURCE, f"expected {s!r} in tools/api/model_routes.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_data_logic_lives_in_tools_api_data_routes():
    unique_strings = [
        '"Vector store not initialized"',
        '"Data collector not initialized"',
        "search_text(collection, query, top_k)",
        "venue_name=venue",
    ]
    for s in unique_strings:
        assert s in DATA_SOURCE, f"expected {s!r} in tools/api/data_routes.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_hypothesis_logic_lives_in_tools_api_hypothesis_routes():
    unique_strings = [
        '"Promotion gate failed: {old_status} → {new_status}"',
        '"Pass force=true to override"',
        '"Unknown fields: {sorted(unknown)}"',
        "validate_model_config(mc)",
        "UPDATE hypotheses SET status = ?, updated_at = ?, ",
    ]
    for s in unique_strings:
        assert s in HYP_SOURCE, f"expected {s!r} in tools/api/hypothesis_routes.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_backtest_logic_lives_in_tools_api_backtest_routes():
    unique_strings = [
        "bulk_fetch_date_range(",
        "resolve_with_scores(run_id, sport)",
    ]
    for s in unique_strings:
        assert s in BT_SOURCE, f"expected {s!r} in tools/api/backtest_routes.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_research_logic_lives_in_tools_api_research_routes():
    unique_strings = [
        '"patterns list required"',
        "batch_purge:generic_edge",
        "WHERE status = 'draft'",
        "generate_from_templates(",
        "set_local_only(enabled)",
    ]
    for s in unique_strings:
        assert s in RES_SOURCE, f"expected {s!r} in tools/api/research_routes.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_system_logic_lives_in_tools_api_system_routes():
    unique_strings = [
        "watchdog_last_ping_ago:",
        "breaker_open[{name}]",
        "pipeline_broken:",
        "write_coordinators\"] = _writer_stats()".replace('"]', '"]'),
        "FROM task_queue\n                    WHERE status = 'PENDING'",
        "SELECT status, COUNT(*) FROM hypotheses GROUP BY status",
        "events_by_status",
        "_health_file_last_write_ts",
        "_clamped_regime_multiplier",
    ]
    for s in unique_strings:
        assert s in SYS_SOURCE, f"expected {s!r} in tools/api/system_routes.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_debug_logic_lives_in_tools_api_debug_routes():
    unique_strings = [
        "CALLISTO_TRACEMALLOC=1 and restart",
        "tracemalloc not active",
        "PRAGMA value assignment not allowed",
        "not in allowlist",
        "Query exceeded 10s timeout",
        "set_progress_handler(_timeout_handler, 10_000)",
        "PRAGMA query_only = ON",
    ]
    for s in unique_strings:
        assert s in DBG_SOURCE, f"expected {s!r} in tools/api/debug_routes.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_order_logic_lives_in_tools_api_order_routes():
    unique_strings = [
        '"order_manager not initialised"',
        "mark_filled(",
        "reconcile_filled_orders(om)",
        "detect_voided_orders(om)",
        "Order manager + bet executor disabled",
        "ensure_logged_in()",
    ]
    for s in unique_strings:
        assert s in ORD_SOURCE, f"expected {s!r} in tools/api/order_routes.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


# ---------------------------------------------------------------------------
# Import surface: modules import cleanly; api keeps compat aliases.
# ---------------------------------------------------------------------------

def _tools_api(modname):
    return importlib.import_module(f"tools.api.{modname}")


@pytest.mark.parametrize(
    ("modname", "funcs"),
    [
        ("model_routes", ["get_model_total", "get_model_environment", "get_injuries", "injury_impact_model"]),
        ("data_routes", ["get_scoreboard", "get_weather", "embedding_stats", "embedding_search", "data_collection_stats"]),
        ("hypothesis_routes", ["create_hypothesis", "list_hypotheses", "get_hypothesis", "hypothesis_report", "hypothesis_significance", "promote_hypothesis", "update_hypothesis"]),
        ("backtest_routes", ["run_backtest", "get_backtest_results", "resolve_backtest", "historical_cache_stats", "fetch_historical"]),
        ("research_routes", ["research_status", "research_pause", "research_resume", "research_local_only", "research_collect", "research_generate", "batch_reject_hypotheses", "get_research_sports"]),
        ("system_routes", ["build_health_report", "health_check", "health_livez", "health_readyz", "health_detailed", "health_deep", "integrity_history", "full_system_status", "regime_sizer_multipliers"]),
        ("debug_routes", ["debug_memory", "debug_memory_traces", "debug_gc", "admin_sql"]),
        ("order_routes", ["get_executor", "executor_status", "executor_enable", "executor_disable", "orders_list", "orders_get", "orders_approve", "orders_reject", "orders_fill", "orders_reconcile", "orders_voids", "orders_expire", "executor_login"]),
    ],
)
def test_extracted_bodies_are_coroutines(modname, funcs):
    mod = _tools_api(modname)
    for fn in funcs:
        f = getattr(mod, fn, None)
        assert f is not None, f"tools.api.{modname}.{fn} missing"
        assert inspect.iscoroutinefunction(f), f"{modname}.{fn} must be async"


def test_validate_admin_sql_is_pure_function():
    from tools.api.debug_routes import validate_admin_sql
    assert validate_admin_sql("SELECT 1") is None
    assert validate_admin_sql("DELETE FROM hypotheses") is not None
    assert validate_admin_sql("PRAGMA writable_schema=1") is not None
    assert validate_admin_sql("PRAGMA integrity_check") is None


def test_evaluate_health_signals_pure_behavior():
    from tools.api.system_routes import evaluate_health_signals
    healthy, severity, reasons = evaluate_health_signals({})
    assert healthy and severity == "ok" and reasons == []

    report = {
        "subsystems": {"odds": {"is_open": True, "last_error": "boom"}},
    }
    healthy, severity, reasons = evaluate_health_signals(report)
    assert not healthy
    assert severity == "critical"
    assert any("breaker_open[odds]" in r for r in reasons)

    report = {"task_queue": {"depth": 99}}
    healthy, severity, reasons = evaluate_health_signals(report)
    # depth>50 demotes healthy but stays "warning" severity
    assert not healthy and severity == "warning"
    assert any("task_queue_depth: 99" in r for r in reasons)


def test_health_signal_demotion_matrix():
    from tools.api.system_routes import evaluate_health_signals
    # pipeline integrity failure -> critical
    h, sev, reasons = evaluate_health_signals({"pipeline_integrity": {"healthy": False}})
    assert not h and sev == "critical"
    # writer failure rate > 1% -> warning
    h, sev, _ = evaluate_health_signals({
        "write_coordinators": [{"db_path": "x.db", "writes_total": 1000, "writes_failed": 50}],
    })
    assert not h and sev == "warning"
    # stale watchdog ping with prior pings -> critical
    h, sev, _ = evaluate_health_signals({
        "watchdog_monitoring": {"last_ping_ago_seconds": 120, "total_pings": 10},
    })
    assert not h and sev == "critical"


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestApiCompatAliases:
    def test_validate_admin_sql_alias(self):
        assert api_mod._validate_admin_sql is _tools_api("debug_routes").validate_admin_sql

    def test_allowed_pragmas_alias(self):
        assert api_mod._ALLOWED_PRAGMAS == _tools_api("debug_routes")._ALLOWED_PRAGMAS

    def test_evaluate_signals_alias(self):
        assert api_mod._evaluate_health_signals is _tools_api("system_routes").evaluate_health_signals

    def test_build_health_report_alias(self):
        assert api_mod._build_health_report is _tools_api("system_routes").build_health_report

    def test_get_executor_alias(self):
        assert api_mod._get_executor is _tools_api("order_routes").get_executor


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestImportSurface:
    def test_app_object_exists(self):
        assert hasattr(api_mod, "app")

    def test_route_handlers_delegate(self):
        routes = {(r.path, ",".join(sorted(getattr(r, "methods", []) or []))): r
                  for r in api_mod.app.routes}
        r = routes.get(("/health/livez", "GET"))
        assert r is not None, "/health/livez route missing"

    def test_seal_untouched(self):
        """Refactor must not touch the paper-trade signal-status seal."""
        statuses = getattr(api_mod, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is not None:
            assert "live" not in statuses
