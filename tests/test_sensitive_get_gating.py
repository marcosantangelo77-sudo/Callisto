"""Source-contract + unit tests for sensitive GET gating (admin-or-loopback).

Follows tests/test_api_auth.py conventions:
  * Does NOT enter FastAPI lifespan.
  * Source contract: each sensitive GET's decorator in api.py text must
    carry `require_admin_or_loopback` near the route declaration.
  * Dependency isolation: call require_admin_or_loopback directly with a
    fake Request — non-loopback/no-token must be rejected; loopback must
    pass when no admin token is configured.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest
from starlette.requests import Request

try:
    from tests.test_api_auth import api_mod, _import_err_msg  # noqa: F401
except Exception:  # pragma: no cover - fallback direct import
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    import importlib

    api_mod = importlib.import_module("api")
    _import_err_msg = ""

API_SOURCE = (Path(__file__).resolve().parent.parent / "api.py").read_text()

SENSITIVE_GETS = [
    "/bets",
    "/bets/bankroll",
    "/bets/clv-report",
    "/bets/clv-forecast",
    "/hypothesis",
    "/hypothesis/{hypothesis_id}",
    "/hypothesis/{hypothesis_id}/report",
    "/hypothesis/{hypothesis_id}/significance",
    "/system/full-status",
    "/executor/status",
    "/odds/edges",
    "/odds/opportunities",
    "/odds/movements",
    "/odds/snapshots/{sport}",
    "/odds/status",
    "/odds/narrative-edges",
    "/odds/kl-metrics",
    # batch 3
    "/odds/sgp-analysis/{sport}",
    "/odds/props/{sport}/{event_id}",
    "/odds/dk-props/{sport}",
    "/odds/learned-correlations",
    "/odds/market-analysis/{sport}",
    "/odds/stale-lines/{sport}",
    "/odds/psychology/{sport}",
    "/odds/psychology",
    "/odds/dead-numbers/{sport}",
    "/odds/line-analysis/{sport}",
    "/odds/line-gaps/{sport}",
    "/odds/prop-gaps/{sport}",
    "/analysis/futures-efficiency",
    "/analysis/half-market/{sport}",
    "/analysis/cross-tabulate/{sport}",
    "/wiki/stats",
    "/wiki/articles",
    "/wiki/article/{topic}",
    "/wiki/search",
    "/wiki/contradictions",
    "/health/detailed",
    "/health/deep",
    "/health/integrity/history",
    "/tasks",
    # batch 4
    "/model/total/{sport}",
    "/model/environment",
    "/model/injury-impact/{sport}",
    "/data/injuries/{sport}",
    "/data/scoreboard/{sport}",
    "/data/weather",
    "/data/referee",
    "/data/stats",
    "/backtest/run/{run_id}",
    "/historical/cache",
    "/research/status",
    "/research/sports",
    "/embeddings/stats",
    "/claude/status",
    "/debug/memory",
    "/debug/memory/top-traces",
]


def _decorator_for(path: str) -> str:
    """Return the @app.get(...) decorator block for the given literal path."""
    escaped = re.escape(path)
    m = re.search(rf'@app\.get\(\s*\n?\s*"{escaped}"[^)]*\)', API_SOURCE)
    assert m is not None, f"route {path} not found in api.py"
    return m.group(0)


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestSensitiveGetSourceContract:
    @pytest.mark.parametrize("path", SENSITIVE_GETS)
    def test_route_gated_with_require_admin_or_loopback(self, path):
        deco = _decorator_for(path)
        assert "require_admin_or_loopback" in deco, (
            f"GET {path} is not gated with require_admin_or_loopback"
        )

    @pytest.mark.parametrize(
        ("path", "func"),
        [
            ("/edges/live", "get_live_edges"),
        ],
    )
    def test_route_gated_via_signature_auth_param(self, path, func):
        """Routes gated via an ``_auth`` signature param instead of the decorator.

        Source pin: the function definition itself must carry
        ``require_admin_or_loopback``.
        """
        m = re.search(rf'@app\.get\(\s*"{re.escape(path)}".*?def {func}\(.*?\):', API_SOURCE, re.DOTALL)
        assert m is not None, f"route {path} ({func}) not found in api.py"
        assert "require_admin_or_loopback" in m.group(0), (
            f"GET {path} ({func}) missing _auth: Depends(require_admin_or_loopback)"
        )


def _fake_request(client_host: str, headers: dict | None = None) -> Request:
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": hdrs,
        "client": (client_host, 55555),
        "query_string": b"",
    }
    return Request(scope)


def _check(req, creds=None):
    """Run the async dependency and return (allowed, status_code_or_None)."""
    try:
        asyncio.run(api_mod.require_admin_or_loopback(req, creds))
        return True, None
    except Exception as exc:
        return False, getattr(exc, "status_code", None)


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestRequireAdminOrLoopbackUnit:
    def test_non_loopback_no_token_rejected(self, monkeypatch):
        monkeypatch.setattr(api_mod, "CALLISTO_ADMIN_TOKEN", "")
        allowed, code = _check(_fake_request("203.0.113.7"))
        assert not allowed
        assert code in (401, 403)

    def test_loopback_allowed_when_token_unset(self, monkeypatch):
        monkeypatch.setattr(api_mod, "CALLISTO_ADMIN_TOKEN", "")
        allowed, _ = _check(_fake_request("127.0.0.1"))
        assert allowed

    def test_non_loopback_with_bad_token_rejected(self, monkeypatch):
        monkeypatch.setattr(api_mod, "CALLISTO_ADMIN_TOKEN", "sekrit")
        from fastapi.security.http import HTTPAuthorizationCredentials

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong")
        allowed, code = _check(_fake_request("203.0.113.7"), creds)
        assert not allowed
        assert code in (401, 403)

    def test_non_loopback_with_good_token_allowed(self, monkeypatch):
        monkeypatch.setattr(api_mod, "CALLISTO_ADMIN_TOKEN", "sekrit")
        from fastapi.security.http import HTTPAuthorizationCredentials

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="sekrit")
        allowed, _ = _check(_fake_request("203.0.113.7"), creds)
        assert allowed

    def test_loopback_allowed_even_when_token_set_without_header(self, monkeypatch):
        monkeypatch.setattr(api_mod, "CALLISTO_ADMIN_TOKEN", "sekrit")
        allowed, _ = _check(_fake_request("127.0.0.1"))
        assert allowed
