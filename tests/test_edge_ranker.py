"""Unit tests for tools.quant.edge_ranker."""

from datetime import datetime, timedelta, timezone

import pytest

from tools.quant.edge_ranker import (
    MarketSnapshot,
    RankedEdge,
    rank_edges,
    score_edge,
    EDGE_RANKER_SCHEMA_SQL,
)
from tools.quant.consensus_engine import BookLine


def _snap(
    placement_book: str,
    placement_implied: float,
    others: list[tuple[str, float]],
    *,
    outcome: str = "Yankees",
    limit: float = 1000,
    paired_placement: float = None,
    updated_at: str = None,
) -> MarketSnapshot:
    """Build a market snapshot for tests.

    ``others`` is list of (book, implied) for the non-placement books.
    If ``paired_placement`` is set, all lines get paired probs where we
    assume a symmetric (implied, 1-implied+vig) pair.
    """
    if paired_placement is None:
        paired_placement = 1.0 - placement_implied + 0.024  # ~5% two-way vig
    placement_line = BookLine(
        book=placement_book,
        implied_prob=placement_implied,
        paired_implied_prob=paired_placement,
        limit=limit,
        updated_at=updated_at,
    )
    others_lines = [
        BookLine(book=b, implied_prob=p, paired_implied_prob=1.0 - p + 0.024)
        for b, p in others
    ]
    return MarketSnapshot(
        sport="baseball_mlb",
        event_id="E1",
        market="h2h",
        outcome=outcome,
        placement_line=placement_line,
        all_lines=[placement_line] + others_lines,
    )


def test_score_edge_recommends_clear_edge_on_soft_book():
    # Soft book (fanatics) offers 0.460 implied; other books consensus is
    # ~0.510. After devig the placement fair is lower than consensus →
    # positive edge.
    snap = _snap(
        "fanatics", 0.460,
        [("pinnacle", 0.505), ("draftkings", 0.510), ("fanduel", 0.508)],
    )
    e = score_edge(snap)
    assert e.raw_edge > 0.03
    assert e.decision == "recommended"
    assert e.rank is None       # rank is assigned by rank_edges, not score_edge


def test_score_edge_skips_no_edge_market():
    snap = _snap(
        "draftkings", 0.510,
        [("pinnacle", 0.505), ("fanduel", 0.508), ("caesars", 0.512)],
    )
    e = score_edge(snap)
    assert abs(e.raw_edge) < 0.02
    assert e.decision == "skip"


def test_score_edge_holds_on_disagreement_but_big_raw_edge():
    # One book wildly off; raw edge is big enough to be actionable but
    # the consensus disagrees. We want a hold, not a bet.
    snap = _snap(
        "fanatics", 0.460,
        [("pinnacle", 0.505), ("draftkings", 0.510), ("wynn", 0.580)],
    )
    e = score_edge(snap)
    # `raw_edge` will be big (consensus near 0.50, placement near 0.44),
    # but wynn will get trimmed and disagreement fires.
    assert e.disagreement is True


def test_score_edge_detection_risk_penalty_scales_quadratically():
    # Softer book with the same raw edge should get a larger penalty,
    # and bigger edges should get disproportionately larger penalties.
    low_edge_soft = _snap(
        "fanatics", 0.495,
        [("pinnacle", 0.505), ("draftkings", 0.510), ("fanduel", 0.508)],
    )
    high_edge_soft = _snap(
        "fanatics", 0.440,
        [("pinnacle", 0.505), ("draftkings", 0.510), ("fanduel", 0.508)],
    )
    e_low = score_edge(low_edge_soft)
    e_high = score_edge(high_edge_soft)
    # Detection-risk grows faster than edge itself (quadratic shape).
    ratio_edge = e_high.raw_edge / max(e_low.raw_edge, 1e-9)
    ratio_penalty = (e_high.penalty_breakdown["detection_risk"]
                     / max(e_low.penalty_breakdown["detection_risk"], 1e-9))
    assert ratio_penalty > ratio_edge


def test_score_edge_staleness_penalty_ramps_with_age():
    now = datetime(2026, 4, 18, 20, 0, 0, tzinfo=timezone.utc)
    fresh = _snap(
        "fanatics", 0.460,
        [("pinnacle", 0.505), ("draftkings", 0.510), ("fanduel", 0.508)],
        updated_at=now.isoformat(),
    )
    old = _snap(
        "fanatics", 0.460,
        [("pinnacle", 0.505), ("draftkings", 0.510), ("fanduel", 0.508)],
        updated_at=(now - timedelta(minutes=10)).isoformat(),
    )
    e_fresh = score_edge(fresh, now=now)
    e_old = score_edge(old, now=now)
    assert e_old.penalty_breakdown["staleness"] > e_fresh.penalty_breakdown["staleness"]


def test_score_edge_limit_penalty_applies_to_low_limits():
    # Same edge, different limit. Lower limit should carry small penalty.
    high_limit = _snap(
        "fanatics", 0.460,
        [("pinnacle", 0.505), ("draftkings", 0.510), ("fanduel", 0.508)],
        limit=5000,
    )
    low_limit = _snap(
        "fanatics", 0.460,
        [("pinnacle", 0.505), ("draftkings", 0.510), ("fanduel", 0.508)],
        limit=50,
    )
    assert score_edge(low_limit).penalty_breakdown["book_limit"] > \
           score_edge(high_limit).penalty_breakdown["book_limit"]


def test_rank_edges_orders_recommendations_by_effective_edge():
    # Two recommended, one hold. Expect rank=1 for the bigger recommended,
    # rank=2 for the smaller, then the hold, then any skips.
    big = _snap(
        "fanatics", 0.400,
        [("pinnacle", 0.505), ("draftkings", 0.510), ("fanduel", 0.508)],
        outcome="BigEdge",
    )
    small = _snap(
        "fanatics", 0.470,
        [("pinnacle", 0.505), ("draftkings", 0.510), ("fanduel", 0.508)],
        outcome="SmallEdge",
    )
    no_edge = _snap(
        "draftkings", 0.510,
        [("pinnacle", 0.505), ("fanduel", 0.508), ("caesars", 0.512)],
        outcome="NoEdge",
    )
    ranked = rank_edges([small, big, no_edge])
    recommended = [r for r in ranked if r.decision == "recommended"]
    assert len(recommended) >= 2
    assert recommended[0].outcome == "BigEdge"
    assert recommended[0].rank == 1
    assert recommended[1].outcome == "SmallEdge"
    assert recommended[1].rank == 2


def test_rank_edges_respects_top_n():
    # Produce > top_n recommendations and make sure the cap kicks in.
    snaps = [
        _snap(
            "fanatics", 0.420,
            [("pinnacle", 0.505), ("draftkings", 0.510), ("fanduel", 0.508)],
            outcome=f"O{i}",
        )
        for i in range(10)
    ]
    ranked = rank_edges(snaps, top_n=3)
    assert len(ranked) == 3


def test_rank_edges_empty_input_returns_empty_list():
    assert rank_edges([]) == []


def test_schema_sql_is_executable_against_memory_db():
    import sqlite3
    c = sqlite3.connect(":memory:")
    try:
        for stmt in EDGE_RANKER_SCHEMA_SQL.split(";"):
            s = stmt.strip()
            if s:
                c.execute(s)
        # Table exists.
        row = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='live_edge_surface'"
        ).fetchone()
        assert row[0] == 1
    finally:
        c.close()
