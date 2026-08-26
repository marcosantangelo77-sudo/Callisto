"""ASCII bankroll-distribution histogram for CLI output."""

from __future__ import annotations

import numpy as np


def ascii_bankroll_histogram(
    final_bankrolls: np.ndarray,
    starting_bankroll: float,
    width: int = 60,
    bins: int = 20,
) -> str:
    """Return a human-readable ASCII histogram of final bankrolls."""
    if final_bankrolls is None or len(final_bankrolls) == 0:
        return "(no data)"
    lo = float(np.min(final_bankrolls))
    hi = float(np.max(final_bankrolls))
    if hi <= lo:
        return f"(all paths ended at ${lo:,.0f})"
    hist, edges = np.histogram(final_bankrolls, bins=bins, range=(lo, hi))
    peak = int(np.max(hist))
    lines = []
    lines.append(f"Bankroll distribution (start=${starting_bankroll:,.0f}):")
    for i, count in enumerate(hist):
        left = edges[i]
        right = edges[i + 1]
        bar_len = int(round(width * count / peak)) if peak > 0 else 0
        bar = "#" * bar_len
        pct = 100.0 * count / len(final_bankrolls)
        lines.append(
            f"  ${left:>8,.0f} - ${right:>8,.0f} | {bar:<{width}} {count:>5} ({pct:4.1f}%)"
        )
    return "\n".join(lines)
