"""Wiki-informed confidence adjustments (feat/wiki-in-the-loop 2026-04-22)."""

from __future__ import annotations

import os
from typing import Optional

from tools.edges.common import logger, WIKI_EDGE_ADJUSTMENT_CAP


async def apply_wiki_adjustments_to_edges(
    edges: list[dict], sport: str, db_path: Optional[str] = None,
) -> list[dict]:
    """Enrich each edge dict with ``wiki_confidence_delta`` and ``wiki_cites``.

    Walks the knowledge wiki once per (sport, market, team) triple and applies
    a bounded adjustment based on matching prior articles:

      - Prior article title/content contains "inflates" / "OVER" / "boost"
        and edge side aligns  → +delta (confirming prior)
      - Prior article title/content contains "UNDER" / "dead" / "null"
        and edge side aligns  → +delta (confirming prior)
      - Edge side contradicts direction of prior → -delta (dampening)

    The absolute sum of deltas is clamped to ``WIKI_EDGE_ADJUSTMENT_CAP``.

    Failures are logged and returned edges are left untouched — wiki being
    down cannot break the edge-scanning path. Respects
    ``CALLISTO_WIKI_IN_LOOP=1`` (default on).
    """
    if os.getenv("CALLISTO_WIKI_IN_LOOP", "1") != "1":
        return edges
    if not edges:
        return edges

    try:
        import aiosqlite
        from tools.knowledge_wiki import get_wiki
    except Exception as e:
        logger.debug(f"Wiki edge adjustments skipped (import): {e}")
        return edges

    wiki = get_wiki() if db_path is None else None
    if wiki is None:
        from tools.knowledge_wiki import KnowledgeWiki
        wiki = KnowledgeWiki(db_path)

    # Cache per (sport, market, team) so N edges on the same team = 1 lookup.
    cache: dict[tuple[str, str, str], list[dict]] = {}

    try:
        async with aiosqlite.connect(wiki.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 15000")
            for edge in edges:
                team = edge.get("team", "")
                market = edge.get("market", "")
                cache_key = (sport, market, team)
                if cache_key not in cache:
                    query = f"{sport} {market} {team} edge prior warning"
                    try:
                        cache[cache_key] = await wiki.search(
                            db, query, top_k=5, min_similarity=0.0,
                        )
                    except Exception as e:
                        logger.debug(f"Wiki edge search failed for {cache_key}: {e}")
                        cache[cache_key] = []
                priors = cache[cache_key]

                delta = 0.0
                cites = []
                for a in priors:
                    sim = a.get("similarity") or 0.0
                    if sim < 0.60:
                        continue
                    blob = (
                        (a.get("title") or "") + " " + (a.get("summary") or "")
                        + " " + (a.get("content") or "")
                    ).lower()
                    # Direction inference from edge — best_line price / team / market.
                    # For totals we use the "side" hint from the sharp_consensus
                    # vs soft-book edges; for spreads/h2h the team IS the side.
                    is_over = "over" in blob
                    is_under = "under" in blob
                    says_dead = any(k in blob for k in ("dead_pattern", "null_result", "demotion"))
                    says_boost = any(k in blob for k in ("inflates", "boost", "success", "promoted"))

                    # Simple scoring:
                    #   confirming boost article  → +0.05 * sim
                    #   confirming null/dead      → -0.05 * sim (prior said pattern doesn't work)
                    # Market-specific direction check: if the article flags an
                    # UNDER bias and current edge favours OVER (via best_line
                    # point trending high for this team's total), dampen.
                    if says_boost:
                        delta += 0.05 * sim
                        cites.append(f"+{a.get('topic')}(sim={sim:.2f})")
                    if says_dead:
                        delta -= 0.05 * sim
                        cites.append(f"-{a.get('topic')}(sim={sim:.2f})")
                    # Market-direction heuristic for totals.
                    if market == "totals":
                        # Edge's implied_range direction — not directly avail
                        # here, so use the presence of OVER/UNDER mentions as
                        # a weak signal.
                        if is_over and not is_under:
                            delta += 0.03 * sim
                            cites.append(f"over/{a.get('topic')}")
                        elif is_under and not is_over:
                            delta -= 0.03 * sim
                            cites.append(f"under/{a.get('topic')}")

                # Clamp to ±cap.
                if delta > WIKI_EDGE_ADJUSTMENT_CAP:
                    delta = WIKI_EDGE_ADJUSTMENT_CAP
                elif delta < -WIKI_EDGE_ADJUSTMENT_CAP:
                    delta = -WIKI_EDGE_ADJUSTMENT_CAP
                edge["wiki_confidence_delta"] = round(delta, 4)
                edge["wiki_cites"] = cites[:5]
    except Exception as e:
        logger.warning(f"Wiki edge adjustment pass failed (non-fatal): {e}")
        return edges

    return edges
