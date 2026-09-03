"""Promotion query helpers extracted from tools.hypothesis.promote.

``HypothesisPromotionMixin`` keeps the original method names as thin
delegates so hasattr pins on the mixin continue to pass. Bodies live here
so promote.py can shrink without changing query behaviour.

Does not import ``tools.autonomous`` (no cycles). Does not arm live
betting. Does not add live to paper-signal. Does not define
``check_promotion_readiness`` (that stays on HypothesisSignificanceMixin).
auto_promote / review_live_hypotheses stay in promote.py.
"""
from __future__ import annotations

from typing import Optional

from tools.hypothesis.config import SIGNAL_COLLAPSE_MODE


async def _get_backtest_signals(self, hypothesis_id: str) -> list[dict]:
    """Get backtest signal events, deduplicated by unique event.

    Pre-2026-04-22 this kept the best-edge row per event_id.  For player-
    prop hypotheses that produce many rows per game that is *selection
    bias* — a 10-row game reports the max edge, not the representative
    edge.  We switch to one of three modes (``CALLISTO_SIGNAL_COLLAPSE_MODE``):

    * ``random_row`` — pick one row per event_id with a deterministic seed
      keyed on event_id+hypothesis_id.  Backward-compatible shape,
      eliminates best-edge selection bias.  Default.
    * ``composite``  — aggregate rows within an event into a single
      composite signal (averaged edge/ev/fair_prob, summed kelly_fraction
      capped at 1.0).  Matches real-world correlated-prop behavior.
    * ``best_edge``  — legacy pre-audit behavior; kept only for hypotheses
      marked ``model_config['legacy']=True``.  Not recommended.
    """
    import random as _random

    cursor = await self._db.execute(
        "SELECT * FROM backtest_events "
        "WHERE hypothesis_id = ? AND signal_generated = 1 "
        "ORDER BY game_date, id",
        (hypothesis_id,),
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    all_events = [dict(zip(cols, row)) for row in rows]

    # Group rows by event_id
    by_event: dict[str, list[dict]] = {}
    for ev in all_events:
        eid = ev["event_id"]
        by_event.setdefault(eid, []).append(ev)

    # Per-hypothesis mode: legacy hyps use best_edge; new hyps use the
    # configured collapse mode (default random_row).
    h = await self.get_hypothesis(hypothesis_id)
    cfg = (h or {}).get("model_config") or {}
    is_legacy = bool(cfg.get("legacy") is True) if isinstance(cfg, dict) else False
    mode = "best_edge" if is_legacy else SIGNAL_COLLAPSE_MODE

    collapsed: list[dict] = []
    for eid, group in by_event.items():
        if len(group) == 1:
            collapsed.append(group[0])
            continue

        if mode == "best_edge":
            pick = max(group, key=lambda e: (e.get("edge") or 0))
            collapsed.append(pick)
        elif mode == "composite":
            # Aggregate across rows: average edge/ev/fair/implied,
            # sum kelly (capped at 1.0 — stake fraction), keep first
            # row's metadata.  actual_result: "won" iff any row won;
            # otherwise "lost" if any row resolved.
            base = dict(group[0])
            n_g = len(group)
            def _avg(key):
                vals = [r.get(key) for r in group if r.get(key) is not None]
                return (sum(vals) / len(vals)) if vals else base.get(key)
            base["edge"] = _avg("edge")
            base["ev_pct"] = _avg("ev_pct")
            base["model_fair_prob"] = _avg("model_fair_prob")
            base["book_implied_prob"] = _avg("book_implied_prob")
            kelly_sum = sum((r.get("kelly_fraction") or 0) for r in group)
            base["kelly_fraction"] = min(1.0, kelly_sum)
            # Composite outcome: count a win only if majority of rows won.
            # This matches "correlated parlay" semantics within an event.
            wins = sum(1 for r in group if r.get("actual_result") == "won")
            losses = sum(1 for r in group if r.get("actual_result") == "lost")
            if wins == 0 and losses == 0:
                base["actual_result"] = None
            elif wins > losses:
                base["actual_result"] = "won"
            elif losses > wins:
                base["actual_result"] = "lost"
            else:
                base["actual_result"] = "push"
            base["_composite_n"] = n_g
            collapsed.append(base)
        else:  # random_row (default)
            rng = _random.Random(f"{hypothesis_id}|{eid}")
            pick = rng.choice(group)
            collapsed.append(pick)

    return sorted(collapsed, key=lambda e: e.get("game_date", ""))

async def _get_backtest_resolved(self, hypothesis_id: str) -> list[dict]:
    """Get resolved backtest events, deduplicated by unique event.

    Fallback for evaluate_significance when 0 signal events exist —
    lets us determine if the thesis has any merit before auto-rejecting.
    Uses the same collapse mode as _get_backtest_signals
    (CALLISTO_SIGNAL_COLLAPSE_MODE) to avoid re-introducing best-edge
    selection bias on the fallback path.
    """
    import random as _random

    cursor = await self._db.execute(
        "SELECT * FROM backtest_events "
        "WHERE hypothesis_id = ? AND actual_result IS NOT NULL "
        "ORDER BY game_date, id",
        (hypothesis_id,),
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    all_events = [dict(zip(cols, row)) for row in rows]

    by_event: dict[str, list[dict]] = {}
    for ev in all_events:
        by_event.setdefault(ev["event_id"], []).append(ev)

    h = await self.get_hypothesis(hypothesis_id)
    cfg = (h or {}).get("model_config") or {}
    is_legacy = bool(cfg.get("legacy") is True) if isinstance(cfg, dict) else False
    mode = "best_edge" if is_legacy else SIGNAL_COLLAPSE_MODE

    collapsed: list[dict] = []
    for eid, group in by_event.items():
        if len(group) == 1:
            collapsed.append(group[0])
            continue
        if mode == "best_edge":
            pick = max(group, key=lambda e: (e.get("edge") or 0))
        else:  # random_row / composite both select deterministically here
            rng = _random.Random(f"{hypothesis_id}|{eid}|resolved")
            pick = rng.choice(group)
        collapsed.append(pick)

    return sorted(collapsed, key=lambda e: e.get("game_date", ""))

async def _diagnose_edge_threshold(self, hypothesis_id: str) -> dict:
    """Check if a hypothesis's edge_threshold is suppressing valid signals.

    Looks at the edge distribution of backtest events to determine if the
    threshold is set above the max observed edge (meaning signals can never fire).
    """
    h = await self.get_hypothesis(hypothesis_id)
    current_threshold = h.get("edge_threshold", 0.03) if h else 0.03

    cursor = await self._db.execute(
        "SELECT edge FROM backtest_events "
        "WHERE hypothesis_id = ? AND edge IS NOT NULL "
        "ORDER BY edge DESC LIMIT 100",
        (hypothesis_id,),
    )
    edges = [r[0] for r in await cursor.fetchall()]

    if not edges:
        return {"threshold_too_high": False, "current_threshold": current_threshold}

    max_edge = max(edges)
    avg_edge = sum(edges) / len(edges)
    above_threshold = sum(1 for e in edges if e >= current_threshold)

    result = {
        "current_threshold": current_threshold,
        "max_edge": max_edge,
        "avg_edge": avg_edge,
        "total_edges": len(edges),
        "above_threshold": above_threshold,
        "threshold_too_high": above_threshold == 0 and max_edge > 0,
    }

    if result["threshold_too_high"]:
        # Set new threshold to 60% of max observed edge (leaves room for real signals)
        result["recommended_threshold"] = round(max(max_edge * 0.6, 0.01), 4)

    return result

async def _get_best_run_stats(self, hypothesis_id: str) -> Optional[dict]:
    """Get the best backtest run stats for a hypothesis (by hit_rate)."""
    cursor = await self._db.execute(
        "SELECT actual_win, actual_loss, hit_rate, avg_edge, avg_ev "
        "FROM backtest_runs "
        "WHERE hypothesis_id = ? AND hit_rate IS NOT NULL "
        "ORDER BY hit_rate DESC LIMIT 1",
        (hypothesis_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "wins": row[0],
        "losses": row[1],
        "hit_rate": row[2],
        "avg_edge": row[3],
        "avg_ev": row[4],
    }

async def _days_of_odds_data(self, hypothesis_id: str) -> Optional[int]:
    """How many days of historical odds data exist for this hypothesis's sport."""
    cursor = await self._db.execute(
        "SELECT sport FROM hypotheses WHERE hypothesis_id = ?",
        (hypothesis_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    sport = row[0]
    cursor = await self._db.execute(
        "SELECT COUNT(DISTINCT snapshot_date) FROM historical_odds_cache "
        "WHERE sport = ?",
        (sport,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0

async def _avg_books_used(self, hypothesis_id: str) -> Optional[float]:
    """Average books_used across backtest events for this hypothesis.

    Returns None if no events have model_factors with books_used.
    A value < 2.0 means the devig was based on a single book — unreliable.
    """
    cursor = await self._db.execute(
        "SELECT model_factors FROM backtest_events "
        "WHERE hypothesis_id = ? AND model_factors IS NOT NULL "
        "LIMIT 50",
        (hypothesis_id,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return None
    import json as _json
    books = []
    for (mf,) in rows:
        try:
            factors = _json.loads(mf)
            b = factors.get("books_used")
            if b is not None:
                books.append(b)
        except (ValueError, TypeError):
            continue
    return sum(books) / len(books) if books else None

async def _count_unresolved(self, hypothesis_id: str) -> int:
    """Count backtest events that haven't been resolved against game results."""
    cursor = await self._db.execute(
        "SELECT COUNT(*) FROM backtest_events "
        "WHERE hypothesis_id = ? AND actual_result IS NULL",
        (hypothesis_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0

async def _get_paper_trades(self, hypothesis_id: str) -> list[dict]:
    """Get paper trades for a hypothesis, deduplicated to best-edge per unique game.

    Each game can produce multiple paper trades (one per book showing edge).
    For evaluation, we keep only the highest-edge trade per unique game
    (game_date + home_team + away_team) to avoid inflating sample counts.
    """
    cursor = await self._db.execute(
        """
        SELECT * FROM paper_trades
        WHERE rowid IN (
            SELECT rowid FROM (
                SELECT rowid,
                       ROW_NUMBER() OVER (
                           PARTITION BY hypothesis_id, game_date, home_team, away_team
                           ORDER BY edge DESC
                       ) as rn
                FROM paper_trades
                WHERE hypothesis_id = ?
            )
            WHERE rn = 1
        )
        ORDER BY game_date
        """,
        (hypothesis_id,),
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    result = [dict(zip(cols, row)) for row in rows]

    # Map paper_trades column names to backtest_events names so that
    # evaluate_significance() (which expects book_odds_american etc.)
    # works transparently with paper trade data.
    for row in result:
        row["book_odds_american"] = row.get("signal_odds_american")
        row["book_implied_prob"] = row.get("signal_implied_prob")
        row["signal_generated"] = 1  # all paper trades are signals

    return result

async def _get_paper_trades_all(self, hypothesis_id: str) -> list[dict]:
    """Get ALL paper trades including multi-book duplicates (for detailed reporting)."""
    cursor = await self._db.execute(
        "SELECT * FROM paper_trades WHERE hypothesis_id = ? ORDER BY game_date",
        (hypothesis_id,),
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in rows]
