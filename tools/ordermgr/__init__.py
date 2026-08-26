"""tools.ordermgr — helpers extracted from tools.order_manager.

The public API still lives in :mod:`tools.order_manager`; this package
holds the extracted building blocks (states/FSM, ULID, models, transitions,
bets sync) so other subsystems can depend on them directly.
"""

from tools.ordermgr.bets_sync import sync_bets_on_fill, sync_bets_on_settle
from tools.ordermgr.constants import (
    CREATE_ORDERS_TABLE_SQL,
    DB_PATH,
    INSERT_ORDER_SQL,
    OPEN_STATES_SQL,
    ORDER_EXPIRY_MIN,
    ORDERS_INDEXES_SQL,
    USE_ORDER_MANAGER,
)
from tools.ordermgr.models import Order, format_approval_message
from tools.ordermgr.states import (
    ALLOWED_TRANSITIONS,
    APPROVED,
    CANCELLED,
    EXPIRED,
    FILLED,
    InvalidTransition,
    OPEN_STATES,
    OrderNotFound,
    PENDING_APPROVAL,
    REJECTED,
    SETTLED_LOSS,
    SETTLED_PUSH,
    SETTLED_WIN,
    SUBMITTED,
    TERMINAL_STATES,
    assert_transition,
    canonical_settle_result,
)
from tools.ordermgr.transitions import apply_transition
from tools.ordermgr.ulid import new_ulid

__all__ = [
    "ALLOWED_TRANSITIONS",
    "APPROVED",
    "CANCELLED",
    "CREATE_ORDERS_TABLE_SQL",
    "DB_PATH",
    "EXPIRED",
    "FILLED",
    "INSERT_ORDER_SQL",
    "InvalidTransition",
    "OPEN_STATES",
    "OPEN_STATES_SQL",
    "ORDER_EXPIRY_MIN",
    "ORDERS_INDEXES_SQL",
    "Order",
    "OrderNotFound",
    "PENDING_APPROVAL",
    "REJECTED",
    "SETTLED_LOSS",
    "SETTLED_PUSH",
    "SETTLED_WIN",
    "SUBMITTED",
    "TERMINAL_STATES",
    "USE_ORDER_MANAGER",
    "apply_transition",
    "assert_transition",
    "canonical_settle_result",
    "format_approval_message",
    "new_ulid",
    "sync_bets_on_fill",
    "sync_bets_on_settle",
]
