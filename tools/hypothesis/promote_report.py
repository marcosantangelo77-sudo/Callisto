"""Hypothesis overlap + report helpers extracted from promote mixin.

``HypothesisPromotionMixin._compute_portfolio_overlap``,
``get_hypothesis_report``, and ``get_temporal_metadata`` stay defined on
the mixin as thin delegates so hasattr pins keep passing. Bodies live
here so ``tools/hypothesis/promote.py`` can keep shrinking.

``check_promotion_readiness`` stays on ``HypothesisSignificanceMixin`` —
do not copy it onto ``HypothesisPromotionMixin``.

Portfolio overlap *reads* existing LIVE hypothesis rows to measure
event-id collision. It does not arm live betting and does not add live
to paper-signal.

Do not import tools.autonomous.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from tools.hypothesis.config import PORTFOLIO_OVERLAP_WINDOW_DAYS


async def compute_portfolio_overlap(
    self,
    hypothesis_id: str,
    window_days: int | None = None,
) -> dict[str, float]:
    """Compute % of candidate's signals that fall on events where an
    existing LIVE hypothesis also fired.

    Returns: {live_hypothesis_id: overlap_pct, …}
    where overlap_pct = |candidate_events ∩ live_events| / |candidate_events|.
    """
    window_days = window_days or PORTFOLIO_OVERLAP_WINDOW_DAYS
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).strftime("%Y-%m-%d")

    # Candidate's distinct signal event_ids in window
    cand_cur = await self._db.execute(
        "SELECT DISTINCT event_id FROM backtest_events "
        "WHERE hypothesis_id = ? AND signal_generated = 1 "
        "AND game_date >= ?",
        (hypothesis_id, cutoff),
    )
    cand_events = {r[0] for r in await cand_cur.fetchall()}
    if not cand_events:
        return {}

    # Live hyps (excluding candidate)
    live_cur = await self._db.execute(
        "SELECT hypothesis_id FROM hypotheses "
        "WHERE status = 'live' AND hypothesis_id != ?",
        (hypothesis_id,),
    )
    live_ids = [r[0] for r in await live_cur.fetchall()]

    overlap_map: dict[str, float] = {}
    for live_id in live_ids:
        live_ev_cur = await self._db.execute(
            "SELECT DISTINCT event_id FROM backtest_events "
            "WHERE hypothesis_id = ? AND signal_generated = 1 "
            "AND game_date >= ?",
            (live_id, cutoff),
        )
        live_events = {r[0] for r in await live_ev_cur.fetchall()}
        if not live_events:
            continue
        shared = cand_events & live_events
        if shared:
            overlap_map[live_id] = len(shared) / len(cand_events)

    return overlap_map


async def get_hypothesis_report(self, hypothesis_id: str) -> dict:
    """Full report across all stages."""
    h = await self.get_hypothesis(hypothesis_id)
    if not h:
        return {"error": "Hypothesis not found"}

    report = {"hypothesis": h, "stages": {}}

    # Backtest stats
    bt_cursor = await self._db.execute(
        "SELECT * FROM backtest_runs WHERE hypothesis_id = ? ORDER BY completed_at DESC LIMIT 5",
        (hypothesis_id,),
    )
    bt_rows = await bt_cursor.fetchall()
    if bt_rows:
        bt_cols = [d[0] for d in bt_cursor.description]
        report["stages"]["backtest"] = {
            "runs": [dict(zip(bt_cols, r)) for r in bt_rows],
        }

    # Latest significance per stage
    for stage in ["backtest", "paper_trade"]:
        stats_cursor = await self._db.execute(
            "SELECT * FROM hypothesis_stats "
            "WHERE hypothesis_id = ? AND stage = ? ORDER BY computed_at DESC LIMIT 1",
            (hypothesis_id, stage),
        )
        stats_row = await stats_cursor.fetchone()
        if stats_row:
            stats_cols = [d[0] for d in stats_cursor.description]
            report["stages"][f"{stage}_latest_stats"] = dict(zip(stats_cols, stats_row))

    # Readiness check
    report["promotion_readiness"] = await self.check_promotion_readiness(hypothesis_id)

    # Temporal metadata
    temporal = self.get_temporal_metadata(h)
    if temporal:
        report["temporal_metadata"] = temporal

    return report


def get_temporal_metadata(hypothesis: dict) -> Optional[dict]:
    """Extract temporal split metadata from a hypothesis's model_config.

    Returns None if no temporal metadata exists (legacy hypothesis).
    """
    config = hypothesis.get("model_config", {})
    training_end = config.get("training_period_end")
    if not training_end:
        return None
    return {
        "training_period_start": config.get("training_period_start"),
        "training_period_end": training_end,
        "temporal_split_gap_days": config.get("temporal_split_gap_days", 7),
        "training_sample_size": config.get("training_sample_size"),
        "training_hit_rate": config.get("training_hit_rate"),
        "training_p_value": config.get("training_p_value"),
        "has_temporal_isolation": True,
    }
