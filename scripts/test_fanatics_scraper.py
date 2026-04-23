#!/usr/bin/env python3
"""Fanatics scraper smoke test.

Usage:
    # Hit the live endpoints for one league and print the first event
    python scripts/test_fanatics_scraper.py --sport basketball_nba

    # Parse a local JSON file that you captured from the browser DevTools
    python scripts/test_fanatics_scraper.py --file dumps/fanatics_nba.json --sport basketball_nba

    # Parse a single URL (useful when probing a new endpoint path)
    python scripts/test_fanatics_scraper.py --url https://api.sportsbook.fanatics.com/api/v1/sportsbook/events?league=nba

When Marco runs this with a live session cookie in his environment:

    CALLISTO_FANATICS_SESSION_COOKIE=... python scripts/test_fanatics_scraper.py --sport basketball_nba

the scraper will attach the cookie and the output may include
account-specific fields (different games surface for in-region vs
out-of-region sessions).

The point of this script is to verify new markets WITHOUT running the
full line_monitor integration. If output shows three markets (h2h,
spreads, totals) for a non-zero number of games, the scraper works.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

# Make "tools.*" importable whether this runs from repo root or scripts/
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import fanatics_scraper as fs  # noqa: E402
from tools.credentials import (  # noqa: E402
    FIELD_SESSION_COOKIE,
    env_var_name,
    has_credential,
)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--sport",
        default="basketball_nba",
        help=f"Callisto sport key (default: basketball_nba). "
             f"Supported: {', '.join(fs.FANATICS_LEAGUE_KEYS.keys())}",
    )
    ap.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Optional path to a JSON payload captured from a browser. "
             "If given, the live HTTP call is skipped and the payload is parsed.",
    )
    ap.add_argument(
        "--url",
        default=None,
        help="Optional override URL to fetch (useful when the endpoint "
             "path moves). Bypasses the _ENDPOINT_CANDIDATES list.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Number of events to print (default: 1).",
    )
    return ap.parse_args()


def _print_game(game: dict, idx: int) -> None:
    print(f"\n=== Event {idx + 1} ===")
    print(f"  matchup       : {game.get('away_team')} @ {game.get('home_team')}")
    print(f"  commence_time : {game.get('commence_time')}")
    print(f"  id            : {game.get('id')}")
    bms = game.get("bookmakers", [])
    for bm in bms:
        print(f"  book          : {bm.get('key')} ({bm.get('title')})")
        for mkt in bm.get("markets", []):
            print(f"    market      : {mkt.get('key')}")
            for o in mkt.get("outcomes", []):
                line = f" @ {o.get('point')}" if "point" in o else ""
                print(f"      {o.get('name'):<25}{line:<12} {o.get('price'):+d}")


async def _from_live(sport: str, url_override: Optional[str]) -> dict:
    if url_override:
        # Monkey-patch the candidate list for this run so we hit exactly
        # the URL the user wants to test.
        fs._ENDPOINT_CANDIDATES = (url_override,)  # type: ignore[attr-defined]
        # Disable the league placeholder interpolation
        league = fs.FANATICS_LEAGUE_KEYS.get(sport, "nba")
        if "{league}" not in url_override:
            fs._ENDPOINT_CANDIDATES = (url_override.rstrip("&?") + ("" if "{league}" in url_override else ""),)
    return await fs.fetch_fanatics_odds(sport)


def _from_file(path: Path, sport: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    events = fs._extract_events(data)
    games = []
    for ev in events:
        parsed = fs._normalize_event(ev, sport)
        if parsed is not None:
            games.append(parsed)
    return {
        "sport": sport,
        "game_count": len(games),
        "games": games,
        "source": "fanatics_scraper(file)",
    }


def main() -> int:
    args = _parse_args()

    if args.sport not in fs.FANATICS_LEAGUE_KEYS:
        print(f"ERROR: sport {args.sport!r} not supported. "
              f"Choose one of: {', '.join(fs.FANATICS_LEAGUE_KEYS.keys())}",
              file=sys.stderr)
        return 2

    # Tell the operator whether we're authenticated.
    cookie_var = env_var_name("fanatics", FIELD_SESSION_COOKIE)
    if has_credential("fanatics", FIELD_SESSION_COOKIE):
        print(f"[auth] using session cookie from {cookie_var}")
    else:
        print(f"[auth] running unauthenticated — set {cookie_var} to upgrade")

    if args.file:
        print(f"[mode] parsing local file: {args.file}")
        result = _from_file(args.file, args.sport)
    else:
        print(f"[mode] live HTTP fetch for {args.sport} "
              f"(league={fs.FANATICS_LEAGUE_KEYS[args.sport]})")
        result = asyncio.run(_from_live(args.sport, args.url))

    if result.get("error"):
        print(f"\nFETCH FAILED: {result['error']}")
        print(f"  status     : {result.get('status', '?')}")
        return 1

    game_count = result.get("game_count", 0)
    print(f"\n[result] {game_count} events parsed for {args.sport}")
    if game_count == 0:
        print("  (endpoint returned no events — may be no games today, "
              "or the endpoint shape changed.)")
        return 0

    for i, game in enumerate(result["games"][: args.limit]):
        _print_game(game, i)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
