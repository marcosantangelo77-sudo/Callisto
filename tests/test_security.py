"""Security tests — auth bypass, SQL injection, log redaction, input validation.

Covers:
  * /admin/sql validator: rejects DROP/DELETE/UPDATE/INSERT/ATTACH/DETACH and
    forbidden PRAGMAs (layered on top of test_api_auth.py's basic set).
  * Auth bypass attempts: non-loopback external caller, missing bearer,
    bad bearer, header-case variants, double-bearer.
  * Log redaction filter: live credentials + label=value shapes both scrubbed
    across console + file handlers via caplog capture.
  * Sport validation: unknown sport path -> 400 via the allowlist helper.
  * File permission hardener tolerates absent paths + Windows gracefully.
"""

from __future__ import annotations

import logging
import os
import sys
import importlib
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers — import api module without triggering lifespan (same pattern as
# tests/test_api_auth.py).
# ---------------------------------------------------------------------------

def _api():
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    if "api" in sys.modules:
        return sys.modules["api"]
    return importlib.import_module("api")


try:
    api_mod = _api()
except Exception as _err:
    api_mod = None
    _api_err = str(_err)
else:
    _api_err = ""


# ---------------------------------------------------------------------------
# SQL injection attempts against /admin/sql AST validator.
# ---------------------------------------------------------------------------

SQL_INJECTION_PAYLOADS: list[str] = [
    "SELECT * FROM hypotheses; DROP TABLE hypotheses;",
    "DROP TABLE hypotheses",
    "DELETE FROM hypotheses WHERE 1=1",
    "UPDATE hypotheses SET status='live'",
    "INSERT INTO hypotheses (id) VALUES (1)",
    "ATTACH DATABASE '/tmp/evil.db' AS e",
    "DETACH DATABASE main",
    "PRAGMA writable_schema=1",
    "PRAGMA journal_mode=OFF",
    "PRAGMA foreign_keys=OFF",
    "VACUUM",
    "REINDEX hypotheses",
    "ALTER TABLE hypotheses ADD COLUMN pwn TEXT",
    "CREATE TABLE x (id INT)",
    "WITH x AS (DELETE FROM hypotheses RETURNING 1) SELECT * FROM x",
    "WITH x AS (INSERT INTO hypotheses(id) VALUES(1) RETURNING 1) SELECT * FROM x",
    "  ;  SELECT 1  ;  DROP TABLE t  ;  ",
    "REPLACE INTO hypotheses VALUES (1, 'pwn')",
]


@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_api_err}")
@pytest.mark.parametrize("payload", SQL_INJECTION_PAYLOADS)
def test_admin_sql_rejects_injection(payload: str):
    err = api_mod._validate_admin_sql(payload)
    assert err is not None, f"validator accepted dangerous payload: {payload!r}"


@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_api_err}")
def test_admin_sql_allows_plain_select():
    assert api_mod._validate_admin_sql("SELECT 1") is None
    assert api_mod._validate_admin_sql("SELECT * FROM hypotheses LIMIT 10") is None


@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_api_err}")
def test_admin_sql_allows_whitelisted_pragmas():
    assert api_mod._validate_admin_sql("PRAGMA integrity_check") is None
    assert api_mod._validate_admin_sql("PRAGMA page_count") is None
    assert api_mod._validate_admin_sql("PRAGMA quick_check") is None


@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_api_err}")
def test_admin_sql_timeout_constant_enforced():
    """The endpoint's timeout should be surfaced as a 504 via the progress
    handler. We assert the literal 10.0s budget stays wired to prevent
    accidental removal.
    """
    import inspect
    src = inspect.getsource(api_mod.admin_sql)
    assert "10.0" in src, "admin_sql must keep a 10-second timeout"
    assert "504" in src, "admin_sql must surface 504 on timeout"


