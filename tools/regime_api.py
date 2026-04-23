"""
regime_api — FastAPI sub-app exposing market-regime signals.

Kept in its own module so api.py can wire it in later with a single
``app.mount("/regime", regime_api.sub_app)`` or
``app.include_router(regime_api.router)`` without this PR having to
touch api.py (explicit isolation requirement — api.py is reserved by
another in-flight branch).

Endpoints:
    GET /regime/{sport}          — full regime payload
    GET /regime/{sport}/safe     — {"safe": bool}
    GET /regime/{sport}/multiplier — {"multiplier": float}

The caller (api.py) can mount this sub_app at any prefix; the routes
here are written with ``/regime`` already included so both forms work:

    app.mount("/", regime_api.sub_app)        # routes served at /regime/*
    # or
    app.include_router(regime_api.router)     # router carries /regime prefix
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

logger = logging.getLogger("callisto.regime_api")

# FastAPI is a hard dep of Callisto's api.py, but keep the import guarded
# so importing this module in a minimal test env (no fastapi installed)
# still exposes a plain build_payload() function.
try:
    from fastapi import APIRouter, FastAPI, HTTPException, Query
    _HAS_FASTAPI = True
except Exception:  # pragma: no cover — defensive
    _HAS_FASTAPI = False
    APIRouter = None  # type: ignore[assignment]
    FastAPI = None  # type: ignore[assignment]
    HTTPException = None  # type: ignore[assignment]
    Query = None  # type: ignore[assignment]

from tools.market_regime import (
    current_regime_multiplier,
    detect_regime,
    regime_safe_for_trading,
)


def build_payload(sport: str, as_of_iso: str | None = None) -> dict[str, Any]:
    """Pure function: compute the regime payload. Used by routes and
    callable directly from tests without requiring FastAPI's TestClient."""
    if as_of_iso:
        try:
            as_of = date.fromisoformat(as_of_iso)
        except ValueError as exc:
            raise ValueError(f"invalid as_of date: {as_of_iso}") from exc
    else:
        as_of = date.today()
    regime = detect_regime(sport, as_of)
    payload = regime.to_dict()
    payload["multiplier"] = current_regime_multiplier(sport, as_of)
    payload["safe_for_trading"] = regime_safe_for_trading(sport, as_of)
    return payload


if _HAS_FASTAPI:
    router = APIRouter(prefix="/regime", tags=["regime"])

    @router.get("/{sport}")
    def get_regime(
        sport: str,
        as_of: str | None = Query(
            None,
            description="Override date (YYYY-MM-DD). Defaults to today.",
        ),
    ) -> dict[str, Any]:
        try:
            return build_payload(sport, as_of)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("regime lookup failed: %s", exc)
            raise HTTPException(status_code=500, detail="regime lookup failed")

    @router.get("/{sport}/safe")
    def get_regime_safe(sport: str) -> dict[str, Any]:
        return {"sport": sport, "safe_for_trading": regime_safe_for_trading(sport)}

    @router.get("/{sport}/multiplier")
    def get_regime_multiplier(sport: str) -> dict[str, Any]:
        return {"sport": sport, "multiplier": current_regime_multiplier(sport)}

    # Stand-alone sub-app — api.py can mount this at / and the routes
    # will appear at /regime/* exactly as above.
    sub_app = FastAPI(
        title="Callisto Regime API",
        description="Market-regime signals (standalone sub-app).",
    )
    sub_app.include_router(router)
else:  # pragma: no cover
    router = None
    sub_app = None


__all__ = ["build_payload", "router", "sub_app"]
