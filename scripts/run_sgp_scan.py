"""
CLI: run the SGP scanner against one game and dump edges to stdout.

Primary data source is ``odds_snapshots`` in the local DB — we do NOT make
upstream calls here (the whole point is to be credit-friendly). If the game
isn't in the DB, the script exits with a diagnostic and leaves credit count
untouched.

Usage:
    python scripts/run_sgp_scan.py <sport> <event_id> [--book draftkings]
    python scripts/run_sgp_scan.py --demo     # run a self-contained demo
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_sgp_scan")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _load_game_from_snapshots(
    db: sqlite3.Connection,
    sport: str,
    event_id: str,
    book: str,
) -> Optional[dict]:
    """Reconstruct a minimal odds-api-shape game dict from odds_snapshots.

    We don't need the full payload — just enough for ``legs_from_game_odds``
    to pull out h2h/spreads/totals/props for the target book. We take the
    most recent snapshot per (market, outcome, point).
    """
    # Probe for both legacy (odds_snapshots) and v2 (odds_snapshots_v2) shapes.
    cur = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('odds_snapshots', 'odds_snapshots_v2')"
    )
    tables = {r[0] for r in cur.fetchall()}
    if not tables:
        logger.warning("No odds_snapshots table in DB")
        return None

    # Prefer the legacy flat table — it has the shape we need without joins.
    if "odds_snapshots" in tables:
        try:
            rows = db.execute(
                """
                SELECT market, outcome_name, price_american, point, book, fetched_at
                FROM odds_snapshots
                WHERE event_id = ? AND sport = ? AND book = ?
                """,
                (event_id, sport, book),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    else:
        rows = []

    if not rows:
        return None

    # Latest per (market, outcome_name, point)
    latest: dict[tuple, dict] = {}
    for r in rows:
        key = (r["market"], r["outcome_name"], r["point"])
        existing = latest.get(key)
        if existing is None or (r["fetched_at"] or "") > (existing.get("fetched_at") or ""):
            latest[key] = dict(r)

    markets: dict[str, list[dict]] = {}
    for r in latest.values():
        markets.setdefault(r["market"], []).append(
            {
                "name": r["outcome_name"],
                "price": r["price_american"],
                "point": r["point"],
            }
        )

    return {
        "id": event_id,
        "sport_key": sport,
        "bookmakers": [
            {
                "key": book,
                "title": book,
                "markets": [
                    {"key": mk, "outcomes": outs} for mk, outs in markets.items()
                ],
            }
        ],
    }


def _demo_game() -> dict:
    """Synthesized NFL game so the CLI is usable without a populated DB."""
    return {
        "id": "demo-nfl-001",
        "sport_key": "americanfootball_nfl",
        "home_team": "Chiefs",
        "away_team": "Raiders",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Chiefs", "price": -250},
                            {"name": "Raiders", "price": +210},
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 47.5},
                            {"name": "Under", "price": -110, "point": 47.5},
                        ],
                    },
                    {
                        "key": "player_pass_yds",
                        "outcomes": [
                            {"name": "Over", "price": -115, "point": 275.5,
                             "description": "P.Mahomes", "team": "Chiefs"},
                            {"name": "Under", "price": -105, "point": 275.5,
                             "description": "P.Mahomes", "team": "Chiefs"},
                        ],
                    },
                    {
                        "key": "player_receiving_yds",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 82.5,
                             "description": "Rashee Rice", "team": "Chiefs"},
                            {"name": "Under", "price": -110, "point": 82.5,
                             "description": "Rashee Rice", "team": "Chiefs"},
                        ],
                    },
                ],
            }
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sport", nargs="?", default="")
    ap.add_argument("event_id", nargs="?", default="")
    ap.add_argument("--book", default="draftkings")
    ap.add_argument("--db", default=os.getenv("CALLISTO_DB_PATH", "memory/callisto.db"))
    ap.add_argument("--threshold", type=float, default=0.03)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--json", action="store_true", help="Emit edges as JSON")
    args = ap.parse_args()

    from tools.sgp_scanner import scan_sgp_edges, legs_from_game_odds

    if args.demo:
        game = _demo_game()
        sport = "nfl"
        event_id = game["id"]
    else:
        if not args.sport or not args.event_id:
            ap.error("provide <sport> <event_id> or use --demo")
        if not Path(args.db).is_file():
            logger.error("DB not found: %s", args.db)
            return 2
        conn = _connect(args.db)
        try:
            game = _load_game_from_snapshots(conn, args.sport, args.event_id, args.book)
        finally:
            conn.close()
        if not game:
            logger.error(
                "No snapshots for event %s / sport %s / book %s",
                args.event_id, args.sport, args.book,
            )
            return 3
        sport = args.sport
        event_id = args.event_id

    legs = legs_from_game_odds(game, book_priority=(args.book,))
    if not legs:
        logger.warning("No usable legs extracted")
        return 4

    edges = scan_sgp_edges(
        sport=sport,
        event_id=event_id,
        legs=legs,
        book=args.book,
        threshold=args.threshold,
    )

    if args.json:
        print(json.dumps([e.to_dict() for e in edges], indent=2))
    else:
        print(f"Scanned {len(legs)} legs -> {len(edges)} SGP edges")
        for e in edges[:20]:
            print("-" * 72)
            print(f"  edge: {e.edge_pct:+0.2f}%   book: {e.book_price_american:+d}"
                  f"   fair: {e.theoretical_fair_american:+d}"
                  f"   corr: {e.correlation_assumed:+0.2f}   conf: {e.confidence}")
            for leg in e.legs:
                print(f"    * {leg.leg_type:30s}  {leg.description:50s}"
                      f" @ {leg.american_odds:+d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
