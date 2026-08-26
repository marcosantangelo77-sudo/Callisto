"""Edge ranker + persistent live edge surface.

Given a snapshot of every currently open market across all books, the
edge ranker:

    1. Computes the consensus fair probability for each market × outcome
       using :mod:`tools.quant.consensus_engine`.
    2. Devigs each placement-book line into a fair-probability estimate.
    3. Computes the raw edge as (consensus_fair − placement_fair).
    4. Applies penalties for detection risk, book limit, staleness, and
       consensus disagreement. The result is an *effective* edge, which
       is what Kelly sizing should consume.
    5. Ranks every outcome by effective edge and returns the top N.

The per-market row lands in ``live_edge_surface`` so downstream clients
(the /edges/live API endpoint, the Telegram listener, Marco's dashboard)
can poll a single table instead of recomputing. The computed_at
timestamp lets consumers detect stale rows.

The ranker is deliberately decoupled from the bet_executor — it only
produces recommendations. Placement, stake sizing, and counterparty
selection are the next module's job. This is the "prices" layer; the
"orders" layer is separate.
"""

from __future__ import annotations

import math

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .consensus_engine import (
    BookLine,
    BOOK_TIER,
    ConsensusResult,
    compute_consensus_fair_prob,
    multiplicative_devig,
)


EDGE_RANKER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS live_edge_surface (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at        TEXT NOT NULL,
    sport              TEXT NOT NULL,
    event_id           TEXT NOT NULL,
    market             TEXT NOT NULL,
    outcome            TEXT NOT NULL,
    placement_book     TEXT NOT NULL,
    placement_implied  REAL NOT NULL,
    placement_fair     REAL NOT NULL,
    consensus_fair     REAL NOT NULL,
    consensus_std_err  REAL,
    raw_edge           REAL NOT NULL,
    effective_edge     REAL NOT NULL,
    penalty_total      REAL NOT NULL,
    penalty_breakdown  TEXT NOT NULL,
    disagreement       INTEGER DEFAULT 0,
    n_books            INTEGER NOT NULL,
    outlier_books      TEXT,
    decision           TEXT NOT NULL,
    rank               INTEGER
);

CREATE INDEX IF NOT EXISTS idx_edge_surface_recency
    ON live_edge_surface(computed_at);
CREATE INDEX IF NOT EXISTS idx_edge_surface_rank
    ON live_edge_surface(computed_at, rank);
CREATE INDEX IF NOT EXISTS idx_edge_surface_sport
    ON live_edge_surface(sport, computed_at);
CREATE INDEX IF NOT EXISTS idx_edge_surface_event
    ON live_edge_surface(event_id, market);
"""


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything the ranker needs to score one market × one outcome.

    ``placement_line`` is the book we'd actually bet at. The other
    entries in ``all_lines`` (which must include ``placement_line``)
    contribute to the consensus. If ``all_lines`` has only one book the
    ranker will return a hold decision — we can't defensibly call a
    single-book line "consensus".
    """
    sport: str
    event_id: str
    market: str
    outcome: str
    placement_line: BookLine
    all_lines: list[BookLine]
    commence_time: Optional[datetime] = None
    sharp_signal_count: int = 0
    recent_limit_down: bool = False


@dataclass(frozen=True)
class RankedEdge:
    """One scored edge, produced by :func:`score_edge`."""
    sport: str
    event_id: str
    market: str
    outcome: str
    placement_book: str
    placement_implied: float
    placement_fair: float
    consensus_fair: float
    consensus_std_err: float
    raw_edge: float
    effective_edge: float
    penalty_total: float
    penalty_breakdown: dict[str, float] = field(default_factory=dict)
    disagreement: bool = False
    n_books: int = 0
    outlier_books: tuple[str, ...] = ()
    decision: str = "hold"
    rank: Optional[int] = None


# ──────────────────────────────────────────────────────────────────────
# Penalty system
# ──────────────────────────────────────────────────────────────────────


def _detection_risk_penalty(book: str, raw_edge: float) -> float:
    """Penalise big edges on books that aggressively limit winners.

    Books track CLV; beating the close too much too consistently gets
    you limited. ``penalty = 3 × edge² × softness`` — quadratic in edge
    so small edges eat small penalties and giant edges eat most of
    themselves, which is exactly the shape you want for stealth.
    """
    softness = {"soft": 1.0, "reference": 0.5, "sharp": 0.0}.get(
        BOOK_TIER.get(book.lower(), "soft"), 1.0
    )
    return 3.0 * max(raw_edge, 0.0) ** 2 * softness


