"""Auth/security primitives for the Callisto REST layer (moved from api.py).

Slice 6 of the api.py split: the Bearer-token gates, the loopback check,
the auth-denial audit logger, and the default-secure write-gate logic all
live here now.  api.py keeps thin ``require_admin`` /
``require_admin_or_loopback`` wrappers (FastAPI ``Depends(...)`` targets)
and the ``@app.middleware("http")`` registration, which delegate here.

CRITICAL CONTRACT (pinned by tests/test_api_slice6.py,
tests/test_api_auth.py, tests/test_sensitive_get_gating.py):
  * ``require_admin`` fails CLOSED when CALLISTO_ADMIN_TOKEN is unset
    (503) — never open.
  * ``require_admin_or_loopback`` allows loopback callers without a
    Bearer header even when a token IS configured (the MCP server and the
    local research loop self-consume the API without auth headers).
  * Every 401/403 emits a WARNING on the dedicated ``callisto.api.auth``
    logger so probing is visible in a separate log stream.
  * Only ``request.client.host`` is trusted — never X-Forwarded-For.

api.py globals (``CALLISTO_ADMIN_TOKEN``, ``_auth_logger``,
``_PUBLIC_WRITE_ENDPOINTS``) are read via a LATE ``from api import ...``
inside each function body so tests can ``monkeypatch.setattr(api_mod,
"CALLISTO_ADMIN_TOKEN", ...)`` and have the gate honour it, and to avoid
a circular import at module load time.
"""

from __future__ import annotations

import secrets as _secrets
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def client_is_loopback(request: Request) -> bool:
    """Return True iff the request originated from the local loopback interface.

    Only trusts ``request.client.host`` — never X-Forwarded-For — because
    Callisto binds to 127.0.0.1 by default and does not sit behind a
    trusted proxy. If someone puts it behind one, loopback-trust must be
    revisited.
    """
    host = (request.client.host if request.client else "") or ""
    return host in _LOOPBACK_HOSTS


def log_auth_denied(request: Request, reason: str, status: int) -> None:
    """Emit a WARNING for every 401/403 so probing is visible in logs."""
    from api import _auth_logger  # late import: avoids cycle, honours patching

    host = (request.client.host if request.client else "?") or "?"
    _auth_logger.warning(
        "AUTH_DENIED host=%s method=%s path=%s status=%d reason=%s",
        host, request.method, request.url.path, status, reason,
    )


def admin_token() -> str:
    """Current admin token, read from api.py at call time.

    Reading the attribute on every call (instead of binding at import
    time) keeps ``monkeypatch.setattr(api_mod, "CALLISTO_ADMIN_TOKEN",
    ...)`` effective for the gates defined here.
    """
    from api import CALLISTO_ADMIN_TOKEN  # late import

    return CALLISTO_ADMIN_TOKEN


async def require_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = None,
) -> None:
    """Hard-gate: require Bearer token. Fails closed if token unset."""
    token = admin_token()
    if not token:
        log_auth_denied(request, "admin_token_unset", 503)
        raise HTTPException(
            status_code=503,
            detail="CALLISTO_ADMIN_TOKEN not configured; admin endpoint disabled",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        log_auth_denied(request, "missing_bearer", 401)
        raise HTTPException(status_code=401, detail="Bearer token required")
    if not _secrets.compare_digest(credentials.credentials, token):
        log_auth_denied(request, "bad_token", 403)
        raise HTTPException(status_code=403, detail="Forbidden")


async def require_admin_or_loopback(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = None,
) -> None:
    """Soft-gate for read endpoints. Allow loopback when token unset; otherwise require token."""
    token = admin_token()
    if not token:
        if client_is_loopback(request):
            return
        log_auth_denied(request, "non_loopback_no_token", 403)
        raise HTTPException(status_code=403, detail="Loopback only when admin token unset")
    if credentials is None or credentials.scheme.lower() != "bearer":
        # Loopback path short-circuit even when a token is set: MCP server and
        # local research loop don't send Authorization headers. They still need
        # to self-consume the API. Non-loopback callers must authenticate.
        if client_is_loopback(request):
            return
        log_auth_denied(request, "missing_bearer", 401)
        raise HTTPException(status_code=401, detail="Bearer token required")
    if not _secrets.compare_digest(credentials.credentials, token):
        log_auth_denied(request, "bad_token", 403)
        raise HTTPException(status_code=403, detail="Forbidden")


async def enforce_default_secure(request: Request) -> Optional[JSONResponse]:
    """Default-secure write-gate core.

    Returns ``None`` when the request may proceed, or a ready-to-return
    ``JSONResponse`` (401/403) when a non-loopback write is not on the
    public allowlist and fails authentication. Reads the allowlist and
    the admin token from api.py at call time.
    """
    from api import _PUBLIC_WRITE_ENDPOINTS  # late import

    method = request.method.upper()
    if method not in {"POST", "PATCH", "PUT", "DELETE"}:
        return None
    path = request.url.path
    if (method, path) in _PUBLIC_WRITE_ENDPOINTS:
        return None
    # Inline the token/loopback check so we can return JSON rather than
    # let HTTPException bubble up before routing.
    if client_is_loopback(request):
        # Loopback always allowed — MCP server & research loop path.
        return None
    auth_header = request.headers.get("authorization", "")
    token = admin_token()
    if not token:
        log_auth_denied(request, "non_loopback_no_token", 403)
        return JSONResponse(
            status_code=403,
            content={"error": "Loopback only when admin token unset", "status": 403},
        )
    if not auth_header.lower().startswith("bearer "):
        log_auth_denied(request, "missing_bearer", 401)
        return JSONResponse(
            status_code=401,
            content={"error": "Bearer token required", "status": 401},
        )
    provided = auth_header.split(" ", 1)[1].strip()
    if not _secrets.compare_digest(provided, token):
        log_auth_denied(request, "bad_token", 403)
        return JSONResponse(
            status_code=403,
            content={"error": "Forbidden", "status": 403},
        )
    return None
