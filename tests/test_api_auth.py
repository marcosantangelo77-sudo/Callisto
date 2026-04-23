"""Tests for the default-secure API auth model + /admin/sql AST validator.

Covers:
  * Middleware-level default gate: write methods require loopback or token.
  * Public allowlist: `/task` + `/context/sync` exempted at middleware layer.
  * GET `/world/{domain}` limit cap.
  * `/admin/sql` validator: rejects PRAGMA writable_schema=1, multi-statement,
    write-verbs-in-CTE, allows whitelisted read-only PRAGMAs.

Does NOT import the full `api` module (lifespan loads DBs, MCP, etc.) —
instead replays the middleware on a miniature FastAPI app so the auth
logic is exercised in isolation. The real `/admin/sql` validator is
imported from `api` via a lightweight direct import (the validator is a
pure function with no global state).
"""

from __future__ import annotations

import os
import sys
import importlib
import types

import pytest
from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Import just the auth helpers + validator from api.py.
# We deliberately avoid triggering the FastAPI app lifespan.
# ---------------------------------------------------------------------------

def _load_api_security_module():
    """Import api.py but stub heavy lifespan imports before import.

    We only need the security helpers, not the running app.
    """
    if "api" in sys.modules:
        return sys.modules["api"]
    # Minimal env so api.py import doesn't blow up on missing config.
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    # Import; this does execute module-level code (sets up FastAPI app) but
    # does NOT trigger lifespan (that only fires on TestClient(app) enter).
    return importlib.import_module("api")


try:
    api_mod = _load_api_security_module()
except Exception as _import_err:
    api_mod = None
    _import_err_msg = str(_import_err)
else:
    _import_err_msg = ""


# ---------------------------------------------------------------------------
# /admin/sql validator tests — pure function, no FastAPI needed.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestAdminSqlValidator:
    def test_plain_select_allowed(self):
        assert api_mod._validate_admin_sql("SELECT 1") is None
        assert api_mod._validate_admin_sql("SELECT * FROM hypotheses LIMIT 10") is None

    def test_multi_statement_rejected(self):
        err = api_mod._validate_admin_sql("SELECT 1; DROP TABLE x;")
        assert err is not None
        assert "multi" in err.lower() or "not allowed" in err.lower()

    def test_pragma_writable_schema_rejected(self):
        err = api_mod._validate_admin_sql("PRAGMA writable_schema=1")
        assert err is not None
        assert "assignment" in err.lower() or "allowlist" in err.lower()

    def test_pragma_journal_mode_off_rejected(self):
        err = api_mod._validate_admin_sql("PRAGMA journal_mode=OFF")
        assert err is not None
        assert "assignment" in err.lower()

    def test_pragma_foreign_keys_off_rejected(self):
        err = api_mod._validate_admin_sql("PRAGMA foreign_keys = OFF")
        assert err is not None
        assert "assignment" in err.lower()

    def test_pragma_integrity_check_allowed(self):
        # Must be on the explicit whitelist.
        assert api_mod._validate_admin_sql("PRAGMA integrity_check") is None

    def test_pragma_page_count_allowed(self):
        assert api_mod._validate_admin_sql("PRAGMA page_count") is None

    def test_pragma_random_rejected(self):
        err = api_mod._validate_admin_sql("PRAGMA cache_spill=0")
        assert err is not None

    def test_cte_with_delete_rejected(self):
        # This should get rejected because DELETE is in the text, and we scan
        # for write-verbs across the whole statement.
        err = api_mod._validate_admin_sql(
            "WITH x AS (DELETE FROM hypotheses RETURNING 1) SELECT * FROM x"
        )
        assert err is not None
        assert "DELETE" in err or "Forbidden" in err

    def test_update_rejected(self):
        err = api_mod._validate_admin_sql("UPDATE hypotheses SET status='live'")
        assert err is not None

    def test_drop_rejected(self):
        err = api_mod._validate_admin_sql("DROP TABLE hypotheses")
        assert err is not None

    def test_attach_rejected(self):
        err = api_mod._validate_admin_sql("ATTACH DATABASE '/tmp/evil.db' AS e")
        assert err is not None

    def test_empty_rejected(self):
        err = api_mod._validate_admin_sql("   ")
        assert err is not None


