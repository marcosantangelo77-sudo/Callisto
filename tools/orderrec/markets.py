"""Market normalisation and order-notes parsing (split from
``tools/order_reconciler``)."""

from __future__ import annotations

import json
from typing import Any, Optional, Tuple


def _normalise_market(market: Optional[str]) -> str:
    if not market:
        return ""
    m = market.lower().strip()
    # Canonicalise to the SUPPORTED_MARKETS keys we branch on.
    if m in ("moneyline", "ml", "h2h"):
        return "h2h"
    if m in ("spread", "spreads", "run_line", "puck_line"):
        return "spreads"
    if m in ("total", "totals", "over_under", "over/under"):
        return "totals"
    if m in ("prop", "props", "player_prop", "player_props"):
        return "player_props"
    if m in ("sgp", "parlay"):
        return "sgp"
    return m


def _parse_side_for_total(side: Optional[str]) -> Optional[str]:
    """Pull 'over' or 'under' out of a side string like 'Over 8.5'."""
    if not side:
        return None
    s = side.lower().strip()
    if "over" in s:
        return "over"
    if "under" in s:
        return "under"
    return None


def _extract_line(order_row: Any, notes: Optional[str]) -> Optional[float]:
    """Find the line for spread/total/prop bets.

    Checks — in order:
      1. A ``line`` key in the order row (tests may populate this).
      2. The linked ``bets.placement_point`` row (production path).
      3. A ``line=`` token inside ``notes`` (SGP / legacy).

    Returns None if we can't recover the line.
    """
    # Dict-like access (aiosqlite.Row + dict both support __getitem__).
    try:
        line = order_row["line"]
        if line is not None:
            return float(line)
    except (KeyError, IndexError, TypeError):
        pass
    if notes and "line=" in notes:
        try:
            tok = notes.split("line=", 1)[1].split()[0].rstrip(",;")
            return float(tok)
        except (ValueError, IndexError):
            pass
    return None


def _extract_player_meta(notes: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Pull ``player=...,stat=...`` tokens from the order ``notes`` column.

    Props submitted via ``submit_order`` stash player/stat there because
    the ``orders`` table doesn't have a dedicated column (intentional —
    keeping the schema narrow).
    """
    if not notes:
        return None, None
    player, stat = None, None
    for tok in notes.replace(";", ",").split(","):
        tok = tok.strip()
        if tok.startswith("player="):
            player = tok.split("=", 1)[1].strip()
        elif tok.startswith("stat="):
            stat = tok.split("=", 1)[1].strip()
    return player, stat


def _parse_legs(notes: str) -> list[dict]:
    """Pull ``legs=[...]`` JSON out of a notes field."""
    if "legs=" not in notes:
        return []
    try:
        chunk = notes.split("legs=", 1)[1]
        # The JSON may be followed by more tokens; find the matching
        # closing bracket.
        depth = 0
        end = -1
        for i, ch in enumerate(chunk):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            return []
        return json.loads(chunk[:end])
    except Exception:
        return []
