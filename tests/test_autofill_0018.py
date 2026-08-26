"""Autofill characterization #0018 — public health trio (LONG).

Characterization pins for Callisto's public health surface:

  * /health, /health/livez, /health/readyz stay PUBLIC: no require_admin,
    no require_admin_or_loopback, no Depends(...) of any auth flavor, and
    no _auth parameter on their handler signatures.
  * /health/detailed and /health/deep REMAIN gated behind
    require_admin_or_loopback (defense in depth is allowed to keep them).
  * The public trio's handler bodies live in tools/api/system_routes.py
    and keep their documented semantics (livez always alive; readyz
    demotes to HTTPException 503 when unhealthy).
  * The paper-trade hard gate stays closed: "live" is never in
    _PAPER_TRADE_SIGNAL_STATUSES and generate_paper_trade_signal
    refuses non-paper statuses.

Tests-only module. Nothing here arms live betting; the fail-closed pins
at the bottom assert the gate stays SHUT.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(REPO, "api.py")) as _f:
    API_SOURCE = _f.read()

with open(os.path.join(REPO, "tools", "api", "system_routes.py")) as _f:
    SYSTEM_ROUTES_SOURCE = _f.read()


def _load_api_module():
    if "api" in sys.modules:
        return sys.modules["api"]
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    return importlib.import_module("api")


try:
    api_mod = _load_api_module()
except Exception as _import_err:
    api_mod = None
    _IMPORT_ERR = str(_import_err)
else:
    _IMPORT_ERR = ""


# ---------------------------------------------------------------------------
# Source-window helpers
# ---------------------------------------------------------------------------

PUBLIC_TRIO = ("/health", "/health/livez", "/health/readyz")
GATED_HEALTH = ("/health/detailed", "/health/deep")


def _decorator_window(path: str) -> str:
    """Source from the @app.get decorator for `path` to the next top-level @."""
    i = API_SOURCE.find(f'@app.get("{path}"')
    assert i != -1, f"{path} missing from api.py"
    j = API_SOURCE.find("\n@", i)
    if j == -1:
        j = len(API_SOURCE)
    return API_SOURCE[i:j]


def _handler_name(path: str) -> str:
    m = re.search(r"^async def (\w+)\(", _decorator_window(path), re.M)
    assert m, f"no async handler found for {path}"
    return m.group(1)


# ---------------------------------------------------------------------------
# Part 1 — decorators: no admin dep anywhere in the public trio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_trio_decorator_has_no_require_admin(path):
    window = _decorator_window(path)
    assert "require_admin" not in window, (
        f"{path} must never gain require_admin / require_admin_or_loopback"
    )


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_trio_decorator_has_no_depends_auth(path):
    window = _decorator_window(path)
    assert "Depends(" not in window, f"{path} must carry no Depends() deps"


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_trio_decorator_is_plain_get(path):
    window = _decorator_window(path)
    assert window.startswith(f'@app.get("{path}")'), (
        f"{path} must be a plain @app.get with no arguments"
    )


def test_no_auth_token_reads_in_trio_handlers():
    for path in PUBLIC_TRIO:
        window = _decorator_window(path)
        assert "HTTPAuthorizationCredentials" not in window
        assert "_bearer_scheme" not in window
        assert "CALLISTO_ADMIN_TOKEN" not in window


# ---------------------------------------------------------------------------
# Part 2 — handler signatures: no _auth parameter on the public trio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_trio_handler_signature_has_no_auth_params(path):
    name = _handler_name(path)
    i = API_SOURCE.find(f"async def {name}(")
    j = API_SOURCE.find("):", i)
    sig = API_SOURCE[i : j + 2]
    assert "_auth" not in sig, f"{name} must not take an auth parameter"
    assert "Request" not in sig or name == "health_check", (
        f"{name} should not need the Request object"
    )


@pytest.mark.parametrize(
    "path,name",
    [("/health", "health_check"), ("/health/livez", "health_livez"), ("/health/readyz", "health_readyz")],
)
def test_trio_handler_names_pinned(path, name):
    """Characterization: handler names stay stable (sentinel/monitor rely on them)."""
    assert _handler_name(path) == name


# ---------------------------------------------------------------------------
# Part 3 — gated health endpoints STAY gated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", GATED_HEALTH)
def test_gated_health_keeps_loopback_gate(path):
    window = _decorator_window(path)
    assert "Depends(require_admin_or_loopback)" in window, (
        f"{path} must remain behind require_admin_or_loopback"
    )


def test_detailed_and_deep_are_not_public():
    for path in GATED_HEALTH:
        window = _decorator_window(path)
        # The decorator line itself carries the gate (not just a comment).
        first_line = window.splitlines()[0]
        assert "require_admin_or_loopback" in first_line


def test_health_integrity_history_stays_gated():
    window = _decorator_window("/health/integrity/history")
    assert "require_admin_or_loopback" in window.splitlines()[0]


# ---------------------------------------------------------------------------
# Part 4 — AST pin: no security dependency objects on the trio's routes
# ---------------------------------------------------------------------------


def _route_deps_from_ast():
    tree = ast.parse(API_SOURCE)
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            src = ast.unparse(dec)
            m = re.match(r"app\.(get|post)\(\s*['\"]([^'\"]+)['\"]", src)
            if not m:
                continue
            method, path = m.groups()
            deps = []
            if len(dec.args) > 1:  # dependencies=[...] kwarg position
                deps = ast.unparse(dec.args[1])
            kw = [ast.unparse(k.value) for k in dec.keywords]
            found.setdefault((method, path), []).append((deps, kw))
    return found


def test_ast_trio_routes_have_zero_dependencies():
    routes = _route_deps_from_ast()
    for path in PUBLIC_TRIO:
        entries = routes.get(("get", path))
        assert entries, f"{path} route not found via AST parse"
        for deps, kws in entries:
            assert "dependencies" not in str(kws), f"{path} gained a dependencies kwarg"
            assert "require" not in deps and "require" not in str(kws)


def test_ast_gated_routes_carry_gate_kwarg():
    routes = _route_deps_from_ast()
    for path in GATED_HEALTH + ("/health/integrity/history",):
        entries = routes.get(("get", path))
        assert entries, f"{path} route not found via AST parse"
        assert any("require_admin_or_loopback" in str(kws) for _, kws in entries), (
            f"{path} lost its gate kwarg"
        )


# ---------------------------------------------------------------------------
# Part 5 — live app object pins (skip-soft when api.py can't import here)
# ---------------------------------------------------------------------------


def _require_api_mod():
    if api_mod is None:
        pytest.skip(f"api module unavailable in test env: {_IMPORT_ERR}")
    return api_mod


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_live_app_route_has_empty_dependencies(path):
    mod = _require_api_mod()
    route = next(r for r in mod.app.routes if getattr(r, "path", None) == path)
    assert list(route.dependencies) == [], f"{path} must have zero route dependencies"


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_live_app_route_methods_and_name(path):
    mod = _require_api_mod()
    route = next(r for r in mod.app.routes if getattr(r, "path", None) == path)
    assert "GET" in route.methods
    fn = route.endpoint
    code = inspect.getsource(fn)
    assert "_auth" not in code, f"{path} endpoint body/signature mentions _auth"


@pytest.mark.parametrize("path", GATED_HEALTH + ("/health/integrity/history",))
def test_live_app_gated_route_dependencies(path):
    mod = _require_api_mod()
    route = next(r for r in mod.app.routes if getattr(r, "path", None) == path)
    names = [getattr(d.dependency, "__name__", "") for d in route.dependencies]
    assert "require_admin_or_loopback" in names, f"{path} lost its runtime gate"


def test_live_app_no_require_admin_on_any_health_path():
    mod = _require_api_mod()
    for r in mod.app.routes:
        p = getattr(r, "path", "")
        if p == "/health" or p.startswith("/health/"):
            for d in r.dependencies:
                assert getattr(d.dependency, "__name__", "") != "require_admin", (
                    f"{p} must never sit behind hard require_admin"
                )


# ---------------------------------------------------------------------------
# Part 6 — handler bodies in tools/api/system_routes.py keep their semantics
# ---------------------------------------------------------------------------


def test_system_routes_docstring_pins_publicity():
    header = SYSTEM_ROUTES_SOURCE[:2000]
    assert "/health, /health/livez, /health/readyz stay PUBLIC" in header
    assert "stay require_admin_or_loopback gated" in header


@pytest.mark.parametrize(
    "fn_name",
    ["health_check", "health_livez", "health_readyz"],
)
def test_system_routes_public_bodies_auth_free(fn_name):
    mod = importlib.import_module("tools.api.system_routes")
    fn = getattr(mod, fn_name)
    src = inspect.getsource(fn)
    assert "require_admin" not in src
    assert "_auth" not in src
    assert inspect.iscoroutinefunction(fn)


def test_health_livez_always_alive():
    mod = importlib.import_module("tools.api.system_routes")
    result = asyncio_run_if_needed(mod.health_livez())
    assert result["alive"] is True
    assert isinstance(result["ts"], float)


def test_health_readyz_healthy_shape():
    mod = importlib.import_module("tools.api.system_routes")

    async def fake_report():
        return {"healthy": True, "severity": "ok", "reasons": [], "uptime_seconds": 42.0}

    orig = mod.build_health_report
    mod.build_health_report = fake_report
    try:
        result = asyncio_run_if_needed(mod.health_readyz())
    finally:
        mod.build_health_report = orig
    assert result == {
        "ready": True,
        "severity": "ok",
        "uptime_seconds": 42.0,
    }


def test_health_readyz_demotes_to_503_when_unhealthy():
    mod = importlib.import_module("tools.api.system_routes")
    from fastapi import HTTPException

    async def fake_report():
        return {"healthy": False, "severity": "critical", "reasons": ["db_down"]}

    orig = mod.build_health_report
    mod.build_health_report = fake_report
    try:
        with pytest.raises(HTTPException) as excinfo:
            asyncio_run_if_needed(mod.health_readyz())
    finally:
        mod.build_health_report = orig
    assert excinfo.value.status_code == 503
    detail = excinfo.value.detail
    assert detail["ready"] is False
    assert detail["severity"] == "critical"
    assert "db_down" in detail["reasons"]


def test_evaluate_health_signals_returns_triple():
    mod = importlib.import_module("tools.api.system_routes")
    out = mod.evaluate_health_signals({"healthy": True})
    assert isinstance(out, tuple) and len(out) == 3


# ---------------------------------------------------------------------------
# Helpers / small utilities
# ---------------------------------------------------------------------------


def asyncio_run_if_needed(coro):
    import asyncio

    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    if loop is not None:
        # Inside a running loop (unlikely here): run in a fresh thread's loop.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, coro).result(timeout=30)
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Part 7 — fail-closed pins: the paper-trade hard gate stays SHUT
# ---------------------------------------------------------------------------

PAPER_MOD_PATH = os.path.join(REPO, "tools", "signals", "paper.py")
with open(PAPER_MOD_PATH) as _f:
    PAPER_SOURCE = _f.read()


def test_paper_statuses_frozenset_only_paper_trading():
    from tools.signals.paper import allowed_paper_statuses

    assert allowed_paper_statuses() == frozenset({"paper_trading"})


@pytest.mark.parametrize("bad", ["live", "LIVE", "paper", "", None, "production"])
def test_reject_non_paper_refuses_everything_else(bad):
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper(bad) is True


def test_reject_non_paper_accepts_only_paper_trading():
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper("paper_trading") is False


def test_no_live_string_in_status_frozenset_line():
    m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\(([^)]*)\)", PAPER_SOURCE)
    assert m, "gate definition missing from tools/signals/paper.py"
    literal = m.group(1).strip().strip("{}").replace('"', "").replace("'", "")
    tokens = {t.strip() for t in literal.split(",") if t.strip()}
    assert tokens == {"paper_trading"}, f"unexpected statuses in gate: {tokens}"


def test_generate_paper_trade_signal_gated_in_backtest_source():
    with open(os.path.join(REPO, "tools", "backtest.py")) as f:
        bt = f.read()
    i = bt.find("async def generate_paper_trade_signal(")
    assert i != -1
    window = bt[i : i + 4000]
    assert "reject_non_paper" in window, (
        "generate_paper_trade_signal must call reject_non_paper before odds processing"
    )
    assert 'h["status"]' in window or "status" in window


def test_api_slice2_pin_still_declares_public_trio():
    """Cross-check: the existing slice-2 pin file agrees with this characterization."""
    with open(os.path.join(REPO, "tests", "test_api_slice2.py")) as f:
        s2 = f.read()
    for p in PUBLIC_TRIO:
        assert f'"{p}"' in s2