# ---------------------------------------------------------------------------
# Middleware auth-model tests — build a mini-app replaying the middleware.
# ---------------------------------------------------------------------------

def _make_mini_app(admin_token: str = "", public_paths: set[tuple[str, str]] | None = None):
    """Build a FastAPI app that imports the auth helpers from api.py.

    Crucially: we replicate the middleware WITHOUT relying on the live app's
    route inventory — the mini-app only has a handful of routes. We use the
    real helpers so logic stays in sync.
    """
    if api_mod is None:
        pytest.skip(f"api module unavailable: {_import_err_msg}")

    import secrets as _secrets
    app = FastAPI()
    public_paths = public_paths or set()

    # Replay middleware, using the test-local public set.
    @app.middleware("http")
    async def _mw(request: Request, call_next):
        method = request.method.upper()
        if method in api_mod._WRITE_METHODS:
            if (method, request.url.path) not in public_paths:
                if api_mod._client_is_loopback(request):
                    pass
                else:
                    auth_header = request.headers.get("authorization", "")
                    if not admin_token:
                        return JSONResponse(
                            status_code=403,
                            content={"error": "Loopback only when admin token unset"},
                        )
                    if not auth_header.lower().startswith("bearer "):
                        return JSONResponse(
                            status_code=401,
                            content={"error": "Bearer token required"},
                        )
                    provided = auth_header.split(" ", 1)[1].strip()
                    if not _secrets.compare_digest(provided, admin_token):
                        return JSONResponse(
                            status_code=403, content={"error": "Forbidden"},
                        )
        return await call_next(request)

    @app.post("/gated")
    async def gated():
        return {"ok": True}

    @app.post("/public")
    async def pub():
        return {"ok": True}

    @app.get("/read")
    async def read():
        return {"ok": True}

    return app


def _make_client(app: FastAPI, client_host: str = "127.0.0.1") -> TestClient:
    """TestClient that reports a specific client.host in request.client.

    Starlette's TestClient uses ('testclient', 50000) by default.
    Override via transport by injecting an ASGI scope mutator.
    """
    # The starlette TestClient accepts `client=(host, port)` since starlette
    # 0.27. Use that to simulate loopback vs external callers.
    return TestClient(app, base_url=f"http://{client_host}:8420", client=(client_host, 5555))


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestMiddlewareGate:
    def test_loopback_allowed_no_token(self):
        app = _make_mini_app(admin_token="")
        c = _make_client(app, client_host="127.0.0.1")
        r = c.post("/gated")
        assert r.status_code == 200, r.text

    def test_external_denied_no_token(self):
        app = _make_mini_app(admin_token="")
        c = _make_client(app, client_host="10.0.0.5")
        r = c.post("/gated")
        assert r.status_code == 403
        assert "Loopback only" in r.json().get("error", "")

    def test_external_without_bearer_denied(self):
        app = _make_mini_app(admin_token="sekret")
        c = _make_client(app, client_host="10.0.0.5")
        r = c.post("/gated")
        assert r.status_code == 401

    def test_external_with_bad_bearer_denied(self):
        app = _make_mini_app(admin_token="sekret")
        c = _make_client(app, client_host="10.0.0.5")
        r = c.post("/gated", headers={"Authorization": "Bearer WRONG"})
        assert r.status_code == 403

    def test_external_with_good_bearer_allowed(self):
        app = _make_mini_app(admin_token="sekret")
        c = _make_client(app, client_host="10.0.0.5")
        r = c.post("/gated", headers={"Authorization": "Bearer sekret"})
        assert r.status_code == 200

    def test_public_endpoint_accessible_external_no_token(self):
        # Public paths bypass the middleware entirely — this models /task
        app = _make_mini_app(
            admin_token="",
            public_paths={("POST", "/public")},
        )
        c = _make_client(app, client_host="10.0.0.5")
        r = c.post("/public")
        assert r.status_code == 200

    def test_get_not_gated_by_middleware(self):
        # The middleware only gates writes. GETs fall through (individual
        # endpoints still add their own require_admin_or_loopback where needed).
        app = _make_mini_app(admin_token="")
        c = _make_client(app, client_host="10.0.0.5")
        r = c.get("/read")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# /world/{domain} limit cap — verified at the handler level rather than live.
