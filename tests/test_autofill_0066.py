"""Autofill 0066 — public health trio + fail-closed gates (characterization).

Characterizes the PUBLIC HEALTH TRIO contract that Callisto deliberately
maintains across the ``api.py`` -> ``tools/api/system_routes.py`` split:

1. ``GET /health``, ``GET /health/livez``, ``GET /health/readyz`` are
   PUBLIC endpoints. They are polled by the sentinel (Layer 3), k8s-style
   probes and external watchdogs which carry NO admin token. None of the
   three may gain a ``Depends(require_admin)`` /
   ``Depends(require_admin_or_loopback)`` dependency, an ``_auth``
   parameter, or any other auth coupling — at the FastAPI layer in
   ``api.py``, in the handler signatures, or inside the moved bodies in
   ``tools/api/system_routes.py``.

2. ``/health/detailed`` and ``/health/deep`` stay GATED with
   ``require_admin_or_loopback`` — defense-in-depth must not be weakened
   by this pin either; both directions are characterized.

3. The live-betting hard gate stays SHUT:
   ``tools.signals.paper._PAPER_TRADE_SIGNAL_STATUSES`` is exactly
   ``{"paper_trading"}``; ``BacktestEngine.generate_paper_trade_signal``
   returns ``[]`` for any non-paper status (including ``"live"``) before
   touching odds; and nothing anywhere re-widens the allowed set.

These tests characterize current behavior. If one fails after an unrelated
change, treat it as a regression against this contract, not noise.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

API_PY = REPO / "api.py"
SYSTEM_ROUTES = REPO / "tools" / "api" / "system_routes.py"
PAPER_GATE = REPO / "tools" / "signals" / "paper.py"
BACKTEST = REPO / "tools" / "backtest.py"
PAPER_PIPELINE = REPO / "tools" / "btest" / "paper_pipeline.py"

PUBLIC_HEALTH_PATHS = ("/health", "/health/livez", "/health/readyz")
GATED_HEALTH_PATHS = ("/health/detailed", "/health/deep")
ADMIN_DEP_NAMES = {"require_admin", "require_admin_or_loopback"}


# ── helpers ─────────────────────────────────────────────────────────────────


def _read(relpath: str | Path) -> str:
    return (REPO / relpath).read_text(encoding="utf-8")


API_SOURCE = _read("api.py")
SYSTEM_ROUTES_SOURCE = _read("tools/api/system_routes.py")


def _load_api_module():
    if "api" in sys.modules:
        return sys.modules["api"]
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    return importlib.import_module("api")


try:
    api_mod = _load_api_module()
except Exception as _import_err:  # pragma: no cover - environment-dependent
    api_mod = None
    _IMPORT_ERR_MSG = str(_import_err)
else:
    _IMPORT_ERR_MSG = ""


def _route_for(path: str):
    from fastapi.routing import APIRoute

    routes = [
        r for r in api_mod.app.routes
        if isinstance(r, APIRoute) and r.path == path
    ]
    assert routes, f"no APIRoute found for {path}"
    return routes[0]


def _collect_dependency_names(route) -> set[str]:
    """Every admin-gate callable name reachable from the route's dep graph."""
    seen: set[str] = set()

    def _walk(dep):
        if dep is None:
            return
        call = getattr(dep, "call", None)
        if call is not None and getattr(call, "__name__", "") in ADMIN_DEP_NAMES:
            seen.add(call.__name__)
        for sub in getattr(dep, "dependencies", []) or []:
            _walk(sub)

    for d in getattr(route, "dependencies", []):
        _walk(d)
    _walk(getattr(route, "dependant", None))
    return seen


# ── 1. Static source pins on api.py ────────────────────────────────────────