def _limit_penalty(limit: Optional[float]) -> float:
    """Small penalty for markets with very low limits — not *wrong*, but
    not scalable; the ranker should prefer markets with headroom."""
    if limit is None or limit >= 500:
        return 0.0
    if limit >= 100:
        return 0.002
    return 0.005


def _staleness_penalty(updated_at: Optional[str], now: datetime) -> float:
    """Older lines are partially priced through. Linear ramp from 1 min
    old (no penalty) to 15 min old (full 1.5% penalty)."""
    if not updated_at:
        return 0.0
    try:
        then = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    age_s = max(0.0, (now - then).total_seconds())
    if age_s < 60:
        return 0.0
    if age_s >= 900:
        return 0.015
    return 0.015 * (age_s - 60) / (900 - 60)


def _disagreement_penalty(disagreement: bool, std_err: float) -> float:
    """Penalise proportionally when consensus has high variance."""
    base = 0.003 if disagreement else 0.0
    return base + min(std_err * 0.3, 0.01)


def _recent_limit_down_penalty(recent_limit_down: bool) -> float:
    """Placement book just limit-dropped — it likely knows something."""
    return 0.004 if recent_limit_down else 0.0


# ──────────────────────────────────────────────────────────────────────
# Core scoring
# ──────────────────────────────────────────────────────────────────────


def _devig_placement(line: BookLine) -> Optional[float]:
    """Fair probability implied by the placement book's single line.

    Returns None when the placement book fails the shared market-sanity
    gate (zero-hold, crossed, excessive hold, non-finite): an invalid
    placement must never be scored into a recommendation.
    """
    from tools.devig import validate_implied_book
    if line.paired_implied_prob is not None and line.paired_implied_prob > 0:
        try:
            validate_implied_book([line.implied_prob, line.paired_implied_prob])
        except ValueError:
            return None
        fair, _ = multiplicative_devig(line.implied_prob, line.paired_implied_prob)
        return fair
    ip = line.implied_prob
    if isinstance(ip, bool) or not isinstance(ip, (int, float)) \
            or not math.isfinite(ip) or not 0.0 < ip < 1.0:
        return None
    tier = BOOK_TIER.get(line.book.lower(), "soft")
    prior_vig = {"sharp": 0.025, "reference": 0.05, "soft": 0.06}.get(tier, 0.05)
    return max(0.001, min(0.999, ip / (1.0 + prior_vig / 2.0)))


def score_edge(
    snap: MarketSnapshot,
    *,
    min_recommend_edge: float = 0.02,
    now: Optional[datetime] = None,
) -> RankedEdge:
    """Score one market × outcome and return a ranked-edge record."""
    now = now or datetime.now(timezone.utc)

    consensus: ConsensusResult = compute_consensus_fair_prob(snap.all_lines)
    placement_fair = _devig_placement(snap.placement_line)
    if placement_fair is None:
        # Placement book failed the market-sanity gate: never a recommendation.
        return RankedEdge(
            sport=snap.sport,
            event_id=snap.event_id,
            market=snap.market,
            outcome=snap.outcome,
            placement_book=snap.placement_line.book,
            placement_implied=snap.placement_line.implied_prob,
            placement_fair=float("nan"),
            consensus_fair=consensus.fair_prob,
            consensus_std_err=consensus.std_err,
            raw_edge=float("nan"),
            effective_edge=float("nan"),
            penalty_total=0.0,
            penalty_breakdown={},
            disagreement=True,
            n_books=consensus.n_books,
            outlier_books=tuple(consensus.outlier_books),
            decision="skip",
        )
    raw_edge = consensus.fair_prob - placement_fair

    penalties = {
        "detection_risk": _detection_risk_penalty(snap.placement_line.book, raw_edge),
        "book_limit":     _limit_penalty(snap.placement_line.limit),
        "staleness":      _staleness_penalty(snap.placement_line.updated_at, now),
        "disagreement":   _disagreement_penalty(consensus.disagreement, consensus.std_err),
        "recent_limit_down": _recent_limit_down_penalty(snap.recent_limit_down),
    }
    penalty_total = sum(penalties.values())
    effective_edge = raw_edge - penalty_total

    if effective_edge >= min_recommend_edge and consensus.n_books >= 2:
        decision = "recommended"
    elif raw_edge >= min_recommend_edge and consensus.disagreement:
        decision = "hold"
    else:
        decision = "skip"

    return RankedEdge(
        sport=snap.sport,
        event_id=snap.event_id,
        market=snap.market,
        outcome=snap.outcome,
        placement_book=snap.placement_line.book,
        placement_implied=snap.placement_line.implied_prob,
        placement_fair=placement_fair,
        consensus_fair=consensus.fair_prob,
        consensus_std_err=consensus.std_err,
        raw_edge=raw_edge,
        effective_edge=effective_edge,
        penalty_total=penalty_total,
        penalty_breakdown=penalties,
        disagreement=consensus.disagreement,
        n_books=consensus.n_books,
        outlier_books=tuple(consensus.outlier_books),
        decision=decision,
    )


