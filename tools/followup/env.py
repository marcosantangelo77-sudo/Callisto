"""tools.followup.env — environment toggles and cost-model defaults.

Every guard in the followup package is toggleable via env vars and
defaults ON. This module is the single place that reads them so the
rest of the package stays free of ``os.getenv`` noise.
"""

from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def max_depth() -> int:
    return _env_int("CALLISTO_MAX_FOLLOWUP_DEPTH", 5)


def max_fanout() -> int:
    return _env_int("CALLISTO_MAX_FOLLOWUP_FANOUT", 3)


def max_chain_budget_usd() -> float:
    return _env_float("CALLISTO_MAX_CHAIN_BUDGET_USD", 1.00)


def dedup_enabled() -> bool:
    return _env_bool("CALLISTO_FOLLOWUP_DEDUP", True)


def quality_gate_enabled() -> bool:
    return _env_bool("CALLISTO_FOLLOWUP_QUALITY_GATE", True)


def dedup_window_seconds() -> int:
    return _env_int("CALLISTO_FOLLOWUP_DEDUP_WINDOW_S", 3600)


def dedup_threshold() -> float:
    return _env_float("CALLISTO_FOLLOWUP_DEDUP_THRESHOLD", 0.95)


# ── Cost model ───────────────────────────────────────────────────────────
# Rough per-task cost estimate used for chain-budget accounting. We don't
# have a live per-request cost hook yet, so we use a conservative bucket:
# one Claude escalation averages ~$0.10 on Opus 4.6 at typical token
# counts, and most tasks escalate once. Callers that have a precise cost
# can pass it via ``record_task_cost``.
DEFAULT_TASK_COST_USD = _env_float("CALLISTO_DEFAULT_TASK_COST_USD", 0.10)
