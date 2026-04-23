"""
Line gap analysis — exploit discontinuities in bookmaker offerings.

When DraftKings offers a player 15+ points and 17+ points but SKIPS 16+,
that gap is information. Why did they skip it?

Possible reasons:
1. Their model shows high liability concentration at 16 — too much sharp
   action expected on one side, so they pull the line entirely.
2. The true probability at 16 is in a "dead zone" where they can't price
   both sides profitably with standard vig.
3. Lazy/automated line generation that uses non-uniform intervals.

The edge:
- If Book A skips 16+ but Book B offers it, Book B may be mispricing it
  (they didn't identify the risk Book A saw) OR Book A is being overly
  cautious and Book B's line is fair.
- We can INFER Book A's implied probability at 16 by interpolating their
  15+ and 17+ lines. If Book B's actual price is softer than the
  interpolated value, that's exploitable.
- Gaps near round numbers or key statistical thresholds are most interesting
  because that's where model uncertainty concentrates.

This module scans alternate lines and player props for gaps, cross-references
across books, and identifies exploitable discontinuities.
"""

import logging
from typing import Optional

from tools.odds_api import calculate_implied_probability, calculate_ev

logger = logging.getLogger("callisto.line_gaps")


def scan_line_gaps(
    bookmakers: list[dict],
    market_key: str = "alternate_spreads",
    team_filter: str = "",
) -> list[dict]:
    """
    Scan a single market across bookmakers for gaps in line offerings.

    A "gap" is when a bookmaker offers lines at point X and point X+2
    but NOT at X+1 (or any non-uniform interval).

    Returns gaps with interpolated fair value and cross-book comparison.
    """
    # Collect all lines by bookmaker
    book_lines = {}  # {book_title: [(point, price, name), ...]}

    for bm in bookmakers:
        title = bm.get("title", "")
        for mkt in bm.get("markets", []):
            if mkt["key"] != market_key:
                continue
            lines = []
            for o in mkt.get("outcomes", []):
                name = o.get("name", "")
                if team_filter and team_filter.lower() not in name.lower():
                    continue
                point = o.get("point")
                price = o.get("price", 0)
                if point is not None:
                    lines.append({
                        "point": point,
                        "price": price,
                        "name": name,
                        "implied": calculate_implied_probability(price),
                    })
            if lines:
                lines.sort(key=lambda x: x["point"])
                book_lines[title] = lines

    if not book_lines:
        return []

    gaps = []

    for book, lines in book_lines.items():
        # Detect gaps: look for non-uniform intervals
        for i in range(len(lines) - 1):
            current = lines[i]
            next_line = lines[i + 1]

            # Expected interval (usually 0.5 or 1.0 for spreads/totals)
            interval = next_line["point"] - current["point"]

            # A gap exists if interval > 1.0 for spreads or > 1 for props
            # (standard granularity is 0.5 for spreads, 1.0 for props)
            if market_key in ("alternate_spreads", "alternate_totals"):
                is_gap = interval > 1.5
            else:
                # Player props typically go in 0.5 or 1.0 increments
                is_gap = interval > 1.5

            if not is_gap:
                continue

            # Identify the missing points
            missing_points = []
            step = 0.5 if market_key in ("alternate_spreads", "alternate_totals") else 1.0
            p = current["point"] + step
            while p < next_line["point"]:
                missing_points.append(p)
                p += step

            if not missing_points:
                continue

            # Interpolate implied probability for the missing points
            # Linear interpolation between the two bracketing lines
            for missing_pt in missing_points:
                fraction = (missing_pt - current["point"]) / (next_line["point"] - current["point"])
                interpolated_implied = current["implied"] + fraction * (next_line["implied"] - current["implied"])
                interpolated_price = _implied_to_american(interpolated_implied)

                # Check if any OTHER book offers this exact point
                other_book_offerings = []
                for other_book, other_lines in book_lines.items():
                    if other_book == book:
                        continue
                    for ol in other_lines:
                        if abs(ol["point"] - missing_pt) < 0.01:
                            other_book_offerings.append({
                                "bookmaker": other_book,
                                "price": ol["price"],
                                "implied": ol["implied"],
                                "vs_interpolated": round(ol["implied"] - interpolated_implied, 4),
                            })

                gap_entry = {
                    "bookmaker_with_gap": book,
                    "team": current["name"],
                    "market": market_key,
                    "gap_point": missing_pt,
                    "bracket_low": {
                        "point": current["point"],
                        "price": current["price"],
                        "implied": round(current["implied"], 4),
                    },
                    "bracket_high": {
                        "point": next_line["point"],
                        "price": next_line["price"],
                        "implied": round(next_line["implied"], 4),
                    },
                    "interpolated_implied": round(interpolated_implied, 4),
                    "interpolated_price": interpolated_price,
                    "other_books_with_line": other_book_offerings,
                    "exploitable": False,
                    "edge_detail": None,
                }

                # Is there an exploitable edge?
                for offering in other_book_offerings:
                    # If other book's actual implied is LOWER than our interpolated
                    # estimate, the other book is underpricing → potential value
                    edge = interpolated_implied - offering["implied"]
                    if edge > 0.02:  # 2% minimum
                        ev = calculate_ev(
                            probability=interpolated_implied,
                            american_odds=offering["price"],
                        )
                        if ev["is_positive_ev"]:
                            gap_entry["exploitable"] = True
                            gap_entry["edge_detail"] = {
                                "play_at": offering["bookmaker"],
                                "play_price": offering["price"],
                                "edge_vs_interpolated": round(edge, 4),
                                "edge_pct": round(edge * 100, 2),
                                "ev": ev,
                                "reasoning": (
                                    f"{book} skips {missing_pt} (gap between {current['point']} and "
                                    f"{next_line['point']}). Interpolated fair value: "
                                    f"{interpolated_implied:.1%}. {offering['bookmaker']} offers it at "
                                    f"{offering['price']} (implied {offering['implied']:.1%}). "
                                    f"Edge: {edge:.1%}. {book}'s gap suggests they see risk "
                                    f"concentration here — {offering['bookmaker']} may be underpricing."
                                ),
                            }

                gaps.append(gap_entry)

    # Sort: exploitable first, then by gap size
    gaps.sort(key=lambda x: (not x["exploitable"], -abs(x.get("edge_detail", {}).get("edge_pct", 0) if x.get("edge_detail") else 0)))
    return gaps


