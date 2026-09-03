"""LIVE review and narrative-edge ResearchLoop phases, extracted from post_live.

Callers still import these names from tools.loop.phases.post_live / phases_impl.
This module must never import tools.autonomous (circular).
phase_live_execute stays defined in the phases_impl facade (not relocated).

phase_review_live demotes underperforming LIVE hypotheses to paused.
It does not arm live betting and does not add live to paper-signal.
"""
from __future__ import annotations

import os

from tools.loop import phases_impl as _impl

logger = _impl.logger
_wiki_in_loop_enabled = _impl._wiki_in_loop_enabled
_fetch_wiki_priors = _impl._fetch_wiki_priors


async def phase_review_live(loop) -> None:
    self = loop
    """Review LIVE hypotheses for underperformance; demote to 'paused'.

    Audit 2026-04-21: Callisto previously had NO demotion path from LIVE.
    Losing hypotheses stayed live until manually retired. This phase runs
    the rolling-window review and calls `review_live_hypotheses()` to
    quantify performance and demote underperformers.

    Cycle-gated: runs once every 4 hours worth of cycles (~every 24 cycles
    at a 10-minute cadence) to avoid thrashing on noisy short windows.
    Can be overridden by setting `_force_live_review` on the instance.
    """
    # Cycle gate: approx every 4 hours. A typical cycle is ~10 minutes, so
    # every 24 cycles ≈ 4 hours. Use modulo so the schedule drifts with
    # actual cycle cadence.
    review_interval = int(os.getenv("CALLISTO_LIVE_REVIEW_EVERY_N_CYCLES", "24"))
    if not getattr(self, "_force_live_review", False):
        if self._cycles == 0 or (self._cycles % max(1, review_interval)) != 0:
            return

    try:
        results = await self.hypothesis_manager.review_live_hypotheses()
    except Exception as e:
        logger.warning(f"_phase_review_live: review failed: {e}")
        return

    if not results:
        return

    demoted = [r for r in results if r.get("demoted")]
    held = [r for r in results if not r.get("demoted")]
    logger.info(
        f"_phase_review_live: reviewed {len(results)} LIVE hypotheses — "
        f"demoted {len(demoted)}, held {len(held)}"
    )

    # ── Wiki consult: surface prior warnings & recovery-cycle precedents ──
    # (feat/wiki-in-the-loop 2026-04-22) — before logging the demotion,
    # query the wiki for matching failure modes so the decision trail
    # includes cites, not just raw stats.
    db = self.data_collector._db if self.data_collector else None
    for r in demoted:
        name = r.get("name") or r["hypothesis_id"]
        wiki_cites = []
        if db and _wiki_in_loop_enabled():
            try:
                prior = await _fetch_wiki_priors(
                    db, f"{name} failure mode underperformance demotion",
                    top_k=3,
                )
                wiki_cites = [
                    f"{a.get('topic')}(sim={a.get('similarity')})"
                    for a in prior if a.get("similarity") is not None
                ]
            except Exception as e:
                logger.debug(f"Wiki demotion prior-fetch failed: {e}")
        r["wiki_cites"] = wiki_cites
        logger.warning(
            f"LIVE DEMOTION: {name} → paused. "
            f"n={r['n_resolved']} hit={r['hit_rate']:.1%} "
            f"roi={r['roi']:.2%} mdd={r['max_drawdown']:.1%} "
            f"clv={r['avg_clv']} reasons={r['reasons']} "
            f"wiki_cites={wiki_cites}"
        )

    # For held hypotheses: consult wiki for historical demote→recover
    # cycles on similar cohorts. Informational only — not a gate.
    if db and _wiki_in_loop_enabled():
        for r in held:
            try:
                name = r.get("name") or r["hypothesis_id"]
                recov = await _fetch_wiki_priors(
                    db, f"{name} demote recover cycle rebound",
                    top_k=2,
                )
                if recov:
                    logger.debug(
                        f"_phase_review_live: held {name} — wiki recovery "
                        f"precedents: {[a.get('topic') for a in recov]}"
                    )
            except Exception:
                pass


async def phase_narrative_edges(loop) -> None:
    self = loop
    """Detect player-level narrative edges that models can't price.

    Runs every cycle to find: usage surges, role changes, milestone
    proximity, revenge games. Logs actionable findings and stores
    them for the deep_work phase to incorporate into analysis.
    """
    try:
        from tools.narrative_edge import full_narrative_scan
    except ImportError as e:
        logger.debug(f"Narrative edge module not available: {e}")
        return

    # Run for active sports with player data
    for sport in ["basketball_nba", "icehockey_nhl", "baseball_mlb"]:
        try:
            results = await full_narrative_scan(sport)

            # Log actionable edges
            actionable = [
                e for e in results.get("usage_surges", [])
                if e.get("actionable")
            ]
            if actionable:
                for edge in actionable[:5]:
                    logger.info(
                        f"NARRATIVE EDGE: {edge['player']} {edge['stat_type']} "
                        f"surge {edge['surge_ratio']:.2f}x "
                        f"(recent {edge['recent_avg']:.1f} vs season {edge['season_avg']:.1f}) "
                        f"| line={edge.get('current_line','?')} gap={edge.get('line_gap','?')} "
                        f"| {edge.get('book','?')} [{sport}]"
                    )

            role_changes = results.get("role_changes", [])
            if role_changes:
                for rc in role_changes[:3]:
                    logger.info(
                        f"ROLE CHANGE: {rc['player']} "
                        f"+{rc['minute_increase']:.0f} min/game "
                        f"({rc['season_avg_minutes']:.0f} -> {rc['recent_avg_minutes']:.0f}) [{sport}]"
                    )

            milestones = results.get("milestones", [])
            if milestones:
                for m in milestones[:3]:
                    logger.info(
                        f"MILESTONE: {m['player']} "
                        f"{m['edge_type']} — {m.get('note','')} [{sport}]"
                    )

            revenge = results.get("revenge_games", [])
            if revenge:
                for r in revenge[:3]:
                    logger.info(
                        f"REVENGE GAME: {r['player']} "
                        f"(now {r['current_team']}, ex-{', '.join(r['former_teams'])}) [{sport}]"
                    )

        except Exception as e:
            logger.debug(f"Narrative scan failed for {sport}: {e}")