def rank_edges(
    snapshots: list[MarketSnapshot],
    *,
    min_recommend_edge: float = 0.02,
    top_n: int = 50,
    now: Optional[datetime] = None,
) -> list[RankedEdge]:
    """Score every snapshot, sort by effective edge, return top N.

    Order is: ``recommended`` (by effective edge desc), then ``hold`` (by
    raw edge desc), then ``skip`` (by raw edge desc). ``rank`` is
    1-indexed only for ``recommended`` rows so callers can easily filter
    the actionable surface.
    """
    scored = [score_edge(s, min_recommend_edge=min_recommend_edge, now=now) for s in snapshots]

    recommended = sorted(
        (e for e in scored if e.decision == "recommended"),
        key=lambda e: e.effective_edge,
        reverse=True,
    )
    held = sorted(
        (e for e in scored if e.decision == "hold"),
        key=lambda e: e.raw_edge,
        reverse=True,
    )
    skipped = sorted(
        (e for e in scored if e.decision == "skip"),
        key=lambda e: e.raw_edge,
        reverse=True,
    )

    ranked: list[RankedEdge] = []
    for i, e in enumerate(recommended, start=1):
        ranked.append(RankedEdge(**{**e.__dict__, "rank": i}))
    ranked.extend(held)
    ranked.extend(skipped)
    return ranked[:top_n]


async def persist_ranked_edges(
    db,
    ranked: list[RankedEdge],
    *,
    computed_at: Optional[str] = None,
    chunk_size: int = 500,
) -> int:
    """Insert a batch of ranked edges into live_edge_surface.

    Each call produces ONE snapshot in time. Consumers query with
    ``WHERE computed_at = (SELECT MAX(computed_at) FROM live_edge_surface)``
    for the latest ranking or ``GROUP BY event_id, market, outcome`` to
    track edge persistence across snapshots.

    Chunks at ``chunk_size`` rows per executemany so the WriteCoordinator
    queue can interleave small writes (hermes learnings, hypothesis
    updates, task-queue inserts) between chunks instead of stalling
    behind one large batch.
    """
    import json as _json
    if not ranked:
        return 0
    ts = computed_at or datetime.now(timezone.utc).isoformat()
    rows = [
        (
            ts,
            e.sport, e.event_id, e.market, e.outcome,
            e.placement_book, e.placement_implied, e.placement_fair,
            e.consensus_fair, e.consensus_std_err,
            e.raw_edge, e.effective_edge, e.penalty_total,
            _json.dumps(e.penalty_breakdown),
            1 if e.disagreement else 0,
            e.n_books,
            _json.dumps(list(e.outlier_books)),
            e.decision, e.rank,
        )
        for e in ranked
    ]
    sql = (
        "INSERT INTO live_edge_surface ("
        "computed_at, sport, event_id, market, outcome, "
        "placement_book, placement_implied, placement_fair, "
        "consensus_fair, consensus_std_err, "
        "raw_edge, effective_edge, penalty_total, penalty_breakdown, "
        "disagreement, n_books, outlier_books, decision, rank"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    for start in range(0, len(rows), chunk_size):
        await db.executemany(sql, rows[start:start + chunk_size])
    return len(rows)
