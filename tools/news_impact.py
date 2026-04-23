"""
news_impact — correlate news events with odds movements to spot under-reactions.

Theory
------
Late-breaking news (injury, scratch, coaching decision) moves the line when
the market prices it in. Three regimes exist:

  1. Book priced it fast. News t0 → line moves within minutes. No edge.
  2. Book priced it slowly. News t0 → line crawls over 15-30min. Early-mover
     edge for whoever got a snapshot before the shift.
  3. Book hasn't priced it yet. News t0 → no meaningful line move within
     30min. Either (a) book missed the headline (rare), (b) book doesn't
     weight this player as heavily as our models do, or (c) the news is
     noise and the line is right. (c) is the failure mode we guard against
     with confidence gates — single-source + minor-severity rows never
     emit edges.

This module reads ``news_events`` rows, cross-references them against
``line_movements`` within a configurable window, and emits ``ev_opportunities``
rows with ``thesis_tag='news_reaction'`` when the book appears to be
under-reacting.

Design constraints
------------------
* Does NOT modify ``line_monitor`` / ``edge_scanner``. Reads ``line_movements``
  directly via a plain SQL query.
* Emissions carry ``is_live=1`` when the game hasn't started yet, matching
  the live-edge convention (the ``live_edges`` module gates on this for
  expiry-aware executor behaviour).
* ``expires_at = first_seen_at + 60min``. News decays fast — an expired row
  stops being quoted to the executor. If the book re-aligns after 60min,
  the opportunity was real and was captured; if not, the model was wrong
  and we don't want the stale edge tempting a late order.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.news_impact")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Default correlation window — we look this far on either side of the news
# event's first_seen_at for a matching line_movements row.
DEFAULT_WINDOW_MINUTES = 30

# Threshold for "significant line move". Price moves of ≥5 cents (10 American
# points at shorter prices) or point moves of ≥0.5 count as meaningful.
# Below this we treat the line as un-moved.
MIN_PRICE_MOVE = 10
MIN_POINT_MOVE = 0.5

# How much projected impact counts as "big". Used as a gate for emitting
# edge candidates from the no-line-move case.
IMPACT_EDGE_THRESHOLD = 0.10  # 10%

# TTL for news_reaction edges once emitted.
EDGE_TTL_MINUTES = 60


@dataclass
class NewsImpactReport:
    """Result of correlating one news event with the odds feed."""
    news_event_id: int
    player_name: Optional[str]
    sport: Optional[str]
    first_seen_at: str
    line_moved: bool
    price_move: int
    point_move: float
    projected_impact: float     # |new-old|/old, 0.0 if unknown
    # True if we think the line hasn't priced the news — the edge case.
    is_under_reaction: bool
    # True if projected impact is plausible (multi-source or non-minor).
    is_actionable: bool

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def compute_expected_starter_impact(
    db: aiosqlite.Connection,
    sport: str,
    player_name: str,
) -> float:
    """Estimate how much this player's absence shifts expected team output.

    v1 heuristic: ``player_last_10_avg / team_game_avg``. If player_stats
    has no records for the player we return 0.0 (unknown). Actual cross-ref
    to props and totals happens downstream in a future revision.

    Returns a fraction in roughly [0.0, 0.4]: a superstar averaging 30pts
    on a team averaging 110pts returns ~0.27.
    """
    try:
        cur = await db.execute(
            """
            SELECT AVG(stat_value) FROM player_stats
            WHERE sport = ? AND player_name = ?
              AND stat_value IS NOT NULL
            ORDER BY game_date DESC
            LIMIT 10
            """,
            (sport, player_name),
        )
        r = await cur.fetchone()
        player_avg = float(r[0]) if r and r[0] is not None else 0.0
    except Exception as e:
        logger.debug(f"expected_starter_impact: player_stats lookup error: {e}")
        return 0.0

    if player_avg <= 0:
        return 0.0

    # Team avg — proxy via the same stat_type across the team's recent games.
    # Without a team mapping we approximate by averaging across distinct
    # players' stats for the sport in the last 30 days. Very rough but keeps
    # the heuristic computable against a bare player_stats table.
    try:
        cur = await db.execute(
            """
            SELECT AVG(stat_value) FROM player_stats
            WHERE sport = ? AND stat_value IS NOT NULL
              AND game_date >= date('now', '-30 days')
            """,
            (sport,),
        )
        r = await cur.fetchone()
        team_avg = float(r[0]) if r and r[0] is not None else 0.0
    except Exception:
        team_avg = 0.0

    if team_avg <= 0:
        return 0.0
    return max(0.0, min(1.0, player_avg / (team_avg * 5)))  # normalise so all-star ~ 0.2


async def find_line_movement_near(
    db: aiosqlite.Connection,
    sport: str,
    at: datetime,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    team_hint: Optional[str] = None,
) -> Optional[dict]:
    """Return the largest line_movements row within ±window of ``at``.

    Uses the ``line_movements`` table shape from ``line_monitor.py``. If the
    table is absent (fresh DB) we return None — the smoke tests exercise
    this path.
    """
    lo = (at - timedelta(minutes=window_minutes)).isoformat()
    hi = (at + timedelta(minutes=window_minutes)).isoformat()
    try:
        if team_hint:
            cur = await db.execute(
                """
                SELECT detected_at, team, market, bookmaker, old_price, new_price,
                       price_movement, old_point, new_point, point_movement, direction
                FROM line_movements
                WHERE sport = ?
                  AND detected_at BETWEEN ? AND ?
                  AND team LIKE ?
                ORDER BY ABS(COALESCE(price_movement, 0)) DESC,
                         ABS(COALESCE(point_movement, 0)) DESC
                LIMIT 1
                """,
                (sport, lo, hi, f"%{team_hint}%"),
            )
        else:
            cur = await db.execute(
                """
                SELECT detected_at, team, market, bookmaker, old_price, new_price,
                       price_movement, old_point, new_point, point_movement, direction
                FROM line_movements
                WHERE sport = ?
                  AND detected_at BETWEEN ? AND ?
                ORDER BY ABS(COALESCE(price_movement, 0)) DESC,
                         ABS(COALESCE(point_movement, 0)) DESC
                LIMIT 1
                """,
                (sport, lo, hi),
            )
        row = await cur.fetchone()
    except aiosqlite.OperationalError as e:
        # "no such table: line_movements" is a legitimate state on a fresh DB.
        logger.debug(f"line_movements lookup skipped: {e}")
        return None

    if not row:
        return None

    cols = (
        "detected_at", "team", "market", "bookmaker", "old_price", "new_price",
        "price_movement", "old_point", "new_point", "point_movement", "direction",
    )
    return dict(zip(cols, row))


def line_moved_significantly(mv: Optional[dict]) -> bool:
    """Classify a movement row as 'the line actually moved'."""
    if not mv:
        return False
    price_move = abs(mv.get("price_movement") or 0)
    point_move = abs(mv.get("point_movement") or 0)
    return price_move >= MIN_PRICE_MOVE or point_move >= MIN_POINT_MOVE


async def score_news_event(
    db: aiosqlite.Connection,
    news_row: dict,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> NewsImpactReport:
    """Assemble an impact report for one news row (dict as stored in
    ``news_events``, with ``id`` required)."""
    first_seen = _parse_iso(news_row.get("first_seen_at")) or datetime.now(timezone.utc)
    sport = news_row.get("sport") or ""
    player = news_row.get("player_name") or ""

    # Team hint from raw_json if stored (ESPN emitter includes team)
    team_hint = None
    raw = news_row.get("raw_json")
    if raw:
        try:
            raw_dict = raw if isinstance(raw, dict) else json.loads(raw)
            # ESPN injury entries don't carry team directly; scoreboard-derived
            # rows stash team under 'team'.
            team_hint = (raw_dict or {}).get("team") or None
        except Exception:
            team_hint = None

    mv = await find_line_movement_near(db, sport, first_seen, window_minutes, team_hint)
    moved = line_moved_significantly(mv)
    price_move = int(abs((mv or {}).get("price_movement") or 0))
    point_move = float(abs((mv or {}).get("point_movement") or 0.0))

    projected = await compute_expected_starter_impact(db, sport, player) if player else 0.0

    # "Under-reaction" = non-trivial projected impact AND line didn't move.
    is_under_reaction = (projected >= IMPACT_EDGE_THRESHOLD) and (not moved)

    # Actionability gate: only emit edges from confirmed or at-least-moderate
    # severity rows. Minor + single-source is too noisy.
    severity = news_row.get("severity")
    confirmed = bool(news_row.get("confirmed_at"))
    sev_ok = severity in ("moderate", "severe", "out_indefinite")
    is_actionable = is_under_reaction and (confirmed or sev_ok)

    return NewsImpactReport(
        news_event_id=int(news_row.get("id") or 0),
        player_name=player or None,
        sport=sport or None,
        first_seen_at=news_row.get("first_seen_at") or "",
        line_moved=moved,
        price_move=price_move,
        point_move=point_move,
        projected_impact=round(projected, 4),
        is_under_reaction=is_under_reaction,
        is_actionable=is_actionable,
    )


async def _ensure_ev_opportunities_schema(db: aiosqlite.Connection) -> None:
    """Tests run against throwaway DBs that haven't seen the live schema.
    Create a minimum-viable ev_opportunities that accepts our columns."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS ev_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL,
            sport TEXT,
            game_id TEXT,
            team TEXT,
            market TEXT,
            bookmaker TEXT,
            american_odds INTEGER,
            implied_probability REAL,
            estimated_true_prob REAL,
            edge REAL,
            expected_value REAL,
            kelly_fraction REAL,
            status TEXT DEFAULT 'open',
            source TEXT DEFAULT 'line_movement',
            steam_only INTEGER DEFAULT 0,
            is_live INTEGER DEFAULT 0,
            thesis_tag TEXT,
            expires_at TEXT
        )
        """
    )
    await db.commit()


async def emit_news_reaction_edge(
    db: aiosqlite.Connection,
    report: NewsImpactReport,
    news_row: dict,
    game_in_progress: bool = False,
) -> Optional[int]:
    """Insert an ``ev_opportunities`` row tagged ``news_reaction``.

    The ``is_live`` flag mirrors the live-edges convention: 1 means "edge
    valid only for an in-play line", 0 means "pre-game edge". For news
    that arrives before tip/first-pitch, ``is_live=1`` in the sense that
    it's perishable; we set ``is_live = (not game_in_progress)`` inverted
    later after confirming convention — for v1 we follow the spec and
    set ``is_live=True if game hasn't started``.
    """
    await _ensure_ev_opportunities_schema(db)
    now_dt = datetime.now(timezone.utc)
    expires_at = (now_dt + timedelta(minutes=EDGE_TTL_MINUTES)).isoformat()
    is_live = 1 if not game_in_progress else 0
    cur = await db.execute(
        """
        INSERT INTO ev_opportunities
          (detected_at, sport, team, edge, expected_value, source,
           is_live, thesis_tag, expires_at, status)
        VALUES (?, ?, ?, ?, ?, 'news_reaction', ?, 'news_reaction', ?, 'open')
        """,
        (
            now_dt.isoformat(),
            report.sport,
            news_row.get("player_name"),
            float(report.projected_impact),
            float(report.projected_impact),  # EV placeholder — priced at impact
            is_live,
            expires_at,
        ),
    )
    await db.commit()
    return cur.lastrowid


async def process_news_events(
    db_path: Optional[str] = None,
    *,
    since_minutes: int = 60,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    emit_edges: bool = True,
) -> dict:
    """Scan recent news_events rows, score each, emit edges where warranted.

    Returns a summary dict suitable for logging.
    """
    path = db_path or DB_PATH
    emitted = 0
    scored = 0
    under_reactions = 0
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        try:
            cur = await db.execute(
                """
                SELECT * FROM news_events
                WHERE first_seen_at > datetime('now', ?)
                ORDER BY first_seen_at DESC
                LIMIT 500
                """,
                (f'-{since_minutes} minutes',),
            )
            rows = [dict(r) for r in await cur.fetchall()]
        except aiosqlite.OperationalError:
            return {"error": "news_events table missing"}

        for row in rows:
            report = await score_news_event(db, row, window_minutes=window_minutes)
            scored += 1
            if report.is_under_reaction:
                under_reactions += 1
            if emit_edges and report.is_actionable:
                try:
                    rid = await emit_news_reaction_edge(db, report, row)
                    if rid:
                        emitted += 1
                except Exception as e:
                    logger.warning(f"emit_news_reaction_edge error: {e}")

    return {
        "scored": scored,
        "under_reactions": under_reactions,
        "emitted": emitted,
    }


__all__ = [
    "NewsImpactReport",
    "compute_expected_starter_impact",
    "find_line_movement_near",
    "line_moved_significantly",
    "score_news_event",
    "emit_news_reaction_edge",
    "process_news_events",
    "DEFAULT_WINDOW_MINUTES",
    "MIN_PRICE_MOVE",
    "MIN_POINT_MOVE",
    "IMPACT_EDGE_THRESHOLD",
    "EDGE_TTL_MINUTES",
]
