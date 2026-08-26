"""Odds dump route handler bodies (moved from api.py).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

Handlers access api.py's module-level singletons (``line_monitor``,
``autonomous``, ``learned_correlation_store``, ``DB_PATH``) via a late
``from api import ...`` inside the function body to avoid a circular
import at module load time.
"""

from __future__ import annotations

import os
from typing import Optional

import aiosqlite
from fastapi import HTTPException


async def get_movements(sport: Optional[str] = None, limit: int = 20):
    """Get recent line movements detected by the monitor."""
    from api import line_monitor

    movements = await line_monitor.get_recent_movements(sport=sport, limit=limit)
    return {"count": len(movements), "movements": movements}


async def get_opportunities(status: str = "open", limit: int = 20):
    """Get current +EV betting opportunities."""
    from api import line_monitor

    opps = await line_monitor.get_ev_opportunities(status=status, limit=limit)
    return {"count": len(opps), "opportunities": opps}


async def get_snapshots(sport: str, limit: int = 10):
    """Get snapshot history for a sport."""
    from api import line_monitor

    snaps = await line_monitor.get_snapshot_history(sport=sport, limit=limit)
    return {"count": len(snaps), "snapshots": snaps}


async def force_snapshot(sport: str):
    """Force an immediate odds snapshot for a sport."""
    from api import line_monitor

    result = await line_monitor.force_snapshot(sport)
    return {
        "sport": sport,
        "game_count": result.get("game_count", 0),
        "credits": result.get("credits", {}),
    }


def get_edges(sport: Optional[str] = None):
    """Get latest cross-book edges, sharp money signals, and low-vig opportunities."""
    from api import line_monitor

    report = line_monitor.get_edge_report(sport=sport)
    return report


async def get_live_edges(
    sport: Optional[str] = None,
    decision: Optional[str] = None,
    limit: int = 50,
):
    """Ranked live edge surface from the quant microstructure engine.

    Returns the most recent snapshot from ``live_edge_surface`` (refreshed
    every ~60s by the quant scanner). Filters:
      - ``sport``: restrict to one sport key (e.g., ``baseball_mlb``).
      - ``decision``: 'recommended' | 'hold' | 'skip'. Default: all.
      - ``limit``: max rows returned (default 50).

    Each row is the ranker's full output for that (event, market, outcome,
    placement_book) — consensus fair, placement fair, raw edge, effective
    edge after penalties, and per-penalty breakdown for transparency.
    """
    import json as _json

    from api import DB_PATH

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout = 30000")
        # Most recent snapshot across the whole table.
        cur = await db.execute(
            "SELECT MAX(computed_at) FROM live_edge_surface"
        )
        row = await cur.fetchone()
        latest = row[0] if row and row[0] else None
        if not latest:
            return {"computed_at": None, "count": 0, "edges": []}

        where_parts = ["computed_at = ?"]
        params: list = [latest]
        if sport:
            where_parts.append("sport = ?")
            params.append(sport)
        if decision:
            where_parts.append("decision = ?")
            params.append(decision)
        where_clause = " AND ".join(where_parts)
        params.append(limit)

        cur = await db.execute(
            f"SELECT sport, event_id, market, outcome, placement_book, "
            f"placement_implied, placement_fair, consensus_fair, "
            f"consensus_std_err, raw_edge, effective_edge, penalty_total, "
            f"penalty_breakdown, disagreement, n_books, outlier_books, "
            f"decision, rank "
            f"FROM live_edge_surface WHERE {where_clause} "
            f"ORDER BY decision='recommended' DESC, rank ASC, "
            f"effective_edge DESC LIMIT ?",
            params,
        )
        rows = await cur.fetchall()

    edges = []
    for r in rows:
        try:
            penalties = _json.loads(r[12] or "{}")
        except Exception:
            penalties = {}
        try:
            outliers = _json.loads(r[15] or "[]")
        except Exception:
            outliers = []
        edges.append({
            "sport": r[0],
            "event_id": r[1],
            "market": r[2],
            "outcome": r[3],
            "placement_book": r[4],
            "placement_implied": r[5],
            "placement_fair": r[6],
            "consensus_fair": r[7],
            "consensus_std_err": r[8],
            "raw_edge": r[9],
            "effective_edge": r[10],
            "penalty_total": r[11],
            "penalty_breakdown": penalties,
            "disagreement": bool(r[13]),
            "n_books": r[14],
            "outlier_books": outliers,
            "decision": r[16],
            "rank": r[17],
        })
    return {
        "computed_at": latest,
        "count": len(edges),
        "filters": {"sport": sport, "decision": decision, "limit": limit},
        "edges": edges,
    }