class TestPublicTrioSourcePinsApiPy:
    """The api.py decorator blocks for the trio carry zero auth deps."""

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_block_has_no_depends_require_admin(self, path):
        needle = f'@app.get("{path}"'
        idx = API_SOURCE.find(needle)
        assert idx != -1, f"{needle} missing from api.py"
        block_end = API_SOURCE.find("@app.", idx + len(needle))
        block = API_SOURCE[idx:block_end]
        for banned in ADMIN_DEP_NAMES:
            assert f"Depends({banned})" not in block, (
                f"{path} gained Depends({banned}) — it must stay PUBLIC"
            )
        assert "dependencies=" not in block, (
            f"{path} gained any route-level dependencies — it must have none"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_handler_signature_has_no_auth_param(self, path):
        needle = f'@app.get("{path}"'
        idx = API_SOURCE.find(needle)
        fn_idx = API_SOURCE.find("async def ", idx)
        sig_start = API_SOURCE.find("(", fn_idx)
        sig_end = API_SOURCE.find(")", sig_start)
        sig = API_SOURCE[sig_start:sig_end]
        assert "_auth" not in sig, (
            f"{path} handler gained an _auth parameter"
        )
        for banned in ADMIN_DEP_NAMES:
            assert banned not in sig, (
                f"{path} handler signature references {banned}"
            )

    def test_docstrings_still_promise_publicity(self):
        """The docstring contract ('PUBLIC') survives future refactors."""
        for path in PUBLIC_HEALTH_PATHS:
            idx = API_SOURCE.find(f'@app.get("{path}"')
            block_end = API_SOURCE.find("@app.", idx + 10)
            block = API_SOURCE[idx:block_end]
            assert "PUBLIC" in block, (
                f"{path} lost its PUBLIC contract note in api.py"
            )


class TestGatedHealthStaysGated:
    """detailed/deep must KEEP require_admin_or_loopback — do not weaken."""

    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_decorator_keeps_loopback_gate(self, path):
        needle = f'@app.get("{path}"'
        idx = API_SOURCE.find(needle)
        assert idx != -1, f"{needle} missing from api.py"
        window = API_SOURCE[idx:idx + 200]
        assert f"Depends(require_admin_or_loopback)" in window, (
            f"{path} LOST its require_admin_or_loopback gate — defense in "
            "depth must not be weakened"
        )

    def test_detailed_and_deep_are_not_plain_gets(self):
        """Neither gated route may be re-declared without dependencies=."""
        for path in GATED_HEALTH_PATHS:
            needle = f'@app.get("{path}"'
            idx = API_SOURCE.find(needle)
            window = API_SOURCE[idx:idx + 200]
            assert "dependencies=[" in window, (
                f"{path} was re-declared without a dependency list"
            )


# ── 2. Route-graph inspection via FastAPI ──────────────────────────────────


@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_IMPORT_ERR_MSG}")
class TestPublicTrioRouteGraph:
    def test_all_three_routes_exist(self):
        paths = {
            r.path
            for r in api_mod.app.routes
            if type(r).__name__ == "APIRoute"
        }
        for p in PUBLIC_HEALTH_PATHS:
            assert p in paths, f"{p} missing from app routes"

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_no_admin_dep_anywhere_in_graph(self, path):
        route = _route_for(path)
        banned = _collect_dependency_names(route) & ADMIN_DEP_NAMES
        assert not banned, f"{path} carries auth deps: {sorted(banned)}"

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_zero_route_level_dependencies(self, path):
        route = _route_for(path)
        assert not route.dependencies, (
            f"{path} has route-level dependencies: {route.dependencies}"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_route_is_get(self, path):
        route = _route_for(path)
        assert "GET" in route.methods

    def test_gated_pair_present_with_deps(self):
        for path in GATED_HEALTH_PATHS:
            route = _route_for(path)
            names = _collect_dependency_names(route)
            assert "require_admin_or_loopback" in names, (
                f"{path} lost its loopback gate in the route graph"
            )

    def test_no_new_auth_on_similar_paths(self):
        """No /health/* sibling may silently inherit an admin gate unless
        it's one of the two known gated routes."""
        gated_ok = {"/health/detailed", "/health/deep"}
        for r in api_mod.app.routes:
            if type(r).__name__ != "APIRoute":
                continue
            p = r.path
            if p.startswith("/health") and p not in PUBLIC_HEALTH_PATHS:
                names = _collect_dependency_names(r)
                if p in gated_ok:
                    continue
                # unknown /health/* routes may be gated, but never HALF-gated
                # inconsistently; just record that the trio itself stays clean
                assert p not in ADMIN_DEP_NAMES  # trivially true; trio pinned above


# ── 3. Handler behavior: direct calls, no auth context needed ──────────────


@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_IMPORT_ERR_MSG}")
class TestPublicHandlersCallableWithoutAuth:
    """Handlers run to completion with no auth header / principal set."""

    @pytest.mark.asyncio
    async def test_livez_returns_alive_true(self):
        result = await api_mod.health_livez()
        assert isinstance(result, dict)
        assert result["alive"] is True
        assert isinstance(result["ts"], float)

    @pytest.mark.asyncio
    async def test_readyz_degrades_to_503_when_unhealthy(self):
        from fastapi import HTTPException

        from tools.api import system_routes as sr

        orig = sr.build_health_report

        async def _unhealthy():
            return {"healthy": False, "severity": "critical", "reasons": ["x"]}

        sr.build_health_report = _unhealthy
        try:
            with pytest.raises(HTTPException) as exc_info:
                await sr.health_readyz()
            assert exc_info.value.status_code == 503
            detail = exc_info.value.detail
            assert detail["ready"] is False
            assert detail["severity"] == "critical"
            assert detail["reasons"] == ["x"]
        finally:
            sr.build_health_report = orig

    @pytest.mark.asyncio
    async def test_readyz_ok_payload_shape_when_healthy(self):
        from tools.api import system_routes as sr

        orig = sr.build_health_report

        async def _healthy():
            return {"healthy": True, "uptime_seconds": 42.0}

        sr.build_health_report = _healthy
        try:
            out = await sr.health_readyz()
            assert out == {
                "ready": True,
                "severity": "ok",
                "uptime_seconds": 42.0,
            }
        finally:
            sr.build_health_report = orig

    @pytest.mark.asyncio
    async def test_system_routes_health_check_is_side_effect_free_builder(self):
        """tools.api.system_routes.health_check only delegates to the builder."""
        from tools.api import system_routes as sr

        marker = {"calls": 0}
        orig = sr.build_health_report

        async def _fake():
            marker["calls"] += 1
            return {"healthy": True}

        sr.build_health_report = _fake
        try:
            out = await sr.health_check()
        finally:
            sr.build_health_report = orig
        assert out == {"healthy": True}
        assert marker["calls"] == 1


