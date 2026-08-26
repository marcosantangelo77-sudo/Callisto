"""Cycle-health helpers extracted from ResearchLoop (tools/autonomous.py).

Pure functions over ``(cycles, ledger)``; the ResearchLoop methods are
one-line wrappers that keep the get_status keys stable.
"""

from __future__ import annotations


def last_cycle_phase_failures(cycles: int, ledger) -> int:
    """Count ledger entries whose cycle == cycles. 0 if cycles == 0.

    Older cycles stay on the ledger for history; this count is only
    ``cycle == cycles`` so a clean cycle reports 0 even if a previous
    cycle failed.
    """
    if cycles == 0:
        return 0
    return sum(1 for entry in ledger.latest(ledger.count) if entry["cycle"] == cycles)


def last_cycle_ok(cycles: int, ledger) -> bool:
    """True iff no phase failed during the current cycle.

    Failures are non-fatal (the loop continues), but a cycle in which any
    phase failed or timed out must not report as healthy. If no cycle has
    run yet (``cycles == 0``), the loop is healthy.
    """
    return last_cycle_phase_failures(cycles, ledger) == 0
