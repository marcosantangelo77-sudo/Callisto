"""Per-event dedup in _get_backtest_signals:

Pre-audit: kept the BEST-edge row per event_id (selection bias for props
where one game has many player-prop rows).

Post-fix: random_row (default) picks one row deterministically; composite
aggregates; best_edge kept only for legacy hypotheses.

This test is self-contained: it constructs synthetic rows and invokes the
collapse logic directly (extracted from the manager) so it doesn't need a
live DB or aiosqlite fixture.
"""
from __future__ import annotations

import random


def _collapse_random_row(rows: list[dict], hyp_id: str) -> list[dict]:
    by_event: dict[str, list[dict]] = {}
    for r in rows:
        by_event.setdefault(r["event_id"], []).append(r)
    out = []
    for eid, group in by_event.items():
        if len(group) == 1:
            out.append(group[0]); continue
        rng = random.Random(f"{hyp_id}|{eid}")
        out.append(rng.choice(group))
    return out


def _collapse_best_edge(rows: list[dict]) -> list[dict]:
    by_event: dict[str, dict] = {}
    for r in rows:
        eid = r["event_id"]
        if eid not in by_event or (r.get("edge") or 0) > (by_event[eid].get("edge") or 0):
            by_event[eid] = r
    return list(by_event.values())


def test_best_edge_selects_maximum():
    """Baseline: legacy best_edge picks edge=0.10."""
    rows = [
        {"event_id": "g1", "edge": 0.02, "actual_result": "lost"},
        {"event_id": "g1", "edge": 0.04, "actual_result": "lost"},
        {"event_id": "g1", "edge": 0.06, "actual_result": "won"},
        {"event_id": "g1", "edge": 0.08, "actual_result": "won"},
        {"event_id": "g1", "edge": 0.10, "actual_result": "won"},
    ]
    out = _collapse_best_edge(rows)
    assert len(out) == 1
    assert out[0]["edge"] == 0.10


def test_random_row_mode_not_always_best():
    """Random-row across many hypothesis_ids yields a non-best pick at least sometimes."""
    rows = [
        {"event_id": "g1", "edge": 0.02},
        {"event_id": "g1", "edge": 0.04},
        {"event_id": "g1", "edge": 0.06},
        {"event_id": "g1", "edge": 0.08},
        {"event_id": "g1", "edge": 0.10},
    ]
    picks = [
        _collapse_random_row(rows, f"hyp_{i}")[0]["edge"] for i in range(100)
    ]
    # Very unlikely to pick 0.10 every single time
    non_best = sum(1 for p in picks if p != 0.10)
    assert non_best > 0, "random_row is deterministically picking the best edge"
    # And the distribution should be non-degenerate across all five edges.
    assert len(set(picks)) > 1


def test_random_row_is_deterministic_per_hypothesis():
    """Same hypothesis_id + event_id → same row twice."""
    rows = [{"event_id": "g1", "edge": e} for e in (0.02, 0.04, 0.06, 0.08, 0.10)]
    a = _collapse_random_row(rows, "hyp_X")[0]["edge"]
    b = _collapse_random_row(rows, "hyp_X")[0]["edge"]
    assert a == b


def test_random_row_more_conservative_on_p_value():
    """With many 1-win / 4-loss games, best_edge picks the WIN row (selection
    bias); random_row averages to 1/5 wins per game, producing a more
    conservative (higher) p-value.
    """
    # Simulate 10 games, each with 5 rows, each with 1 winner (edge=0.10) + 4 losers
    rows = []
    for g in range(10):
        for i, edge in enumerate((0.02, 0.04, 0.06, 0.08, 0.10)):
            rows.append({
                "event_id": f"g{g}",
                "edge": edge,
                # Only the highest-edge row "wins" — classic selection bias.
                "actual_result": "won" if i == 4 else "lost",
            })

    best = _collapse_best_edge(rows)
    # 10 "won" rows under best_edge → hit rate 100%
    best_wins = sum(1 for r in best if r["actual_result"] == "won")
    assert best_wins == 10

    # Random row over 20 deterministic shufflings — expected hit rate ~20%
    hit_rates = []
    for trial in range(20):
        out = _collapse_random_row(rows, f"trial_{trial}")
        wins = sum(1 for r in out if r["actual_result"] == "won")
        hit_rates.append(wins / len(out))

    mean_hit = sum(hit_rates) / len(hit_rates)
    # Expected ~0.20; allow wide band because only 10 games each trial
    assert 0.05 <= mean_hit <= 0.40, f"random_row mean hit={mean_hit:.2f}"
    # And clearly lower than best_edge's 100%
    assert mean_hit < 0.50