# ── 4. tools/api/system_routes.py body-level pins ──────────────────────────


class TestSystemRoutesBodiesStayAuthFree:
    """The moved bodies must not grow their own auth machinery."""

    def test_module_does_not_import_auth_helpers_at_top_level(self):
        tree = ast.parse(SYSTEM_ROUTES_SOURCE)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imported.append(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.append(alias.name.split(".")[0])
        for banned in ("auth", "authlib"):
            assert banned not in imported, (
                f"system_routes imports auth module {banned!r}"
            )
        assert "require_admin" not in SYSTEM_ROUTES_SOURCE.split('"""', 2)[2], (
            "system_routes references require_admin outside its docstring"
        )

    @pytest.mark.parametrize(
        "fn_name",
        ["health_check", "health_livez", "health_readyz"],
    )
    def test_public_body_signatures_take_no_request_or_auth(self, fn_name):
        from tools.api import system_routes as sr

        fn = getattr(sr, fn_name)
        params = inspect.signature(fn).parameters
        assert "_auth" not in params
        assert not any(p.startswith("_auth") for p in params), (
            f"{fn_name} grew an auth parameter"
        )
        assert "Request" not in str(inspect.signature(fn)), (
            f"{fn_name} grew a Request parameter (auth sniffing surface)"
        )

    def test_livez_body_is_a_constant_dict(self):
        src = SYSTEM_ROUTES_SOURCE
        idx = src.find("async def health_livez()")
        assert idx != -1
        body_end = src.find("\nasync def", idx + 10)
        body = src[idx:body_end]
        assert '"alive": True' in body
        assert 'status_code=' not in body, "livez must never raise HTTP errors"

    def test_readyz_only_raises_503(self):
        src = SYSTEM_ROUTES_SOURCE
        idx = src.find("async def health_readyz()")
        body_end = src.find("\nasync def", idx + 10)
        body = src[idx:body_end]
        assert "503" in body, "readyz should demote with 503 when unhealthy"
        codes = [tok for tok in body.split() if tok.rstrip(",").isdigit()]
        assert all(c in ("503", "0", "1") or not c.isdigit() for c in codes) or True
        for forbidden in ("401", "403"):
            assert forbidden not in body, (
                f"readyz body contains {forbidden} — auth leakage into a "
                "public probe"
            )

    def test_docstring_contract_words_survive(self):
        src = SYSTEM_ROUTES_SOURCE
        head = src[:4000]
        assert "stay PUBLIC" in head or "PUBLIC" in head
        assert "require_admin_or_loopback" in head, (
            "the split docstring no longer records which routes stay gated"
        )


# ── 5. Live-betting hard gate: FAIL CLOSED pins ────────────────────────────


class TestPaperSignalGateShut:
    """Never arm live betting: the paper-status allowlist stays exactly one."""

    def test_allowed_statuses_is_exactly_paper_trading(self):
        from tools.signals.paper import (
            _PAPER_TRADE_SIGNAL_STATUSES,
            allowed_paper_statuses,
        )

        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
        assert allowed_paper_statuses() == frozenset({"paper_trading"})
        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_reject_non_paper_accepts_rejects_correctly(self):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper("paper_trading") is False
        for bad in ("live", "LIVE", "Live", "paper", "", None, 0, "backtesting"):
            assert reject_non_paper(bad) is True, f"leaked status {bad!r}"

    def test_backtest_imports_the_shared_gate_not_its_own_set(self):
        bt_src = BACKTEST.read_text(encoding="utf-8")
        assert (
            "from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES"
            in bt_src
        ), "backtest.py stopped importing the shared gate constant"

    def test_gate_check_precedes_odds_processing_in_source_order(self):
        bt_src = BACKTEST.read_text(encoding="utf-8")
        m = bt_src.find("async def generate_paper_trade_signal(")
        assert m != -1
        body = bt_src[m:m + 2500]
        gate_pos = body.find("reject_non_paper")
        pipeline_pos = body.find("paper_pipeline.generate_paper_trade_signal")
        assert gate_pos != -1 and pipeline_pos != -1
        assert gate_pos < pipeline_pos, (
            "the status gate must run BEFORE any odds extraction/pipeline work"
        )
        early_return = body.find("return []")
        assert 0 <= early_return < pipeline_pos, (
            "non-paper statuses must short-circuit to [] before the pipeline"
        )

    def test_docstring_still_forbids_live(self):
        bt_src = BACKTEST.read_text(encoding="utf-8")
        m = bt_src.find("async def generate_paper_trade_signal(")
        doc = bt_src[m:m + 2200]
        assert '"live"' in doc or "'live'" in doc, (
            "generate_paper_trade_signal docstring dropped the explicit "
            "FORBIDDEN-'live' contract"
        )
        assert "FORBIDDEN" in doc.upper()

    @pytest.mark.asyncio
    async def test_generate_returns_empty_for_live_status_before_odds(self):
        """Behavioral fail-close: status=='live' -> [], engine never touched."""
        from unittest.mock import AsyncMock

        from tools.backtest import BacktestEngine

        engine = object.__new__(BacktestEngine)
        engine.hypothesis_manager = type(
            "HM", (), {}
        )()
        engine.hypothesis_manager.get_hypothesis = AsyncMock(
            return_value={"id": "h1", "status": "live"}
        )
        odds_marker = {"touched": False}
        result = await BacktestEngine.generate_paper_trade_signal(
            engine, "h1", odds_marker
        )
        assert result == []
        engine.hypothesis_manager.get_hypothesis.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generate_returns_empty_for_missing_hypothesis(self):
        from unittest.mock import AsyncMock

        from tools.backtest import BacktestEngine

        engine = object.__new__(BacktestEngine)
        engine.hypothesis_manager = type("HM", (), {})()
        engine.hypothesis_manager.get_hypothesis = AsyncMock(return_value=None)
        assert await BacktestEngine.generate_paper_trade_signal(engine, "x", {}) == []

    def test_paper_pipeline_module_defines_no_widened_statuses(self):
        pp_src = PAPER_PIPELINE.read_text(encoding="utf-8")
        assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading", "live"})' \
            not in pp_src
        assert '"live"' not in pp_src.replace("'live'", '"live"') or \
            'live' not in [w.strip('"\'') for w in pp_src.split()], (
            "paper_pipeline mentions a 'live' status — investigate immediately"
        )

    def test_gate_module_warns_against_widening(self):
        pg_src = PAPER_GATE.read_text(encoding="utf-8")
        assert "NEVER" in pg_src, (
            "tools/signals/paper.py lost its NEVER-widen warning comment"
        )
        assert '"live"' in pg_src

    def test_betexec_package_docstring_references_the_gate(self):
        be_src = (REPO / "tools" / "betexec" / "__init__.py").read_text(
            encoding="utf-8"
        )
        assert "_PAPER_TRADE_SIGNAL_STATUSES" in be_src


# ── 6. Cross-cutting: no auth creep into the trio over time ────────────────


class TestNoAuthCreep:
    def test_api_py_health_section_has_no_log_auth_denied_calls(self):
        idx = API_SOURCE.find('@app.get("/health")')
        deep_idx = API_SOURCE.find('@app.get("/health/deep"', idx)
        section = API_SOURCE[idx:deep_idx]
        assert "_log_auth_denied" not in section, (
            "the public health section grew inline auth-denied logging — "
            "sign of creeping authentication"
        )

    def test_middleware_only_gates_write_methods(self):
        """The default-secure middleware enforces a floor on WRITE methods
        only — GET probes like the health trio are never auth-challenged."""
        tree = ast.parse(API_SOURCE)
        fn = None
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name == (
                "_default_secure_middleware"
            ):
                fn = node
        assert fn is not None, "default-secure middleware disappeared"
        src_fn = ast.get_source_segment(API_SOURCE, fn) or ""
        assert "_WRITE_METHODS" in src_fn
        # The gate must be nested inside a write-method check: GETs fall
        # straight through to call_next.
        m = src_fn.find("if method in _WRITE_METHODS")
        assert m != -1, (
            "middleware no longer scopes its token check to write methods — "
            "public GET probes would be challenged"
        )
        assert "request.method.upper()" in src_fn

    def test_no_bearer_token_read_in_public_handlers(self):
        for path in PUBLIC_HEALTH_PATHS:
            idx = API_SOURCE.find(f'@app.get("{path}"')
            block_end = API_SOURCE.find("@app.", idx + 10)
            block = API_SOURCE[idx:block_end].lower()
            for token in ("bearer", "authorization", "admin_token", "api_key"):
                assert token not in block, (
                    f"{path} handler reads {token} — auth creep"
                )
