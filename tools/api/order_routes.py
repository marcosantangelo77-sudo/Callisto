"""Executor/order route handler bodies (moved from api.py).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

GATING CONTRACT (pinned by tests/test_api_slice3.py):
  * /executor/enable, /orders/*/approve|reject|fill, /orders/reconcile
    writes keep require_admin. Never downgraded to loopback-allowing.
  * Read-only order/executor status routes keep require_admin_or_loopback.

Handlers access api.py's module-level singletons (``app``,
``order_manager_instance``, ``reconcile_filled_orders``,
``detect_voided_orders``) via late ``from api import ...`` inside the
function body to avoid a circular import at module load time.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

_executor = None


async def get_executor():
    global _executor
    if _executor is None:
        from tools.bet_executor import BetExecutor
        _executor = BetExecutor()
        await _executor.initialize()
    return _executor


def _require_order_manager():
    from api import order_manager_instance
    if order_manager_instance is None:
        raise HTTPException(503, "order_manager not initialised")
    return order_manager_instance


async def executor_status():
    """Get bet executor status."""
    ex = await get_executor()
    return await ex.status()


async def executor_enable():
    """Enable both the order manager and the legacy bet executor.

    The order_manager is the default active subsystem
    (CALLISTO_USE_ORDER_MANAGER=1); bet_executor is kept enabled as
    fallback. Flipping either flag off is an explicit /pause via the
    subsystem-specific endpoint below.
    """
    from api import app, order_manager_instance
    ex = await get_executor()
    ex.enable()
    # Wire into research loop if available
    if hasattr(app.state, "research_loop"):
        app.state.research_loop._bet_executor = ex
    om = order_manager_instance
    if om is not None:
        om.enable()
    return {
        "status": "enabled",
        "order_manager": om.is_enabled if om else None,
        "bet_executor": ex.is_enabled,
        "message": "Order manager + bet executor are LIVE",
    }


async def executor_disable():
    """Disable both subsystems — no orders will be submitted or placed."""
    from api import order_manager_instance
    ex = await get_executor()
    ex.disable()
    om = order_manager_instance
    if om is not None:
        om.disable()
    return {
        "status": "disabled",
        "message": "Order manager + bet executor disabled",
    }


_ORDER_SUMMARY_FIELDS = (
    "order_id", "hypothesis_id", "signal_id", "sport", "event_id",
    "market", "side", "price_american", "stake_units", "stake_dollars",
    "state", "book", "placed_at", "settled_at", "pnl_dollars",
    "expires_at", "created_at", "bet_id", "edge",
)


def _summarize_order(o) -> dict:
    return {field: getattr(o, field) for field in _ORDER_SUMMARY_FIELDS}


async def orders_list(state: Optional[str] = None, limit: int = 50):
    """List orders, optionally filtered by state."""
    om = _require_order_manager()
    rows = await om.list_orders(state=state, limit=limit)
    return {"count": len(rows), "orders": [_summarize_order(o) for o in rows]}


async def orders_get(order_id: str):
    """Fetch one order including full state history."""
    from tools.order_manager import OrderNotFound
    om = _require_order_manager()
    try:
        o = await om.get_order(order_id)
    except OrderNotFound:
        raise HTTPException(404, f"order {order_id} not found")
    d = _summarize_order(o)
    d.update({
        "odds_snapshot_id": o.odds_snapshot_id,
        "state_history": o.state_history,
        "fair_prob": o.fair_prob,
    })
    return d


async def orders_approve(order_id: str):
    from tools.order_manager import OrderNotFound, InvalidTransition
    om = _require_order_manager()
    try:
        o = await om.approve(order_id, reason="http_approve")
    except OrderNotFound:
        raise HTTPException(404, f"order {order_id} not found")
    except InvalidTransition as e:
        raise HTTPException(409, str(e))
    return {"status": "approved", "order_id": o.order_id, "state": o.state}


async def orders_reject(order_id: str, reason: str = "http_reject"):
    from tools.order_manager import OrderNotFound, InvalidTransition
    om = _require_order_manager()
    try:
        o = await om.reject(order_id, reason=reason)
    except OrderNotFound:
        raise HTTPException(404, f"order {order_id} not found")
    except InvalidTransition as e:
        raise HTTPException(409, str(e))
    return {"status": "rejected", "order_id": o.order_id, "state": o.state}


async def orders_fill(order_id: str, actual_price: Optional[int] = None):
    from tools.order_manager import OrderNotFound, InvalidTransition
    om = _require_order_manager()
    try:
        o = await om.mark_filled(
            order_id, actual_price=actual_price, reason="http_fill"
        )
    except OrderNotFound:
        raise HTTPException(404, f"order {order_id} not found")
    except InvalidTransition as e:
        raise HTTPException(409, str(e))
    return {"status": "filled", "order_id": o.order_id, "state": o.state,
            "price_american": o.price_american}


async def orders_reconcile():
    """Trigger the settlement reconciler immediately (cron path)."""
    from api import reconcile_filled_orders
    om = _require_order_manager()
    stats = await reconcile_filled_orders(om)
    return {"status": "ok", **stats}


async def orders_voids():
    """Trigger the postponed/cancelled game void-detector immediately."""
    from api import detect_voided_orders
    om = _require_order_manager()
    stats = await detect_voided_orders(om)
    return {"status": "ok", **stats}


async def orders_expire():
    """Trigger the expiry sweep immediately."""
    om = _require_order_manager()
    expired = await om.expire_stale()
    return {"status": "ok", "expired": expired, "count": len(expired)}


async def executor_login():
    """Launch browser for DraftKings login. Browser opens visible for manual login."""
    ex = await get_executor()
    logged_in = await ex.ensure_logged_in()
    if logged_in:
        return {"status": "logged_in", "message": "DraftKings session active"}
    else:
        return {
            "status": "login_required",
            "message": "Browser opened — please log into DraftKings manually. Session will persist.",
        }
