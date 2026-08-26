"""Source-contract + behavior tests for the slice-4 api.py split.

Pins that:
  * api.py still owns the FastAPI decorators with the original gating for
    every route whose body moved to tools/api in this slice
    (tools/api/boost_routes.py, tools/api/task_routes.py).
  * /health, /health/livez, /health/readyz stay PUBLIC (no admin dep) and
    /health/livez awaits _system_routes.health_livez() — never a bare
    coroutine.
  * Gated dumps (/health/detailed, /health/deep, debug/memory, admin/sql)
    still require admin-or-loopback or admin; executor gates untouched.
  * Facade wrappers in api.py stay thin: each extracted route's body is a
    single delegation expression into the tools.api module (plus docstring).
  * The paper-trade/live seal is untouched by this refactor.
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


BOOST_SOURCE = _read(os.path.join("tools", "api", "boost_routes.py"))
TASK_SOURCE = _read(os.path.join("tools", "api", "task_routes.py"))
SIM_SOURCE = _read(os.path.join("tools", "api", "simulate.py"))


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
# Route inventory: gating contract per route touched in slice 4.
# ---------------------------------------------------------------------------

ADMIN_OR_LOOPBACK_ROUTES = [
    ("get", "/task/{task_id}"),
    ("get", "/task/{task_id}/chain"),
    ("get", "/session/{session_id}"),
    ("get", "/world/{domain}"),
    ("get", "/tasks"),
    ("post", "/admin/restart"),
]

ADMIN_ONLY_ROUTES = [
    ("post", "/context/sync"),
]

BOOST_ADMIN_OR_LOOPBACK_ROUTES = [
    ("post", "/boosts/evaluate-fixed"),
    ("post", "/boosts/evaluate-percentage"),
    ("post", "/boosts/evaluate-free-bet"),
    ("post", "/boosts/hedge"),
    ("post", "/boosts/devig"),
    ("post", "/boosts/evaluate-parlay"),
]

EXECUTOR_ADMIN_ONLY_ROUTES = [
    ("post", "/executor/enable"),
    ("post", "/executor/login"),
    ("post", "/orders/{order_id}/approve"),
    ("post", "/orders/{order_id}/reject"),
    ("post", "/orders/{order_id}/fill"),
]

GATED_DUMP_ROUTES = [
    ("get", "/health/detailed"),
    ("get", "/health/deep"),
    ("get", "/debug/memory"),
    ("get", "/debug/memory/top-traces"),
    ("post", "/debug/memory/gc"),
    ("post", "/admin/sql"),
    ("get", "/admin/writer"),
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
def test_task_routes_keep_loopback_or_admin(method, path):
    deco = _decorator_block(path, method)
    assert "require_admin_or_loopback" in deco, (
        f"{method.upper()} {path} lost require_admin_or_loopback"
    )


@pytest.mark.parametrize(("method", "path"), ADMIN_ONLY_ROUTES)
def test_context_sync_requires_full_admin(method, path):
    """POST /context/sync keeps the strict require_admin gate."""
    deco = _decorator_block(path, method)
    assert "Depends(require_admin)" in deco
    assert "require_admin_or_loopback" not in deco


@pytest.mark.parametrize(("method", "path"), BOOST_ADMIN_OR_LOOPBACK_ROUTES)
def test_boost_routes_keep_loopback_or_admin(method, path):
    deco = _decorator_block(path, method)
    assert "dependencies=[Depends(require_admin_or_loopback)]" in deco, (
        f"{method.upper()} {path} lost require_admin_or_loopback"
    )
    assert 'Depends(require_admin)]' not in deco.replace(
        "Depends(require_admin_or_loopback)", ""
    ) or True  # loopback variant contains require_admin as a prefix; exact check:
    assert re.search(r"Depends\(require_admin\)(?!_or_loopback)", deco) is None


@pytest.mark.parametrize(("method", "path"), EXECUTOR_ADMIN_ONLY_ROUTES)
def test_executor_gates_untouched(method, path):
    deco = _decorator_block(path, method)
    assert re.search(r"Depends\(require_admin\)(?!_or_loopback)", deco), (
        f"{method.upper()} {path} lost strict require_admin"
    )
    assert "require_admin_or_loopback" not in deco


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
    """/health/livez must await the extracted coroutine — never return one."""
    m = re.search(
        r'@app\.get\("/health/livez"\).*?async def health_livez\(\).*?return (.*?)\n',
        API_SOURCE,
        re.DOTALL,
    )
    assert m is not None, "health_livez body not found"
    ret = m.group(1).strip()
    assert ret.startswith("await "), f"/health/livez must await its body, got: {ret!r}"
    assert "_system_routes.health_livez()" in ret
    # And the awaited target really is an async function.
    from tools.api import system_routes
    assert inspect.iscoroutinefunction(system_routes.health_livez)


def test_public_write_allowlist_unchanged():
    """POST /task and POST /context/sync remain on the public-write registry
    (context/sync is additionally hard-gated via require_admin)."""
    assert 'public_endpoint("POST", "/task")' in API_SOURCE
    assert 'public_endpoint("POST", "/context/sync")' in API_SOURCE


# ---------------------------------------------------------------------------
# Moved logic lives in tools/api modules, not api.py.
# ---------------------------------------------------------------------------

def test_boost_logic_lives_in_tools_api_boost_routes():
    unique_strings = [
        "class FixedBoostRequest(BaseModel):",
        "class PctBoostRequest(BaseModel):",
        "class FreeBetRequest(BaseModel):",
        "class HedgeRequest(BaseModel):",
        "class BoostedParlayRequest(BaseModel):",
        "evaluate_fixed_boost(",
        "evaluate_percentage_boost(",
        "evaluate_free_bet(",
        "calculate_hedge(",
        '"recommended": "multiplicative"',
        "[leg.dict() for leg in req.legs]",
    ]
    for s in unique_strings:
        assert s in BOOST_SOURCE, f"expected {s!r} in tools/api/boost_routes.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_task_logic_lives_in_tools_api_task_routes():
    unique_strings = [
        '"source": "wiki_short_circuit"',
        "CALLISTO_TASK_SHORT_CIRCUIT_THRESHOLD",
        "get_chain_tree(db, task_id)",
        'detail="Session not found")',
        "seal failed verification",
        "Invalid domain. Must be one of:",
        "ORDER BY created_at DESC LIMIT ?",
        "actionable_queries entries must be 1-20000 chars",
        'compare_digest(confirm, "YES")',
        "Watchdog will restart with new code",
        "Exiting for restart...",
    ]
    for s in unique_strings:
        assert s in TASK_SOURCE, f"expected {s!r} in tools/api/task_routes.py"
        assert s not in API_SOURCE, f"{s!r} should no longer be in api.py"


def test_task_route_schemas_defined_in_tools_api():
    for schema in (
        "TaskSubmission", "TaskResponse", "ContextSync",
    ):
        m = re.search(rf"class {schema}\(BaseModel\):", TASK_SOURCE)
        assert m is not None, f"{schema} schema should live in task_routes.py"
    # api.py keeps subclass facades only.
    assert "class TaskSubmission(_task_routes.TaskSubmission):" in API_SOURCE
    assert "class ContextSync(_task_routes.ContextSync):" in API_SOURCE


def test_boost_route_schemas_subclassed_in_api():
    for schema in (
        "FixedBoostRequest", "PctBoostRequest", "FreeBetRequest",
        "HedgeRequest", "BoostedParlayLeg", "BoostedParlayRequest",
        "DevigRequest",
    ):
        assert f"class {schema}(_boost_routes.{schema}):" in API_SOURCE, schema


# ---------------------------------------------------------------------------
# Facade wrappers stay thin: single delegation statement + docstring only.
# ---------------------------------------------------------------------------

def _handler_source(func_name: str) -> str:
    m = re.search(
        rf"async def {func_name}\(.*?(?=\n@|\n\n\n|\Z)",
        API_SOURCE,
        re.DOTALL,
    )
    assert m is not None, f"handler {func_name} not found in api.py"
    return m.group(0)


THIN_FACADES = [
    # (handler name, delegated-to expression fragment)
    ("submit_task", "_task_routes.submit_task(submission)"),
    ("get_task", "_task_routes.get_task(task_id)"),
    ("get_task_chain", "_task_routes.get_task_chain(task_id)"),
    ("get_session", "_task_routes.get_session(session_id)"),
    ("sync_context", "_task_routes.sync_context(ctx)"),
    ("eval_fixed_boost", "_boost_routes.eval_fixed_boost(req)"),
    ("eval_pct_boost", "_boost_routes.eval_pct_boost(req)"),
    ("eval_free_bet", "_boost_routes.eval_free_bet(req)"),
    ("hedge_calc", "_boost_routes.hedge_calc(req)"),
    ("devig", "_boost_routes.devig(req)"),
    ("eval_boosted_parlay", "_boost_routes.eval_boosted_parlay(req)"),
]


@pytest.mark.parametrize(("func", "delegation"), THIN_FACADES)
def test_facade_is_single_delegation(func, delegation):
    src = _handler_source(func)
    assert delegation in src, f"{func} does not delegate via {delegation}"
    # Count non-trivial statements inside the body: docstring + return only.
    tree = ast.parse(src)
    fn = tree.body[0]
    stmt_types = [type(s).__name__ for s in fn.body]
    assert stmt_types == ["Expr", "Return"], (
        f"{func} facade gained logic: {stmt_types}"
    )


def test_admin_restart_facade_registers_restart_sink():
    """/admin/restart delegates and passes the H-14 restart-task sink."""
    src = _handler_source("admin_restart")
    assert "_task_routes.admin_restart(" in src
    assert "set_restart_task=_set_restart_task" in src
    # The sink writes back into api._restart_task so shutdown can cancel it.
    assert "global _restart_task" in API_SOURCE


# ---------------------------------------------------------------------------
# Import surface: modules import cleanly; handlers are async coroutines.
# ---------------------------------------------------------------------------

def _tools_api(modname):
    return importlib.import_module(f"tools.api.{modname}")


@pytest.mark.parametrize(
    ("modname", "funcs"),
    [
        ("boost_routes", [
            "eval_fixed_boost", "eval_pct_boost", "eval_free_bet",
            "hedge_calc", "devig", "eval_boosted_parlay",
        ]),
        ("task_routes", [
            "submit_task", "get_task", "get_task_chain", "get_session",
            "query_world", "list_tasks", "sync_context", "admin_restart",
            "wiki_task_short_circuit",
        ]),
        ("simulate", [
            "simulate_portfolio_endpoint",
        ]),
    ],
)
def test_extracted_bodies_are_coroutines(modname, funcs):
    mod = _tools_api(modname)
    for fn in funcs:
        f = getattr(mod, fn, None)
        assert f is not None, f"tools.api.{modname}.{fn} missing"
        assert inspect.iscoroutinefunction(f), f"{modname}.{fn} must be async"


def test_boost_request_schemas_validate():
    from tools.api.boost_routes import (
        DevigRequest, HedgeRequest, BoostedParlayLeg, BoostedParlayRequest,
    )

    d = DevigRequest(odds_a=-110, odds_b=+100)
    assert d.odds_a == -110 and d.odds_b == 100

    h = HedgeRequest(boost_stake=50, boosted_odds=200, hedge_odds=-150,
                     fair_probability=0.5)
    assert h.boost_stake == 50

    leg = BoostedParlayLeg(american_odds=150, market="h2h")
    p = BoostedParlayRequest(legs=[leg], boosted_parlay_odds=400, sport="nba")
    assert p.max_stake == 100 and p.book == ""

    with pytest.raises(Exception):
        BoostedParlayRequest(legs=[], boosted_parlay_odds=400)  # sport required


def test_task_submission_schema_bounds():
    from tools.api.task_routes import TaskSubmission, ContextSync

    t = TaskSubmission(query="hello")
    assert t.priority == 0
    with pytest.raises(Exception):
        TaskSubmission(query="")
    with pytest.raises(Exception):
        TaskSubmission(query="x", priority=99)

    c = ContextSync(session_summary="s")
    assert c.actionable_queries == []
    with pytest.raises(Exception):
        ContextSync(session_summary="")


def test_wiki_short_circuit_returns_none_on_error(monkeypatch):
    """Any failure inside the short-circuit path returns None, never raises."""
    import tools.api.task_routes as tr

    monkeypatch.setenv("CALLISTO_TASK_SHORT_CIRCUIT_THRESHOLD", "not-a-float")
    result = asyncio_run(tr.wiki_task_short_circuit("any query"))
    assert result is None


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def test_devig_endpoint_body_math():
    """The devig body computes multiplicative + additive fair probabilities."""
    import asyncio as _aio
    from tools.api.boost_routes import DevigRequest, devig

    out = _aio.run(devig(DevigRequest(odds_a=-110, odds_b=-110)))
    assert out["recommended"] == "multiplicative"
    mult = out["multiplicative"]
    # Two equal -110 sides devig to ~0.5/0.5.
    assert abs(mult["side_a"] - 0.5) < 0.02
    assert abs(mult["side_a"] + mult["side_b"] - 1.0) < 0.01


def test_hedge_endpoint_body_math():
    import asyncio as _aio
    from tools.api.boost_routes import HedgeRequest, hedge_calc

    out = _aio.run(hedge_calc(HedgeRequest(
        boost_stake=100, boosted_odds=200, hedge_odds=-150,
        fair_probability=2 / 3,
    )))
    assert isinstance(out, dict)


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestApiCompatAliases:
    def test_wiki_short_circuit_alias(self):
        assert api_mod._wiki_task_short_circuit is _tools_api(
            "task_routes"
        ).wiki_task_short_circuit

    def test_boost_module_imported_on_api(self):
        assert api_mod._boost_routes is _tools_api("boost_routes")

    def test_task_module_imported_on_api(self):
        assert api_mod._task_routes is _tools_api("task_routes")

    def test_seal_untouched(self):
        """Refactor must not touch the paper-trade signal-status seal."""
        statuses = getattr(api_mod, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is not None:
            assert "live" not in statuses


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestImportSurface:
    def test_app_object_exists(self):
        assert hasattr(api_mod, "app")

    def test_all_boost_routes_registered(self):
        paths = {r.path for r in api_mod.app.routes}
        for p in (
            "/boosts/evaluate-fixed", "/boosts/evaluate-percentage",
            "/boosts/evaluate-free-bet", "/boosts/hedge", "/boosts/devig",
            "/boosts/evaluate-parlay",
        ):
            assert p in paths, f"{p} route missing"

    def test_health_livez_route_present_and_async(self):
        routes = {(r.path, ",".join(sorted(getattr(r, "methods", []) or []))): r
                  for r in api_mod.app.routes}
        r = routes.get(("/health/livez", "GET"))
        assert r is not None, "/health/livez route missing"
        assert inspect.iscoroutinefunction(r.endpoint)

    def test_health_livez_returns_alive_dict_when_called_directly(self):
        """Calling the endpoint body directly yields JSON {'alive': True}."""
        import asyncio
        result = asyncio.run(api_mod.health_livez())
        assert isinstance(result, dict)
        assert result.get("alive") is True
