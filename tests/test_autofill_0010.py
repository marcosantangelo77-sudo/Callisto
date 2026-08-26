"""autofill #0010 — public health trio characterization.

Pins the PUBLIC health surface of api.py:
  * GET /health        — comprehensive Layer-2 report; polled by sentinel/watchdog.
  * GET /health/livez  — k8s-style liveness.
  * GET /health/readyz — k8s-style readiness (503 when degraded).

These three must NEVER gain require_admin / require_admin_or_loopback /
signature _auth gating. Sentinel + watchdog + k8s probes depend on anonymous
access; adding an admin dep would blind every resilience layer at once.

The richer diagnostics stay gated and that is pinned too:
  * /health/detailed                — require_admin_or_loopback
  * /health/deep                    — require_admin_or_loopback
  * /health/integrity/history       — require_admin_or_loopback

Safety rails also characterized here (fail-closed posture):
  * _PAPER_TRADE_SIGNAL_STATUSES never contains "live".
  * generate_paper_trade_signal is never widened to status == "live".

Tests-only module. No production file is modified by these tests.
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


SYSTEM_ROUTES_SOURCE = _read(os.path.join("tools", "api", "system_routes.py"))

PUBLIC_TRIO = [
    ("/health", "health_check"),
    ("/health/livez", "health_livez"),
    ("/health/readyz", "health_readyz"),
]

GATED_HEALTH_ROUTES = [
    ("/health/detailed", "require_admin_or_loopback"),
    ("/health/deep", "require_admin_or_loopback"),
    ("/health/integrity/history", "require_admin_or_loopback"),
]

GATING_TOKENS = [
    "require_admin",
    "require_admin_or_loopback",
    "_auth",
]


# ---------------------------------------------------------------------------
# Source-window helpers
# ---------------------------------------------------------------------------


def _route_block(path: str) -> str:
    """Return the decorator+handler block for @app.get("<path>") in api.py."""
    i = API_SOURCE.find(f'@app.get("{path}"')
    assert i != -1, f'GET {path} missing from api.py'
    j = API_SOURCE.find("\n@app.", i)
    return API_SOURCE[i : j if j != -1 else len(API_SOURCE)]


def _decorator_line(path: str) -> str:
    m = re.search(rf'@app\.get\(\s*"{re.escape(path)}"[^\n]*', API_SOURCE)
    assert m is not None, f"decorator for GET {path} not found"
    return m.group(0)


# ---------------------------------------------------------------------------
# 1. The public trio stays public — decorator-level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "handler"), PUBLIC_TRIO)
def test_trio_decorator_has_no_dependencies_argument(path, handler):
    deco = _decorator_line(path)
    assert "dependencies=" not in deco, (
        f"{path} gained a dependencies= argument on its decorator"
    )


@pytest.mark.parametrize(("path", "handler"), PUBLIC_TRIO)
def test_trio_route_block_free_of_gating_tokens(path, handler):
    block = _route_block(path)
    for token in GATING_TOKENS:
        assert token not in block, (
            f"{path} route block contains gating token {token!r}; "
            "the sentinel/watchdog/k8s probes need it anonymous"
        )


@pytest.mark.parametrize(("path", "handler"), PUBLIC_TRIO)
def test_trio_handler_is_async(path, handler):
    block = _route_block(path)
    assert re.search(rf"async def {re.escape(handler)}\(", block), (
        f"{path} handler {handler} is missing or no longer async"
    )


@pytest.mark.parametrize(("path", "handler"), PUBLIC_TRIO)
def test_trio_handler_takes_no_request_auth_params(path, handler):
    """Handlers must not sprout Request/API-key parameters to hand-roll auth."""
    block = _route_block(path)
    sig = re.search(rf"async def {re.escape(handler)}\(([^)]*)\)", block)
    assert sig is not None
    params = sig.group(1)
    for bad in ("Request", "api_key", "token", "secret", "x_admin"):
        assert bad.lower() not in params.lower(), (
            f"{path} handler param {bad!r} smells like hand-rolled auth"
        )


def test_health_docstring_pins_public_contract():
    block = _route_block("/health")
    assert "PUBLIC" in block, "/health docstring should document its public contract"


def test_livez_docstring_pins_public_contract():
    block = _route_block("/health/livez")
    assert "PUBLIC" in block


def test_readyz_docstring_pins_public_contract():
    block = _route_block("/health/readyz")
    assert "PUBLIC" in block


# ---------------------------------------------------------------------------
# 2. Gated diagnostics stay gated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "dep"), GATED_HEALTH_ROUTES)
def test_gated_health_routes_keep_their_dep(path, dep):
    deco = _decorator_line(path)
    assert f"Depends({dep})" in deco, f"GET {path} lost its {dep} gate"


@pytest.mark.parametrize(("path", "dep"), GATED_HEALTH_ROUTES)
def test_gated_health_routes_not_downgraded_to_public(path, dep):
    block = _route_block(path)
    assert dep in block, f"GET {path} must keep {dep} somewhere in its block"


def test_no_new_public_health_sibling_routes():
    """Every /health* route is one of the known six — no unreviewed additions."""
    found = set(re.findall(r'@app\.get\("(/health[^"]*)"', API_SOURCE))
    known = {
        "/health",
        "/health/livez",
        "/health/readyz",
        "/health/detailed",
        "/health/deep",
        "/health/integrity/history",
    }
    extra = found - known
    assert not extra, f"unexpected new health routes appeared: {sorted(extra)}"


# ---------------------------------------------------------------------------
# 3. Live behavior of the trio through FastAPI's route table
# ---------------------------------------------------------------------------

try:
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    api_mod = importlib.import_module("api") if "api" not in sys.modules else sys.modules["api"]
except Exception as _e:  # pragma: no cover - environment-dependent
    api_mod = None
    _API_IMPORT_ERROR = str(_e)


def _skip_if_no_api():
    if api_mod is None:
        pytest.skip(f"api module unavailable in this env: {_API_IMPORT_ERROR}")


def _find_route(app, path):
    for r in app.routes:
        if getattr(r, "path", None) == path and "GET" in getattr(r, "methods", set()):
            return r
    return None


@pytest.mark.parametrize(("path", "handler"), PUBLIC_TRIO)
def test_trio_route_table_entry_has_zero_dependencies(path, handler):
    _skip_if_no_api()
    route = _find_route(api_mod.app, path)
    assert route is not None, f"{path} not registered as a GET route"
    assert list(getattr(route, "dependencies", []) or []) == [], (
        f"{path} registered with dependencies in the route table"
    )


@pytest.mark.parametrize(("path", "handler"), PUBLIC_TRIO)
def test_trio_route_table_handler_names(path, handler):
    _skip_if_no_api()
    route = _find_route(api_mod.app, path)
    assert route is not None
    assert route.endpoint.__name__ == handler


@pytest.mark.parametrize(("path", "_"), [(p, h) for p, h in GATED_HEALTH_ROUTES])
def test_gated_routes_have_dependency_objects_in_table(path, _):
    _skip_if_no_api()
    route = _find_route(api_mod.app, path)
    assert route is not None, f"{path} not registered"
    assert len(list(getattr(route, "dependencies", []) or [])) >= 1, (
        f"{path} lost its dependency objects in the route table"
    )


# ---------------------------------------------------------------------------
# 4. Handler semantics via tools.api.system_routes
# ---------------------------------------------------------------------------


def test_system_routes_exposes_trio_helpers():
    from tools.api import system_routes as sr
    for fn in ("health_livez", "health_readyz", "build_health_report"):
        assert hasattr(sr, fn), f"system_routes lost {fn}"


def test_livez_shape():
    from tools.api import system_routes as sr
    payload = asyncio.run(sr.health_livez())
    assert isinstance(payload, dict)
    assert payload.get("alive") is True
    assert "ts" in payload


def test_readyz_fails_closed_when_unhealthy():
    """With no health monitor initialized, readiness must be a hard 503."""
    from fastapi import HTTPException

    from tools.api import system_routes as sr
    with pytest.raises(HTTPException) as ei:
        asyncio.run(sr.health_readyz())
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_build_health_report_reports_unhealthy_without_monitor():
    from tools.api import system_routes as sr
    payload = await sr.build_health_report()
    assert isinstance(payload, dict)
    assert payload.get("healthy") is False
    assert "severity" in payload and "reasons" in payload


# ---------------------------------------------------------------------------
# 5. Fail-closed safety rails (paper-trade / live betting)
# ---------------------------------------------------------------------------


def test_paper_trade_statuses_source_exists():
    paper_src = _read(os.path.join("tools", "signals", "paper.py"))
    assert "_PAPER_TRADE_SIGNAL_STATUSES" in paper_src


def test_extracted_statuses_never_contain_live():
    paper_src = _read(os.path.join("tools", "signals", "paper.py"))
    m = re.search(
        r"_PAPER_TRADE_SIGNAL_STATUSES\s*(?::[^=]+)?=\s*frozenset\s*\(\{.*?\}\)|"
        r"_PAPER_TRADE_SIGNAL_STATUSES\s*(?::[^=]+)?=\s*([\[{].*?[\]}])",
        paper_src,
        re.DOTALL,
    )
    assert m is not None, "could not locate _PAPER_TRADE_SIGNAL_STATUSES literal"
    literal = m.group(0)
    statuses = set(re.findall(r'"([^"]+)"', literal)) | set(
        re.findall(r"'([^']+)'", literal)
    )
    assert "live" not in {s.lower() for s in statuses}, (
        "'live' must never appear in _PAPER_TRADE_SIGNAL_STATUSES"
    )
    assert statuses, "status set should not be emptied either"


def test_statuses_is_frozenset_so_it_cannot_be_mutated_to_add_live():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as st
    assert isinstance(st, frozenset)
    with pytest.raises(AttributeError):
        st.add("live")
    assert "live" not in st
    assert "live" not in {s.lower() for s in st}


def test_generate_paper_trade_signal_not_widened_to_live():
    """No code path may gate generate_paper_trade_signal on status=='live'."""
    paper_src = _read(os.path.join("tools", "signals", "paper.py"))
    betexec_src = _read(os.path.join("tools", "betexec", "__init__.py"))
    for src_name, src in (("paper.py", paper_src), ("betexec", betexec_src), ("api.py", API_SOURCE)):
        if "generate_paper_trade_signal" not in src:
            continue
        for m in re.finditer(r"generate_paper_trade_signal", src):
            window = src[max(0, m.start() - 200) : m.end() + 300]
            for bad in ('== "live"', "== 'live'", '!= "paper_trading"'):
                assert bad not in window, (
                    f"{src_name}: suspicious comparison near "
                    "generate_paper_trade_signal: {bad!r}"
                )


def test_generate_paper_trade_signal_signature_unchanged():
    from inspect import signature

    from tools.signals import paper
    fn = getattr(paper, "generate_paper_trade_signal", None)
    if fn is None:
        pytest.skip("generate_paper_trade_signal not exposed by tools.signals.paper")
    sig = str(signature(fn))
    assert "live" not in sig.lower(), (
        "generate_paper_trade_signal grew a live-related parameter"
    )


def test_livez_never_reports_armed_betting():
    from tools.api import system_routes as sr
    src = inspect.getsource(sr.health_livez)
    assert "armed" not in src.lower()
    assert "bet" not in src.lower(), "liveness probe drifted into betting territory"


# ---------------------------------------------------------------------------
# 6. Structural AST pins — the decorators are exactly what we think they are
# ---------------------------------------------------------------------------


def _ast_decorators_for(path_literal: str):
    tree = ast.parse(API_SOURCE)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                try:
                    rendered = ast.unparse(dec)
                except Exception:  # pragma: no cover
                    continue
                if path_literal in rendered and ".get(" in rendered:
                    out.append((node.name, rendered))
    return out


@pytest.mark.parametrize(("path", "handler"), PUBLIC_TRIO)
def test_ast_decorator_of_public_trio_is_bare(path, handler):
    hits = _ast_decorators_for(path)
    matches = [h for h in hits if h[0] == handler]
    assert len(matches) == 1, f"expected exactly one decorator for {handler}, got {hits}"
    _, rendered = matches[0]
    assert "Depends" not in rendered and "dependencies" not in rendered


@pytest.mark.parametrize(("path", "dep"), GATED_HEALTH_ROUTES)
def test_ast_decorator_of_gated_keeps_dep(path, dep):
    hits = _ast_decorators_for(path)
    assert hits, f"{path} not found via AST scan"
    assert any(dep in rendered for _, rendered in hits)


def test_api_parses_as_valid_python():
    ast.parse(API_SOURCE)  # sanity: our regexes operate on parseable source


# ---------------------------------------------------------------------------
# 7. Regression net: watchdog/sentinel expectations documented in code
# ---------------------------------------------------------------------------


def test_debounce_constant_present():
    """The /health disk-write debounce machinery is part of the public contract."""
    assert "_HEALTH_FILE_DEBOUNCE_SECONDS" in API_SOURCE
    assert "_HEALTH_FILE_LAST_WRITE_TS" in API_SOURCE


def test_health_file_write_offloaded_to_thread():
    block = _route_block("/health")
    assert "asyncio.to_thread(system_health.write_health_file)" in block, (
        "/health must keep sync JSON IO off the event loop"
    )


def test_health_never_raises_on_disk_write_failure():
    block = _route_block("/health")
    assert "except Exception" in block, (
        "/health must swallow health-file write failures (never fail the probe)"
    )


def test_readyz_uses_system_routes_helper():
    block = _route_block("/health/readyz")
    assert "_system_routes.health_readyz()" in block


def test_livez_uses_system_routes_helper():
    block = _route_block("/health/livez")
    assert "_system_routes.health_livez()" in block
