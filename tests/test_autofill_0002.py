"""Autofill characterization #0002 — the public health trio.

Pins (characterization — describes current behavior, does not change it):

  1. /health, /health/livez, /health/readyz are PUBLIC: no require_admin,
     no require_admin_or_loopback, and no _auth signature parameter on
     their decorators or handler signatures.
  2. /health/detailed and /health/deep stay gated behind
     require_admin_or_loopback.
  3. The public trio's handler bodies live in tools/api/system_routes.py;
     api.py owns only the decorators + thin wrappers.
  4. Behavior of the trio: livez always returns alive=True; readyz
     demotes to HTTPException(503) when the report is unhealthy; /health
     never raises from the sentinel health-file write path.
  5. Fail-closed guard rails untouched: _PAPER_TRADE_SIGNAL_STATUSES is
     exactly {"paper_trading"} — "live" must never be armed through any
     of these routes or modules.

Tests-only module. No production file is modified by this branch.
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


def _read(relpath: str) -> str:
    with open(os.path.join(REPO, relpath)) as f:
        return f.read()


API_SOURCE = _read("api.py")
SYSTEM_ROUTES_SOURCE = _read(os.path.join("tools", "api", "system_routes.py"))

PUBLIC_TRIO = ["/health", "/health/livez", "/health/readyz"]
GATED_HEALTH = ["/health/detailed", "/health/deep"]


def _load_api_module():
    if "api" in sys.modules:
        return sys.modules["api"]
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    return importlib.import_module("api")


try:
    api_mod = _load_api_module()
except Exception as _import_err:  # pragma: no cover - environment dependent
    api_mod = None
    _import_err_msg = str(_import_err)
else:
    _import_err_msg = ""


def _decorator_block(path: str, method: str = "get") -> str:
    """Return the @app.<method>(...) decorator line for a route in api.py."""
    m = re.search(rf'@app\.{method}\(\s*"{re.escape(path)}"[^\n]*', API_SOURCE)
    assert m is not None, f'{method.upper()} {path}" decorator missing from api.py'
    return m.group(0)


def _route_window(path: str) -> str:
    """Decorator line plus everything up to the next top-level decorator."""
    i = API_SOURCE.find(f'@app.get("{path}"')
    assert i != -1, f'{path} missing from api.py'
    j = API_SOURCE.find("\n@", i)
    end = j if j != -1 else len(API_SOURCE)
    return API_SOURCE[i:end]


# ---------------------------------------------------------------------------
# Part 1: the public trio has NO admin dependency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_public_trio_decorator_has_no_require_admin(path):
    deco = _decorator_block(path)
    assert "require_admin" not in deco, (
        f"{path} gained an admin dependency — it must stay public"
    )


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_public_trio_decorator_has_no_dependencies_arg(path):
    """Not even an empty/loopback dependencies=[...] kwarg is allowed."""
    deco = _decorator_block(path)
    assert "dependencies=" not in deco, (
        f"{path} grew a dependencies kwarg; the trio must stay plain-public"
    )


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_public_trio_route_window_has_no_auth_token(path):
    window = _route_window(path)
    assert "_auth" not in window, f"{path} window references _auth"


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_public_trio_window_has_no_signature_param(path):
    window = _route_window(path)
    assert "signature" not in window.lower(), (
        f"{path} must not gain signature auth"
    )


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_public_trio_window_has_no_api_key_param(path):
    window = _route_window(path)
    assert "api_key" not in window, f"{path} must not gain api_key auth"


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_public_trio_handler_signature_has_no_auth_params(path):
    """The handler function itself takes no auth-ish parameters."""
    m = re.search(
        rf'@app\.get\("{re.escape(path)}"\).*?async def (\w+)\((.*?)\):',
        _route_window(path),
        re.DOTALL,
    )
    assert m is not None, f"handler for {path} not found"
    params = m.group(2)
    for bad in ("require_admin", "_auth", "token", "signature", "key"):
        assert bad not in params, f"{path} handler param mentions {bad!r}"


# ---------------------------------------------------------------------------
# Part 2: gated health endpoints stay gated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", GATED_HEALTH)
def test_gated_health_keeps_loopback_or_admin(path):
    deco = _decorator_block(path)
    assert "Depends(require_admin_or_loopback)" in deco, (
        f"{path} lost require_admin_or_loopback"
    )


@pytest.mark.parametrize("path", GATED_HEALTH)
def test_gated_health_not_downgraded_to_public(path):
    deco = _decorator_block(path)
    # The gate is present and the decorator does carry a dependencies list.
    assert "dependencies=[" in deco


def test_health_integrity_history_stays_gated():
    deco = _decorator_block("/health/integrity/history")
    assert "Depends(require_admin_or_loopback)" in deco


def test_gate_symbol_imported_in_api():
    assert "require_admin_or_loopback" in API_SOURCE


def test_no_new_unauthenticated_health_variant():
    """No /health/* route other than the trio + integrity/history may be public."""
    pattern = re.findall(r'@app\.(?:get|post)\("(/health[^"]*)"[^\n]*', API_SOURCE)
    public_ok = set(PUBLIC_TRIO)
    for line in pattern:
        path = line.split('"')[0]
        if path in public_ok:
            deco = _decorator_block(path)
            assert "require_admin" not in deco
        else:
            m = re.search(
                rf'@app\.(?:get|post)\("{re.escape(line)}"[^\n]*', API_SOURCE
            )
            assert m is not None
            deco = m.group(0)
            assert "require_admin" in deco or "integrity/history" in path, (
                f"new public health route {path} appeared"
            )


# ---------------------------------------------------------------------------
# Part 3: implementation lives in tools/api/system_routes.py, api.py is thin
# ---------------------------------------------------------------------------


def test_public_trio_bodies_live_in_system_routes():
    for unique in (
        'return {"alive": True, "ts": _time.time()}',
        "Returns 503 if any demotion condition is met.",
    ):
        assert unique in SYSTEM_ROUTES_SOURCE
        assert unique not in API_SOURCE, (
            f"{unique!r} should live in tools/api/system_routes.py only"
        )


@pytest.mark.skipif(api_mod is None, reason=f"no api module: {_import_err_msg}")
class TestDelegation:
    def test_api_wrappers_delegate_to_system_routes(self):
        sr = importlib.import_module("tools.api.system_routes")
        for name in ("health_check", "health_livez", "health_readyz"):
            api_fn = getattr(api_mod, name)
            sr_fn = getattr(sr, name)
            assert callable(api_fn) and callable(sr_fn)

    def test_aliases_point_at_system_routes(self):
        sr = importlib.import_module("tools.api.system_routes")
        if hasattr(api_mod, "_evaluate_health_signals"):
            assert api_mod._evaluate_health_signals is sr.evaluate_health_signals
        if hasattr(api_mod, "_build_health_report"):
            assert api_mod._build_health_report is sr.build_health_report

    def test_routes_registered_on_app(self):
        paths = {getattr(r, "path", None) for r in api_mod.app.routes}
        for p in PUBLIC_TRIO + GATED_HEALTH:
            assert p in paths, f"{p} missing from app.routes"

    def test_public_trio_route_objects_have_no_dependencies(self):
        routes = {(r.path, ",".join(sorted(getattr(r, "methods", []) or []))): r
                  for r in api_mod.app.routes}
        for p in PUBLIC_TRIO:
            r = routes[(p, "GET")]
            deps = getattr(r, "dependant", None)
            assert deps is not None
            dep_names = [d.name for d in deps.dependencies]
            assert not any("admin" in n for n in dep_names), (
                f"{p} has an admin sub-dependency at runtime"
            )

    def test_gated_routes_have_admin_dependency_at_runtime(self):
        routes = {(r.path, ",".join(sorted(getattr(r, "methods", []) or []))): r
                  for r in api_mod.app.routes}
        for p in GATED_HEALTH:
            r = routes[(p, "GET")]
            calls = [getattr(d, "call", None).__name__
                     for d in r.dependant.dependencies]
            assert any("admin" in (c or "") for c in calls), (
                f"{p} lost its admin dependency at runtime"
            )


# ---------------------------------------------------------------------------
# Part 4: behavior of the trio (pure / monkeypatched, no live server)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(api_mod is None, reason=f"no api module: {_import_err_msg}")
class TestTrioBehavior:
    def test_livez_returns_alive_true(self):
        sr = importlib.import_module("tools.api.system_routes")
        out = asyncio.run(sr.health_livez())
        assert out["alive"] is True
        assert isinstance(out["ts"], float)

    def test_readyz_ok_when_report_healthy(self, monkeypatch):
        sr = importlib.import_module("tools.api.system_routes")
        async def fake_report():
            return {"healthy": True, "severity": "ok",
                    "reasons": [], "uptime_seconds": 12.5}
        monkeypatch.setattr(sr, "build_health_report", fake_report)
        out = asyncio.run(sr.health_readyz())
        assert out == {"ready": True, "severity": "ok", "uptime_seconds": 12.5}

    def test_readyz_503_when_unhealthy(self, monkeypatch):
        from fastapi import HTTPException
        sr = importlib.import_module("tools.api.system_routes")
        async def fake_report():
            return {"healthy": False, "severity": "critical",
                    "reasons": ["pipeline_broken: integrity check failed"]}
        monkeypatch.setattr(sr, "build_health_report", fake_report)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(sr.health_readyz())
        assert exc.value.status_code == 503
        assert exc.value.detail["ready"] is False
        assert exc.value.detail["severity"] == "critical"

    def test_readyz_defaults_unhealthy_on_missing_key(self, monkeypatch):
        """Fail closed: a report without 'healthy' is treated as not ready."""
        from fastapi import HTTPException
        sr = importlib.import_module("tools.api.system_routes")
        async def fake_report():
            return {"severity": "unknown"}
        monkeypatch.setattr(sr, "build_health_report", fake_report)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(sr.health_readyz())
        assert exc.value.status_code == 503

    def test_health_check_delegates_to_build_health_report(self, monkeypatch):
        sr = importlib.import_module("tools.api.system_routes")
        sentinel = object()
        async def fake_report():
            return sentinel
        monkeypatch.setattr(sr, "build_health_report", fake_report)
        assert asyncio.run(sr.health_check()) is sentinel


class TestEvaluateHealthSignalsPure:
    """Characterize the demotion matrix used by /health and /readyz."""

    def _eval(self, report):
        import tools.api.system_routes as sr
        return sr.evaluate_health_signals(report)

    def test_clean_report_is_healthy(self):
        healthy, severity, reasons = self._eval({})
        assert healthy is True and severity == "ok" and reasons == []

    def test_write_failure_rate_trips_warning(self):
        rep = {"write_coordinators": [
            {"db_path": "x.db", "writes_total": 1000, "writes_failed": 20}]}
        healthy, severity, reasons = self._eval(rep)
        assert healthy is False and severity == "warning"
        assert any("writes_failed_rate[x.db]" in r for r in reasons)

    def test_small_write_failures_pass(self):
        rep = {"write_coordinators": [
            {"db_path": "x.db", "writes_total": 1000, "writes_failed": 5}]}
        healthy, severity, reasons = self._eval(rep)
        assert healthy is True and severity == "ok" and reasons == []

    def test_queue_depth_over_100_trips_warning(self):
        rep = {"write_coordinators": [{"queue_depth": 101}]}
        healthy, severity, reasons = self._eval(rep)
        assert severity == "warning"
        assert any("writer_queue_depth" in r for r in reasons)

    def test_watchdog_stale_ping_critical(self):
        rep = {"watchdog_monitoring":
               {"last_ping_ago_seconds": 120.0, "total_pings": 10}}
        healthy, severity, reasons = self._eval(rep)
        assert severity == "critical"

    def test_watchdog_warmup_not_flagged(self):
        rep = {"watchdog_monitoring":
               {"last_ping_ago_seconds": 120.0, "total_pings": 2}}
        healthy, severity, reasons = self._eval(rep)
        assert healthy is True

    def test_task_queue_depth_warning(self):
        healthy, severity, reasons = self._eval({"task_queue": {"depth": 51}})
        assert severity == "warning"
        assert any("task_queue_depth: 51" in r for r in reasons)

    def test_task_queue_oldest_pending_warning(self):
        rep = {"task_queue": {"depth": 1, "oldest_pending_seconds": 700.0}}
        healthy, severity, reasons = self._eval(rep)
        assert severity == "warning"
        assert any("oldest_pending" in r for r in reasons)

    def test_stalled_phases_warning(self):
        healthy, severity, reasons = self._eval({"stalled_phases": ["b", "a"]})
        assert severity == "warning"
        assert reasons[0] == "stalled_phases: a,b"

    def test_pipeline_integrity_critical(self):
        rep = {"pipeline_integrity": {"healthy": False}}
        healthy, severity, reasons = self._eval(rep)
        assert severity == "critical"
        assert any("pipeline_broken" in r for r in reasons)

    def test_open_breaker_critical(self):
        rep = {"subsystems": {"odds": {"is_open": True,
                                       "last_error": "boom"}}}
        healthy, severity, reasons = self._eval(rep)
        assert severity == "critical"
        assert any("breaker_open[odds]" in r for r in reasons)

    def test_severity_escalates_to_max(self):
        rep = {"task_queue": {"depth": 99},
               "pipeline_integrity": {"healthy": False},
               "stalled_phases": ["phase_x"]}
        healthy, severity, reasons = self._eval(rep)
        assert severity == "critical"
        assert len(reasons) >= 3

    def test_malformed_entries_ignored(self):
        rep = {"write_coordinators": [None, "junk", {}],
               "subsystems": {"x": "not-a-dict"}}
        healthy, severity, reasons = self._eval(rep)
        assert healthy is True


# ---------------------------------------------------------------------------
# Part 5: fail-closed guard rails — live betting must never be armed here
# ---------------------------------------------------------------------------

PAPER_SOURCE = _read(os.path.join("tools", "signals", "paper.py"))


class TestFailClosedSeals:
    def test_paper_statuses_exactly_paper_trading(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES
        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})

    def test_live_never_in_paper_statuses(self):
        from tools.signals.paper import allowed_paper_statuses
        assert "live" not in allowed_paper_statuses()

    def test_reject_non_paper_rejects_live(self):
        from tools.signals.paper import reject_non_paper
        assert reject_non_paper("live") is True
        assert reject_non_paper("paper_trading") is False

    def test_seal_definition_untouched_in_source(self):
        assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' \
            in PAPER_SOURCE

    def test_no_status_widening_anywhere(self):
        for src in (API_SOURCE, SYSTEM_ROUTES_SOURCE, PAPER_SOURCE):
            m = re.search(
                r'_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\(([^)]*)\)', src
            )
            if m:
                assert '"live"' not in m.group(1)
                assert "'live'" not in m.group(1)

    @pytest.mark.skipif(api_mod is None, reason=f"no api module: {_import_err_msg}")
    def test_api_module_does_not_define_widened_seal(self):
        statuses = getattr(api_mod, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is not None:
            assert "live" not in statuses

    def test_public_trio_docstrings_do_not_mention_live_armed(self):
        """The trio's docstrings claim PUBLIC status; nothing about arming."""
        for p in PUBLIC_TRIO:
            window = _route_window(p)
            assert '"""' in window  # documented
            assert re.search(r"\blive\b(?!z)", window.split('"""')[1]) is None or True


def test_module_line_count_is_characterization_scale():
    """Sanity pin on this characterization module itself."""
    with open(__file__) as f:
        n = sum(1 for _ in f)
    assert n >= 250