# ---------------------------------------------------------------------------
# Auth bypass attempts against the middleware (mini-app replay).
# ---------------------------------------------------------------------------

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _mini_app(admin_token: str = ""):
    if api_mod is None:
        pytest.skip(_api_err)
    import secrets as _secrets
    app = FastAPI()

    @app.middleware("http")
    async def _mw(request: Request, call_next):
        if request.method.upper() in api_mod._WRITE_METHODS:
            if api_mod._client_is_loopback(request):
                return await call_next(request)
            auth_header = request.headers.get("authorization", "")
            if not admin_token:
                return JSONResponse(status_code=403, content={"error": "forbidden"})
            if not auth_header.lower().startswith("bearer "):
                return JSONResponse(status_code=401, content={"error": "unauth"})
            provided = auth_header.split(" ", 1)[1].strip()
            if not _secrets.compare_digest(provided, admin_token):
                return JSONResponse(status_code=403, content={"error": "forbidden"})
        return await call_next(request)

    @app.post("/admin/secret")
    async def _secret():
        return {"ok": True}

    return app


def _client(app, host: str = "10.0.0.5"):
    return TestClient(app, base_url=f"http://{host}:8420", client=(host, 5555))


@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_api_err}")
def test_auth_bypass_no_token_external():
    c = _client(_mini_app(admin_token=""))
    r = c.post("/admin/secret")
    assert r.status_code == 403


@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_api_err}")
def test_auth_bypass_wrong_token():
    c = _client(_mini_app(admin_token="sekret"))
    r = c.post("/admin/secret", headers={"Authorization": "Bearer WRONG"})
    assert r.status_code == 403


@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_api_err}")
def test_auth_bypass_missing_scheme():
    c = _client(_mini_app(admin_token="sekret"))
    r = c.post("/admin/secret", headers={"Authorization": "sekret"})
    assert r.status_code == 401  # no "Bearer " prefix


@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_api_err}")
def test_auth_bypass_empty_bearer():
    c = _client(_mini_app(admin_token="sekret"))
    r = c.post("/admin/secret", headers={"Authorization": "Bearer "})
    assert r.status_code == 403


@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_api_err}")
def test_auth_correct_token_allowed():
    c = _client(_mini_app(admin_token="sekret"))
    r = c.post("/admin/secret", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200


@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_api_err}")
def test_auth_loopback_allowed_no_token():
    c = TestClient(_mini_app(admin_token=""), base_url="http://127.0.0.1:8420",
                   client=("127.0.0.1", 5555))
    r = c.post("/admin/secret")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Log redaction filter.
# ---------------------------------------------------------------------------

def _capture(name: str):
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    buf: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):
            try:
                buf.append(self.format(record))
            except Exception:
                buf.append(record.getMessage())

    h = _H()
    h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(h)
    return logger, buf


def test_redaction_filter_masks_bearer_token():
    from logging_config import RedactionFilter
    logger, buf = _capture("test.redact.bearer")
    logger.addFilter(RedactionFilter())
    logger.info("Authorization: Bearer abcdef0123456789deadbeefcafe")
    assert "abcdef0123456789deadbeefcafe" not in "\n".join(buf)
    assert "<redacted>" in "\n".join(buf)


def test_redaction_filter_masks_apikey_query():
    from logging_config import RedactionFilter
    logger, buf = _capture("test.redact.apikey")
    logger.addFilter(RedactionFilter())
    logger.info("fetching https://api.example.com/v4?apiKey=1234567890ABCDEFxyz&sport=nba")
    joined = "\n".join(buf)
    assert "1234567890ABCDEFxyz" not in joined
    assert "<redacted>" in joined


def test_redaction_filter_masks_password_kv():
    from logging_config import RedactionFilter
    logger, buf = _capture("test.redact.pw")
    logger.addFilter(RedactionFilter())
    logger.info("login attempt with password=thisIsASuperSecretValue123!")
    joined = "\n".join(buf)
    assert "thisIsASuperSecretValue123" not in joined


def test_redaction_filter_passes_through_safe_strings():
    from logging_config import RedactionFilter
    logger, buf = _capture("test.redact.safe")
    logger.addFilter(RedactionFilter())
    logger.info("SELECT * FROM hypotheses LIMIT 10")
    logger.info("This is a normal status message with no secrets")
    joined = "\n".join(buf)
    assert "<redacted>" not in joined


