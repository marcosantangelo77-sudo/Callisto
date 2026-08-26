"""Order FSM states, transitions table, and errors.

Extracted from ``tools.order_manager`` so other subsystems can depend on
the state machine without pulling in the manager itself.
"""

from __future__ import annotations

# --- States ----------------------------------------------------------------

PENDING_APPROVAL = "pending_approval"
APPROVED = "approved"
SUBMITTED = "submitted"
FILLED = "filled"
REJECTED = "rejected"
CANCELLED = "cancelled"
SETTLED_WIN = "settled_win"
SETTLED_LOSS = "settled_loss"
SETTLED_PUSH = "settled_push"
EXPIRED = "expired"

OPEN_STATES = frozenset({PENDING_APPROVAL, APPROVED, SUBMITTED, FILLED})
TERMINAL_STATES = frozenset(
    {REJECTED, CANCELLED, EXPIRED, SETTLED_WIN, SETTLED_LOSS, SETTLED_PUSH}
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING_APPROVAL: frozenset({APPROVED, REJECTED, EXPIRED, CANCELLED}),
    APPROVED: frozenset({SUBMITTED, CANCELLED, REJECTED}),
    SUBMITTED: frozenset({FILLED, CANCELLED, REJECTED}),
    FILLED: frozenset({SETTLED_WIN, SETTLED_LOSS, SETTLED_PUSH, CANCELLED}),
    REJECTED: frozenset(),
    CANCELLED: frozenset(),
    EXPIRED: frozenset(),
    SETTLED_WIN: frozenset(),
    SETTLED_LOSS: frozenset(),
    SETTLED_PUSH: frozenset(),
}


class InvalidTransition(ValueError):
    """Attempted FSM transition is not in :data:`ALLOWED_TRANSITIONS`."""


class OrderNotFound(LookupError):
    """No row in ``orders`` for the given order_id."""


_SETTLE_ALIASES = {
    "win": SETTLED_WIN,
    "won": SETTLED_WIN,
    "settled_win": SETTLED_WIN,
    "loss": SETTLED_LOSS,
    "lost": SETTLED_LOSS,
    "settled_loss": SETTLED_LOSS,
    "push": SETTLED_PUSH,
    "settled_push": SETTLED_PUSH,
}


def canonical_settle_result(result: str) -> str:
    """Map a short or long settle result to its canonical state name."""
    canonical = _SETTLE_ALIASES.get((result or "").lower())
    if not canonical:
        raise ValueError(f"Unknown settle result: {result!r}")
    return canonical


def assert_transition(current_state: str, new_state: str, order_id: str) -> None:
    """Raise :class:`InvalidTransition` unless the edge is legal."""
    allowed = ALLOWED_TRANSITIONS.get(current_state, frozenset())
    if new_state not in allowed:
        raise InvalidTransition(
            f"Cannot transition {order_id} from {current_state} to "
            f"{new_state}; allowed: {sorted(allowed)}"
        )