async def get_narrative_edges(sport: str = "basketball_nba"):
    """Detect player-level narrative edges: usage surges, role changes,
    milestone proximity, revenge games. These exploit the lag between
    a player's real situation and their prop line (set from season averages)."""
    from tools.narrative_edge import full_narrative_scan
    return await full_narrative_scan(sport)


async def get_kl_metrics(sport: Optional[str] = None, limit: int = 50):
    """Get KL divergence metrics — measures information flow between odds snapshots.

    High KL = significant price discovery (sharp info flowing in).
    Low KL = stale/unchanged lines (thin market, no information flow).
    """
    from api import line_monitor

    db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        if sport:
            cursor = await db.execute(
                "SELECT sport, event_id, market_type, kl_divergence, js_divergence, "
                "n_books, opening_entropy, closing_entropy, computed_at "
                "FROM kl_metrics WHERE sport = ? ORDER BY computed_at DESC LIMIT ?",
                (sport, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT sport, event_id, market_type, kl_divergence, js_divergence, "
                "n_books, opening_entropy, closing_entropy, computed_at "
                "FROM kl_metrics ORDER BY computed_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()

    metrics = [
        {
            "sport": r[0], "event_id": r[1], "market_type": r[2],
            "kl_divergence": r[3], "js_divergence": r[4],
            "n_books": r[5], "opening_entropy": r[6], "closing_entropy": r[7],
            "computed_at": r[8],
        }
        for r in rows
    ]
    cache_size = len(line_monitor._kl_cache)
    return {
        "count": len(metrics),
        "cached_in_memory": cache_size,
        "metrics": metrics,
    }


async def parlay_scan(sport: str):
    """Scan for correlated parlay edges on a sport. Pulls odds + alternates.

    Combines the parlay_scanner (cross-book alternate line exploitation) with
    the correlation engine (build_correlated_parlay) to find SGP edges where
    books misprice correlated legs as independent.
    """
    from tools.odds_api import get_odds as _get_odds, get_alternate_lines as _get_alt
    from tools.parlay_scanner import find_correlated_parlay_edges
    from tools.correlation import build_correlated_parlay

    # Get standard odds
    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
    if odds_data.get("error"):
        raise HTTPException(status_code=503, detail=odds_data["error"])

    all_edges = []
    correlated_suggestions = []
    # Scan first 5 games (credit budget awareness)
    for game in odds_data.get("games", [])[:5]:
        event_id = game.get("id", "")
        if not event_id:
            continue
        alt_data = await _get_alt(sport=sport, event_id=event_id)
        if alt_data.get("error"):
            continue
        edges = find_correlated_parlay_edges(game, alt_data)
        all_edges.extend(edges)

        # Also run correlation engine on standard markets
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        game_data = {"home_team": home, "away_team": away}
        available_props = []
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                for outcome in mkt.get("outcomes", []):
                    price = outcome.get("price", 0)
                    if price == 0:
                        continue
                    point = outcome.get("point")
                    desc = f"{outcome.get('name', '')} {mkt['key']}"
                    if point is not None:
                        desc += f" {point}"
                    available_props.append({
                        "market": mkt["key"],
                        "american_odds": price,
                        "description": f"{desc} ({bm['title']})",
                        "side": outcome.get("name", ""),
                    })
        if available_props:
            suggestions = build_correlated_parlay(
                available_props=available_props[:20],
                game_data=game_data,
                sport=sport,
                min_correlation=0.25,
                max_legs=3,
            )
            for s in suggestions[:5]:
                if s.get("correlation_edge_pct", 0) > 0.5:
                    correlated_suggestions.append(s)

    return {
        "sport": sport,
        "games_scanned": min(5, odds_data.get("game_count", 0)),
        "edges_found": len(all_edges),
        "edges": all_edges,
        "correlated_parlay_suggestions": correlated_suggestions,
        "credits": odds_data.get("credits", {}),
    }


async def sgp_analysis(sport: str):
    """Analyze SGP mispricing and excessive vig for a sport.

    Shows:
    1. Correlated parlay suggestions (legs that books treat as independent but aren't)
    2. Anti-correlated pairs to avoid (legs that fight each other)
    3. Strongest market correlations for this sport

    Uses cached snapshot data — zero extra API credits.
    """
    from api import autonomous, line_monitor
    from tools.correlation import (
        build_correlated_parlay,
        list_correlated_markets,
        get_all_correlations,
    )

    if not line_monitor:
        raise HTTPException(status_code=503, detail="Line monitor not initialized")

    snapshot = line_monitor._snapshots.get(sport)
    if not snapshot or not snapshot.get("games"):
        raise HTTPException(
            status_code=503,
            detail=(
                f"No snapshot data for {sport}. "
                f"Wait for next snapshot cycle or force one via POST /odds/snapshot/{sport}"
            ),
        )

    games = snapshot["games"]
    all_suggestions = []
    all_anti = []

    for game in games[:8]:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        game_data = {"home_team": home, "away_team": away}

        available_props = []
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                for outcome in mkt.get("outcomes", []):
                    price = outcome.get("price", 0)
                    if price == 0:
                        continue
                    point = outcome.get("point")
                    desc = f"{outcome.get('name', '')} {mkt['key']}"
                    if point is not None:
                        desc += f" {point}"
                    available_props.append({
                        "market": mkt["key"],
                        "american_odds": price,
                        "description": f"{desc} ({bm['title']})",
                        "side": outcome.get("name", ""),
                    })

        if not available_props:
            continue

        suggestions = build_correlated_parlay(
            available_props=available_props[:20],
            game_data=game_data,
            sport=sport,
            min_correlation=0.2,
            max_legs=3,
        )
        for s in suggestions[:5]:
            if s.get("correlation_edge_pct", 0) > 0.5:
                all_suggestions.append(s)

        # Check for anti-correlated pairs among available markets
        from tools.correlation import detect_anti_correlation
        anti = detect_anti_correlation(available_props[:15], sport)
        for a in anti:
            a["game"] = f"{away} @ {home}"
        all_anti.extend(anti)

    # Get strongest correlations for this sport
    all_corrs = get_all_correlations(sport)
    top_correlations = sorted(
        [
            {"market_a": k[0], "market_b": k[1], "correlation": v}
            for k, v in all_corrs.items()
        ],
        key=lambda x: abs(x["correlation"]),
        reverse=True,
    )[:20]

    return {
        "sport": sport,
        "games_analyzed": min(8, len(games)),
        "correlated_parlay_suggestions": sorted(
            all_suggestions,
            key=lambda x: x.get("correlation_edge_pct", 0),
            reverse=True,
        )[:15],
        "anti_correlated_pairs": all_anti[:10],
        "top_sport_correlations": top_correlations,
        "cached_parlay_scan": (
            autonomous.get_parlay_scan_report().get(sport)
            if autonomous else None
        ),
    }


async def scan_props(sport: str, event_id: str, target_book: str = "draftkings", threshold: float = 0.015):
    """
    Scan player props for +EV edges on target book.

    Full pipeline: pull props -> devig each book -> average fair values -> flag edges.
    This is the single-call prop scanner that makes Callisto autonomous.
    """
    from tools.prop_scanner import scan_props_ev
    return await scan_props_ev(sport, event_id, target_book=target_book, edge_threshold=threshold)


async def dk_props(sport: str):
    """
    Scrape DraftKings player props for all games in a sport — FREE, no API credits.

    Returns all available player props (points, rebounds, assists, threes, PRA)
    directly from DraftKings' undocumented API. Useful for:
    - Checking current DK prop lines from your phone
    - Feeding the prop scanner with target book data
    - Finding props to cross-reference against other books
    """
    from tools.dk_scraper import scrape_dk_odds, scrape_dk_props

    # First get game list
    games_data = await scrape_dk_odds(sport)
    if games_data.get("error"):
        raise HTTPException(status_code=503, detail=games_data["error"])

    results = []
    for game in games_data.get("games", []):
        event_id = game.get("id", "")
        if not event_id:
            continue
        props = await scrape_dk_props(sport, event_id)
        if props.get("player_count", 0) > 0:
            results.append({
                "game": f"{game.get('away_team', '')} @ {game.get('home_team', '')}",
                "event_id": event_id,
                "commence_time": game.get("commence_time", ""),
                **props,
            })

    return {
        "sport": sport,
        "games_with_props": len(results),
        "total_players": sum(r.get("player_count", 0) for r in results),
        "source": "draftkings_scraper",
        "credits_used": 0,
        "games": results,
    }


async def odds_status():
    """Get line monitor status and credit info."""
    from api import line_monitor

    if not line_monitor:
        raise HTTPException(status_code=503, detail="Monitor not initialized")
    return await line_monitor.get_status()


async def get_learned_correlations():
    """Get learned correlation estimates — Bayesian blend of priors + empirical data."""
    from api import learned_correlation_store

    if learned_correlation_store is None:
        raise HTTPException(status_code=503, detail="Learned correlation store not initialized")
    estimates = await learned_correlation_store.get_all_learned()
    stats = learned_correlation_store.get_stats()
    return {"stats": stats, "estimates": estimates}


async def market_analysis(sport: str):
    """Full market structure analysis — key numbers, stale lines, Pinnacle benchmark."""
    from tools.odds_api import get_odds as _get_odds
    from tools.market_analysis import full_market_analysis

    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
    if odds_data.get("error"):
        raise HTTPException(status_code=503, detail=odds_data["error"])

    analysis = full_market_analysis(odds_data.get("games", []), sport)
    analysis["credits"] = odds_data.get("credits", {})
    return analysis


async def stale_lines(sport: str):
    """Find retail book lines that are stale vs sharp benchmark."""
    from tools.odds_api import get_odds as _get_odds
    from tools.market_analysis import find_stale_lines

    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
    if odds_data.get("error"):
        raise HTTPException(status_code=503, detail=odds_data["error"])

    stale = find_stale_lines(odds_data.get("games", []))
    return {"count": len(stale), "stale_lines": stale, "credits": odds_data.get("credits", {})}


async def line_gaps(sport: str, event_id: str = "", market: str = "alternate_spreads"):
    """Scan alternate lines for gaps — missing points that reveal risk concentration."""
    from tools.odds_api import get_odds as _get_odds, get_alternate_lines as _get_alt
    from tools.line_gaps import scan_line_gaps

    if event_id:
        alt_data = await _get_alt(sport=sport, event_id=event_id)
        if alt_data.get("error"):
            raise HTTPException(status_code=503, detail=alt_data["error"])
        gaps = scan_line_gaps(alt_data.get("bookmakers", []), market_key=market)
        return {"event_id": event_id, "market": market, "gap_count": len(gaps), "gaps": gaps}

    # No event_id — scan first 5 games
    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h")
    if odds_data.get("error"):
        raise HTTPException(status_code=503, detail=odds_data["error"])

    all_gaps = []
    for game in odds_data.get("games", [])[:5]:
        eid = game.get("id", "")
        if not eid:
            continue
        alt_data = await _get_alt(sport=sport, event_id=eid)
        if alt_data.get("error"):
            continue
        gaps = scan_line_gaps(alt_data.get("bookmakers", []), market_key=market)
        for g in gaps:
            g["game"] = f"{game.get('away_team', '')} @ {game.get('home_team', '')}"
            g["event_id"] = eid
        all_gaps.extend(gaps)

    return {
        "sport": sport,
        "market": market,
        "games_scanned": min(5, odds_data.get("game_count", 0)),
        "gap_count": len(all_gaps),
        "exploitable": len([g for g in all_gaps if g.get("exploitable")]),
        "gaps": all_gaps,
        "credits": odds_data.get("credits", {}),
    }


async def prop_gaps(sport: str, event_id: str = ""):
    """Scan player props for line gaps across bookmakers."""
    from tools.odds_api import get_odds as _get_odds, get_player_props as _get_props
    from tools.line_gaps import scan_prop_gaps

    if event_id:
        prop_data = await _get_props(sport=sport, event_id=event_id)
        if prop_data.get("error"):
            raise HTTPException(status_code=503, detail=prop_data["error"])
        gaps = scan_prop_gaps(prop_data)
        return {"event_id": event_id, "gap_count": len(gaps), "gaps": gaps}

    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h")
    if odds_data.get("error"):
        raise HTTPException(status_code=503, detail=odds_data["error"])

    all_gaps = []
    for game in odds_data.get("games", [])[:3]:
        eid = game.get("id", "")
        if not eid:
            continue
        prop_data = await _get_props(sport=sport, event_id=eid)
        if prop_data.get("error"):
            continue
        gaps = scan_prop_gaps(prop_data)
        for g in gaps:
            g["game"] = f"{game.get('away_team', '')} @ {game.get('home_team', '')}"
        all_gaps.extend(gaps)

    return {
        "sport": sport,
        "games_scanned": min(3, odds_data.get("game_count", 0)),
        "gap_count": len(all_gaps),
        "gaps": all_gaps,
        "credits": odds_data.get("credits", {}),
    }