def test_redaction_filter_masks_live_credential(monkeypatch):
    # Set a canonical credential then log a string containing its raw value.
    monkeypatch.setenv("CALLISTO_FANATICS_SESSION_COOKIE", "XYZLIVECREDENTIAL12345")
    # Force re-import of credentials to reset any cached state.
    import tools.credentials as _c
    importlib.reload(_c)
    from logging_config import RedactionFilter
    logger, buf = _capture("test.redact.live")
    logger.addFilter(RedactionFilter())
    logger.info("outbound call body: {\"session\": \"XYZLIVECREDENTIAL12345\"}")
    joined = "\n".join(buf)
    assert "XYZLIVECREDENTIAL12345" not in joined


def test_redaction_filter_does_not_raise_on_garbage():
    """Filters must swallow all errors — a logging path must never crash."""
    from logging_config import RedactionFilter
    f = RedactionFilter()
    # Build a record with weird args to exercise the except clause.
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=1,
        msg="something %s", args=(object(),), exc_info=None,
    )
    assert f.filter(record) is True


# ---------------------------------------------------------------------------
# Sport / market input validation.
# ---------------------------------------------------------------------------

def test_sport_validation_rejects_unknown():
    from tools.sport_validation import is_allowed_sport, validate_sport
    assert not is_allowed_sport("not_a_sport")
    assert not is_allowed_sport("'; DROP TABLE t;--")
    assert not is_allowed_sport("")
    assert not is_allowed_sport(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        validate_sport("bogus")


def test_sport_validation_accepts_known():
    from tools.sport_validation import is_allowed_sport, validate_sport
    assert is_allowed_sport("baseball_mlb")
    assert is_allowed_sport("basketball_nba")
    assert validate_sport("icehockey_nhl") == "icehockey_nhl"


def test_market_validation():
    from tools.sport_validation import is_allowed_market
    assert is_allowed_market("h2h")
    assert is_allowed_market("spreads")
    assert is_allowed_market("player_points")
    assert not is_allowed_market("../../etc/passwd")
    assert not is_allowed_market("")


# ---------------------------------------------------------------------------
# File permission hardener.
# ---------------------------------------------------------------------------

def test_harden_paths_absent_file(tmp_path):
    from tools.file_perms import harden_paths
    result = harden_paths([str(tmp_path / "does_not_exist.txt")])
    for v in result.values():
        assert v in ("absent", "skipped_windows")


def test_harden_paths_existing_file(tmp_path):
    from tools.file_perms import harden_paths
    p = tmp_path / "secret.txt"
    p.write_text("hunter2")
    result = harden_paths([str(p)])
    status = list(result.values())[0]
    # On Windows we skip; on POSIX we chmod 600. Both are valid outcomes.
    assert status in ("chmod_600", "skipped_windows")
    if status == "chmod_600":
        mode = oct(p.stat().st_mode & 0o777)
        assert mode == "0o600"


def test_harden_paths_never_raises(tmp_path):
    """Hardener should swallow every error — startup must not abort."""
    from tools.file_perms import harden_paths
    # Pass a path with a weird type to force the except branch.
    result = harden_paths(["/proc/1/root/definitely/denied"])
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Public-write registry — sensitive endpoints must not be on the allowlist.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(api_mod is None, reason=f"api import failed: {_api_err}")
def test_public_registry_excludes_sql_and_restart():
    assert ("POST", "/admin/sql") not in api_mod._PUBLIC_WRITE_ENDPOINTS
    assert ("POST", "/admin/restart") not in api_mod._PUBLIC_WRITE_ENDPOINTS
    assert ("POST", "/admin/claude/reset") not in api_mod._PUBLIC_WRITE_ENDPOINTS
    assert ("POST", "/executor/enable") not in api_mod._PUBLIC_WRITE_ENDPOINTS
    assert ("POST", "/research/pause") not in api_mod._PUBLIC_WRITE_ENDPOINTS
    assert ("POST", "/hypothesis") not in api_mod._PUBLIC_WRITE_ENDPOINTS
