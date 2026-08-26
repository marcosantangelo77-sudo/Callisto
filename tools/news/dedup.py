"""Cross-source deduplication of injury events."""
from __future__ import annotations

from typing import Optional

from tools.player_name_index import (
    _normalise as _normalize_name,
    fuzzy_match_score,
    DEFAULT_CONFIDENCE_THRESHOLD,
)
from tools.news.models import InjuryEvent


def dedup_key(sport: str, player_name: str, body_part: Optional[str]) -> tuple:
    """Canonicalise the three-tuple used to match the same underlying event
    across sources. Name is lowercased + punct-stripped via the shared
    player-name normaliser; body_part falls back to ``'unknown'`` so a row
    with no body_part still dedupes against itself."""
    return (
        (sport or "").strip(),
        _normalize_name(player_name or ""),
        (body_part or "unknown").lower(),
    )


def dedupe_injuries(events: list[InjuryEvent]) -> list[dict]:
    """Collapse same-injury events across sources into single rows.

    Sets ``confirmed_at`` to the first_seen_at of the SECOND source that
    reports the same key. Single-source rows keep ``confirmed_at=None``.
    The returned list is schema-shaped (dicts ready for INSERT).

    Matching strategy: O(n^2) across the (usually small) events list. Two
    events match iff they are for the same sport, their body_parts agree
    (both NULL counts as agree), and ``fuzzy_match_score(name_a, name_b) >=
    DEFAULT_CONFIDENCE_THRESHOLD`` (0.90). That upgrades
    ``"Jayson Tatum" <-> "J. Tatum"`` to a dedup hit while keeping
    ``"Kevin Durant" <-> "Kevin Huerter"`` distinct.
    """
    groups: list[list[InjuryEvent]] = []

    def _bp_compat(a: Optional[str], b: Optional[str]) -> bool:
        # Treat unknown body_part as compatible with anything — ESPN's
        # detail free-text often doesn't let us classify body_part even
        # though the event is the same.
        if not a or not b:
            return True
        return a == b

    for ev in events:
        placed = False
        for group in groups:
            head = group[0]
            if (head.sport == ev.sport
                    and _bp_compat(head.body_part, ev.body_part)
                    and fuzzy_match_score(head.player_name, ev.player_name)
                    >= DEFAULT_CONFIDENCE_THRESHOLD):
                group.append(ev)
                placed = True
                break
        if not placed:
            groups.append([ev])

    out: list[dict] = []
    for group in groups:
        group.sort(key=lambda e: e.first_seen_at)
        primary = group[0]
        row = primary.as_news_row()
        # Prefer the non-None, most-severe classification across sources.
        severities = [g.severity for g in group if g.severity]
        status_vals = [g.status for g in group if g.status]
        if severities:
            # Order: out_indefinite > severe > moderate > minor
            order = {"minor": 1, "moderate": 2, "severe": 3, "out_indefinite": 4}
            row["severity"] = max(severities, key=lambda s: order.get(s, 0))
        if status_vals:
            row["status"] = status_vals[0]  # first-source status wins; later sources agree ~always
        # Cross-source confirmation: need distinct source strings.
        distinct_sources = {g.source for g in group}
        if len(distinct_sources) >= 2:
            # confirmed_at = first_seen_at of the 2nd source to report.
            row["confirmed_at"] = group[1].first_seen_at
            row["source"] = "+".join(sorted(distinct_sources))
        out.append(row)
    return out
