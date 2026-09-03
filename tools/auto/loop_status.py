"""AutonomousLoop cache-cleanup and status helpers extracted from tools.auto.loop.

``AutonomousLoop._cleanup_dedup`` and ``get_status`` stay defined on the
class as thin delegates so slice2/slice3 method-name pins keep passing.
The bodies live here so tools/auto/loop.py can keep shrinking without
changing behaviour.

``_loop`` stays on AutonomousLoop for this peel.
Do not import the autonomous facade (no cycles).
Do not arm live betting. Do not add live to paper-signal.
"""
from __future__ import annotations

import time

from tools.auto.loop import (
    ANALYSIS_COOLDOWN,
    EDGE_DEDUP_WINDOW,
    MIN_CONFIDENCE_TO_ALERT,
)


def cleanup_dedup(loop) -> None:
    """Remove old entries from the dedup and injury analysis caches."""
    self = loop
    now = time.time()
    expired = [
        k for k, t in self._analyzed_edges.items()
        if now - t > EDGE_DEDUP_WINDOW * 1.5
    ]
    for k in expired:
        del self._analyzed_edges[k]
    # Hard cap: if cache grows beyond 500 entries, keep only newest 250
    if len(self._analyzed_edges) > 500:
        sorted_keys = sorted(self._analyzed_edges, key=self._analyzed_edges.get)
        for k in sorted_keys[:len(sorted_keys) - 250]:
            del self._analyzed_edges[k]
    # ── Cap ALL in-memory caches to prevent unbounded growth (200 MB/hr leak) ──
    # Injury analysis: LRU-evict oldest entries instead of bulk clear
    if len(self._injury_analysis_cache) > 50:
        # Keep only the 25 most recent entries (approximation: evict half)
        keys = list(self._injury_analysis_cache.keys())
        for k in keys[:len(keys) - 25]:
            del self._injury_analysis_cache[k]
    # Parlay scan: keyed by sport so bounded by sport count (~10) — but
    # clear stale results older than 30 min to free nested data structures
    stale_parlay = [
        s for s, t in self._parlay_scan_ts.items()
        if now - t > 1800
    ]
    for s in stale_parlay:
        self._parlay_scan_cache.pop(s, None)
        self._parlay_scan_ts.pop(s, None)
    # Psychology: same pattern — clear stale entries > 30 min old
    stale_psych = [
        s for s, t in self._psychology_ts.items()
        if now - t > 1800
    ]
    for s in stale_psych:
        self._psychology_cache.pop(s, None)
        self._psychology_ts.pop(s, None)
    # Injury cache: clear stale ESPN injury reports > 30 min old.
    # Without this, _injury_cache grows unbounded (no eviction existed).
    stale_injury = [
        s for s, t in self._injury_ts.items()
        if now - t > 1800
    ]
    for s in stale_injury:
        self._injury_cache.pop(s, None)
        self._injury_ts.pop(s, None)


def get_status(loop) -> dict:
    """Return loop status."""
    self = loop
    now = time.time()
    psych_summary = {}
    for sport, psych in self._psychology_cache.items():
        psych_summary[sport] = {
            "shaded_lines": len(psych.get("number_shading", [])),
            "attention_recommendation": psych.get("attention_arbitrage", {}).get("recommendation", "N/A"),
            "age_seconds": round(now - self._psychology_ts.get(sport, 0)),
        }
    return {
        "running": self._running,
        "sessions_run": self._session_count,
        "alerts_sent": self._alert_count,
        "cached_edge_keys": len(self._analyzed_edges),
        "analysis_cooldown_seconds": ANALYSIS_COOLDOWN,
        "min_confidence_to_alert": MIN_CONFIDENCE_TO_ALERT,
        "market_psychology": psych_summary,
        "parlay_correlation": {
            sport: {
                "amplified_parlays": len(scan.get("amplified_parlays", [])),
                "age_seconds": round(now - self._parlay_scan_ts.get(sport, 0)),
            }
            for sport, scan in self._parlay_scan_cache.items()
        },
    }