def scan_prop_gaps(
    player_props: dict,
) -> list[dict]:
    """
    Scan player props for line gaps across bookmakers.

    Player props are where gaps are fattest because:
    1. Each book uses different models for props
    2. Some books don't offer certain thresholds (the gaps)
    3. The gaps tell you where their model has uncertainty
    4. If another book DOES offer that threshold, they may be mispricing

    Example: DraftKings offers Player X 15+ points and 17+ points but not 16+.
    Fanatics offers 16+ at -110. Is Fanatics mispricing, or is DK being cautious?
    """
    gaps = []
    players = player_props.get("players", {})

    for player_name, lines in players.items():
        # Group by bookmaker and market
        by_book_market = {}
        for line in lines:
            key = (line["bookmaker"], line["market"])
            by_book_market.setdefault(key, []).append(line)

        # For each bookmaker, find gaps in their offerings
        for (book, market), book_lines in by_book_market.items():
            # Separate overs and unders
            overs = sorted(
                [l for l in book_lines if l["name"] == "Over"],
                key=lambda x: x.get("point", 0),
            )

            if len(overs) < 2:
                continue

            for i in range(len(overs) - 1):
                current = overs[i]
                next_over = overs[i + 1]

                current_pt = current.get("point", 0)
                next_pt = next_over.get("point", 0)
                interval = next_pt - current_pt

                # Gap: interval > 1.5 for props (standard is 0.5 or 1.0)
                if interval <= 1.5:
                    continue

                # Find missing points
                step = 1.0  # Props usually go in 1.0 increments
                if interval <= 2.0:
                    step = 0.5

                missing_pt = current_pt + step
                while missing_pt < next_pt:
                    # Interpolate
                    current_implied = calculate_implied_probability(current.get("price", -110))
                    next_implied = calculate_implied_probability(next_over.get("price", -110))
                    fraction = (missing_pt - current_pt) / (next_pt - current_pt)
                    interp_implied = current_implied + fraction * (next_implied - current_implied)

                    # Check other books for this point
                    other_offerings = []
                    for (other_book, other_market), other_lines in by_book_market.items():
                        if other_book == book or other_market != market:
                            continue
                        # Also check other bookmakers in the full player data
                    # Need to check ALL bookmakers for this player/market
                    for other_line in lines:
                        if other_line["bookmaker"] == book:
                            continue
                        if other_line["market"] != market:
                            continue
                        if other_line["name"] != "Over":
                            continue
                        if abs(other_line.get("point", 0) - missing_pt) < 0.01:
                            other_implied = calculate_implied_probability(other_line["price"])
                            other_offerings.append({
                                "bookmaker": other_line["bookmaker"],
                                "price": other_line["price"],
                                "point": other_line.get("point"),
                                "implied": round(other_implied, 4),
                            })

                    gap_entry = {
                        "player": player_name,
                        "market": market,
                        "bookmaker_with_gap": book,
                        "gap_point": missing_pt,
                        "bracket_low": {"point": current_pt, "price": current.get("price")},
                        "bracket_high": {"point": next_pt, "price": next_over.get("price")},
                        "interpolated_implied": round(interp_implied, 4),
                        "other_books_with_line": other_offerings,
                    }

                    # Check for exploitable edges
                    for offering in other_offerings:
                        edge = interp_implied - offering["implied"]
                        if edge > 0.02:
                            ev = calculate_ev(
                                probability=interp_implied,
                                american_odds=offering["price"],
                            )
                            gap_entry["exploitable"] = True
                            gap_entry["edge"] = {
                                "play_at": offering["bookmaker"],
                                "price": offering["price"],
                                "edge_pct": round(edge * 100, 2),
                                "ev": ev,
                            }

                    gaps.append(gap_entry)
                    missing_pt += step

    return gaps


def _implied_to_american(implied: float) -> int:
    """Convert implied probability back to American odds."""
    if implied <= 0 or implied >= 1:
        return 0
    if implied >= 0.5:
        return int(-100 * implied / (1 - implied))
    else:
        return int(100 * (1 - implied) / implied)