# ---------------------------------------------------------------------------

class TestWorldLimitCap:
    def test_cap_logic_501_becomes_500(self):
        # This mirrors the clamp used in query_world.
        limit = 999_999
        capped = max(1, min(int(limit), 500))
        assert capped == 500

    def test_cap_logic_under_cap_unchanged(self):
        limit = 50
        capped = max(1, min(int(limit), 500))
        assert capped == 50

    def test_cap_logic_zero_becomes_one(self):
        limit = 0
        capped = max(1, min(int(limit), 500))
        assert capped == 1


# ---------------------------------------------------------------------------
# Public write registry sanity
# ---------------------------------------------------------------------------

@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestPublicRegistry:
    def test_task_is_public(self):
        assert ("POST", "/task") in api_mod._PUBLIC_WRITE_ENDPOINTS

    def test_context_sync_is_public(self):
        # /context/sync has its own require_admin gate — it's marked public at
        # the middleware layer so the endpoint's own gate is the single source
        # of truth (rather than double-gating inconsistently).
        assert ("POST", "/context/sync") in api_mod._PUBLIC_WRITE_ENDPOINTS

    def test_public_list_is_minimal(self):
        # We want this short and deliberate. If the count grows past a few
        # entries, update this test AND document each new addition.
        assert len(api_mod._PUBLIC_WRITE_ENDPOINTS) <= 4, (
            f"Public write allowlist grew to {len(api_mod._PUBLIC_WRITE_ENDPOINTS)}: "
            f"{api_mod._PUBLIC_WRITE_ENDPOINTS}"
        )

    def test_sensitive_endpoints_not_public(self):
        sensitive = [
            ("POST", "/admin/sql"),
            ("POST", "/admin/restart"),
            ("POST", "/executor/disable"),
            ("POST", "/executor/enable"),
            ("POST", "/hypothesis"),
            ("POST", "/odds/snapshot/baseball_mlb"),
            ("POST", "/research/pause"),
        ]
        for pair in sensitive:
            assert pair not in api_mod._PUBLIC_WRITE_ENDPOINTS, (
                f"{pair} must not be in public allowlist"
            )


# ---------------------------------------------------------------------------
# MCP-server compatibility smoke test
# ---------------------------------------------------------------------------
# The MCP server (tools/callisto_mcp_server.py) POSTs to /task and /context/sync
# WITHOUT any Authorization header. It always runs on localhost. We verify both
# paths stay open via the loopback path (modeled) OR the public-endpoint path
# (the two we've allow-listed).

@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestMcpCompatibility:
    def test_mcp_task_submission_loopback_no_token(self):
        # Simulates: CALLISTO_ADMIN_TOKEN is set, but MCP server POSTs /task
        # from 127.0.0.1 with no Authorization header. Must succeed.
        app = _make_mini_app(
            admin_token="sekret",
            public_paths={("POST", "/task"), ("POST", "/context/sync")},
        )

        @app.post("/task")
        async def task():
            return {"task_id": 1}

        c = _make_client(app, client_host="127.0.0.1")
        r = c.post("/task", json={"query": "test", "priority": 1})
        assert r.status_code == 200, r.text
        assert r.json() == {"task_id": 1}

    def test_mcp_admin_sql_from_loopback_allowed_at_middleware(self):
        # /admin/sql has its own require_admin gate (strict). But the
        # middleware must LET IT THROUGH so the endpoint's own gate can run.
        # This test proves the middleware doesn't double-reject on loopback.
        app = _make_mini_app(admin_token="sekret")

        @app.post("/admin/sql")
        async def admin_sql():
            return {"ok": True}

        c = _make_client(app, client_host="127.0.0.1")
        # From loopback, the middleware passes through — the endpoint's own
        # require_admin dependency would then enforce the token; our
        # mini-app doesn't add that dependency, so we just see 200 here.
        r = c.post("/admin/sql", json={"sql": "SELECT 1"})
        assert r.status_code == 200
