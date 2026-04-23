"""Migration 007: add ``local_game_date`` column to every table that stores
a game date, and backfill it from commence_time + home_team using the
venue-timezone helper in ``tools.game_dates``.

Problem this fixes
------------------
Callisto had at least two conflicting definitions of "the date of a game":
  - ``game_results.game_date`` — populated from ESPN's ET-oriented scoreboard
    date
  - ``backtest_events.game_date`` — populated by a ``commence_time[:10]``
    string-slice, which is the UTC date
  - ``paper_trades.game_date`` — recently (pre-fix) pulled from
    ``commence_time[:10]`` via ``_game_date_from_commence``

For a Dodgers home game commencing ``2026-04-22T02:30:00Z`` (April 21 PT),
the UTC-sliced path tagged it ``2026-04-22`` while the ESPN path tagged it
``2026-04-21``. Joins between ``game_results`` and ``backtest_events`` then
silently fell back to a ``±1 day`` fuzzy resolver in ``backtest.py`` that
papered over the mismatch (at the cost of occasional wrong-game matches on
adjacent days). Day-of-week and day/night cohort aggregations in
``temporal_analysis.py`` were wrong for every West-Coast late game.

What this migration does
------------------------
1. Adds ``local_game_date DATE`` column to:
     - ``game_results``
     - ``game_contexts``
     - ``backtest_events``
     - ``paper_trades``
     - ``clv_log`` (if present — it's created lazily in some installs)
2. Backfills each row's ``local_game_date`` by:
     - Looking up ``markets.commence_time`` via ``event_id`` when available
     - Converting UTC → venue local timezone using ``home_team``
     - Falling back to the existing ``game_date`` when ``commence_time`` is
       unreachable. Existing ``game_date`` values are ambiguous (the whole
       point of this migration) but using them as a last-resort floor is
       strictly better than NULL, and the "days off" population is bounded
       to rows where we never had a UTC timestamp to begin with.
3. Reports counts: total rows, backfilled-from-commence, fallback-from-
   existing-game_date, un-normalizable.

Idempotency
-----------
- Column adds guarded by an ``ALTER TABLE ... ADD COLUMN`` wrapped in a
  "duplicate column" catch, matching the runner's pattern.
- Backfill only UPDATEs rows where ``local_game_date IS NULL`` — safe to
  re-run without double-work.

Why we keep ``game_date``
-------------------------
The legacy column is load-bearing for indexes and a dozen queries; flipping
it in one migration would balloon risk. We add the new column, update
consumers to read from it, and leave the legacy column in place (flagged
DEPRECATED in comments) for a subsequent cleanup migration.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timezone


logger = logging.getLogger("callisto.migrations.007")


# Tables that get a ``local_game_date`` column. Ordered so a partial run is
# still useful (game_results first — the consumer that matters most).
TARGET_TABLES = [
    "game_results",
    "game_contexts",
    "backtest_events",
    "paper_trades",
    "clv_log",
]


# ─────────────────────────────────────────────
# Inline copy of the tz helper
# ─────────────────────────────────────────────
# Intentional duplication: the migration runner imports this module at a
# point where ``tools.game_dates`` may not yet be importable in older
# installs that run migrations before the tools package has been fully
# bootstrapped. Inline the minimal subset so the migration is self-
# contained. The tables we write today must match tools.game_dates exactly;
# both are sourced from the same team list.

from zoneinfo import ZoneInfo

_MLB_TEAM_TZ = {
    "Baltimore Orioles": "America/New_York", "Boston Red Sox": "America/New_York",
    "New York Yankees": "America/New_York", "Tampa Bay Rays": "America/New_York",
    "Toronto Blue Jays": "America/Toronto",
    "Chicago White Sox": "America/Chicago", "Cleveland Guardians": "America/New_York",
    "Detroit Tigers": "America/Detroit", "Kansas City Royals": "America/Chicago",
    "Minnesota Twins": "America/Chicago",
    "Houston Astros": "America/Chicago", "Los Angeles Angels": "America/Los_Angeles",
    "Oakland Athletics": "America/Los_Angeles", "Athletics": "America/Los_Angeles",
    "Seattle Mariners": "America/Los_Angeles", "Texas Rangers": "America/Chicago",
    "Atlanta Braves": "America/New_York", "Miami Marlins": "America/New_York",
    "New York Mets": "America/New_York", "Philadelphia Phillies": "America/New_York",
    "Washington Nationals": "America/New_York",
    "Chicago Cubs": "America/Chicago", "Cincinnati Reds": "America/New_York",
    "Milwaukee Brewers": "America/Chicago", "Pittsburgh Pirates": "America/New_York",
    "St. Louis Cardinals": "America/Chicago",
    "Arizona Diamondbacks": "America/Phoenix", "Colorado Rockies": "America/Denver",
    "Los Angeles Dodgers": "America/Los_Angeles", "San Diego Padres": "America/Los_Angeles",
    "San Francisco Giants": "America/Los_Angeles",
}
_NBA_TEAM_TZ = {
    "Atlanta Hawks": "America/New_York", "Boston Celtics": "America/New_York",
    "Brooklyn Nets": "America/New_York", "Charlotte Hornets": "America/New_York",
    "Chicago Bulls": "America/Chicago", "Cleveland Cavaliers": "America/New_York",
    "Dallas Mavericks": "America/Chicago", "Denver Nuggets": "America/Denver",
    "Detroit Pistons": "America/Detroit", "Golden State Warriors": "America/Los_Angeles",
    "Houston Rockets": "America/Chicago", "Indiana Pacers": "America/Indiana/Indianapolis",
    "LA Clippers": "America/Los_Angeles", "Los Angeles Clippers": "America/Los_Angeles",
    "Los Angeles Lakers": "America/Los_Angeles", "Memphis Grizzlies": "America/Chicago",
    "Miami Heat": "America/New_York", "Milwaukee Bucks": "America/Chicago",
    "Minnesota Timberwolves": "America/Chicago", "New Orleans Pelicans": "America/Chicago",
    "New York Knicks": "America/New_York", "Oklahoma City Thunder": "America/Chicago",
    "Orlando Magic": "America/New_York", "Philadelphia 76ers": "America/New_York",
    "Phoenix Suns": "America/Phoenix", "Portland Trail Blazers": "America/Los_Angeles",
    "Sacramento Kings": "America/Los_Angeles", "San Antonio Spurs": "America/Chicago",
    "Toronto Raptors": "America/Toronto", "Utah Jazz": "America/Denver",
    "Washington Wizards": "America/New_York",
}
_NHL_TEAM_TZ = {
    "Anaheim Ducks": "America/Los_Angeles", "Arizona Coyotes": "America/Phoenix",
    "Boston Bruins": "America/New_York", "Buffalo Sabres": "America/New_York",
    "Calgary Flames": "America/Edmonton", "Carolina Hurricanes": "America/New_York",
    "Chicago Blackhawks": "America/Chicago", "Colorado Avalanche": "America/Denver",
    "Columbus Blue Jackets": "America/New_York", "Dallas Stars": "America/Chicago",
    "Detroit Red Wings": "America/Detroit", "Edmonton Oilers": "America/Edmonton",
    "Florida Panthers": "America/New_York", "Los Angeles Kings": "America/Los_Angeles",
    "Minnesota Wild": "America/Chicago", "Montreal Canadiens": "America/Montreal",
    "Nashville Predators": "America/Chicago", "New Jersey Devils": "America/New_York",
    "New York Islanders": "America/New_York", "New York Rangers": "America/New_York",
    "Ottawa Senators": "America/Toronto", "Philadelphia Flyers": "America/New_York",
    "Pittsburgh Penguins": "America/New_York", "San Jose Sharks": "America/Los_Angeles",
    "Seattle Kraken": "America/Los_Angeles", "St. Louis Blues": "America/Chicago",
    "Tampa Bay Lightning": "America/New_York", "Toronto Maple Leafs": "America/Toronto",
    "Utah Hockey Club": "America/Denver", "Vancouver Canucks": "America/Vancouver",
    "Vegas Golden Knights": "America/Los_Angeles", "Washington Capitals": "America/New_York",
    "Winnipeg Jets": "America/Winnipeg",
}
_NFL_TEAM_TZ = {
    "Arizona Cardinals": "America/Phoenix", "Atlanta Falcons": "America/New_York",
    "Baltimore Ravens": "America/New_York", "Buffalo Bills": "America/New_York",
    "Carolina Panthers": "America/New_York", "Chicago Bears": "America/Chicago",
    "Cincinnati Bengals": "America/New_York", "Cleveland Browns": "America/New_York",
    "Dallas Cowboys": "America/Chicago", "Denver Broncos": "America/Denver",
    "Detroit Lions": "America/Detroit", "Green Bay Packers": "America/Chicago",
    "Houston Texans": "America/Chicago", "Indianapolis Colts": "America/Indiana/Indianapolis",
    "Jacksonville Jaguars": "America/New_York", "Kansas City Chiefs": "America/Chicago",
    "Las Vegas Raiders": "America/Los_Angeles", "Los Angeles Chargers": "America/Los_Angeles",
    "Los Angeles Rams": "America/Los_Angeles", "Miami Dolphins": "America/New_York",
    "Minnesota Vikings": "America/Chicago", "New England Patriots": "America/New_York",
    "New Orleans Saints": "America/Chicago", "New York Giants": "America/New_York",
    "New York Jets": "America/New_York", "Philadelphia Eagles": "America/New_York",
    "Pittsburgh Steelers": "America/New_York", "San Francisco 49ers": "America/Los_Angeles",
    "Seattle Seahawks": "America/Los_Angeles", "Tampa Bay Buccaneers": "America/New_York",
    "Tennessee Titans": "America/Chicago", "Washington Commanders": "America/New_York",
}
_SPORT_TEAM_TZ = {
    "baseball_mlb": _MLB_TEAM_TZ,
    "basketball_nba": _NBA_TEAM_TZ,
    "icehockey_nhl": _NHL_TEAM_TZ,
    "americanfootball_nfl": _NFL_TEAM_TZ,
}
_DEFAULT_TZ_NAME = "America/New_York"

_tz_cache: dict[str, ZoneInfo] = {}


def _get_tz(sport: str, home_team: str | None) -> ZoneInfo:
    m = _SPORT_TEAM_TZ.get((sport or "").strip(), {})
    home = (home_team or "").strip()
    name = m.get(home, _DEFAULT_TZ_NAME)
    z = _tz_cache.get(name)
    if z is None:
        try:
            z = ZoneInfo(name)
        except Exception:
            z = ZoneInfo(_DEFAULT_TZ_NAME)
        _tz_cache[name] = z
    return z


def _parse_utc(ts: str | None) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # try space-separated SQLite datetime, e.g. '2026-04-22 02:30:00'
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _local_date_from_utc(ts: str | None, sport: str, home_team: str | None) -> date | None:
    dt = _parse_utc(ts)
    if dt is None:
        return None
    return dt.astimezone(_get_tz(sport, home_team)).date()


# ─────────────────────────────────────────────
# Column add (idempotent)
# ─────────────────────────────────────────────

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == col for row in cols)


def _add_column(conn: sqlite3.Connection, table: str) -> bool:
    """Add ``local_game_date`` column if missing. Returns True if added."""
    if not _table_exists(conn, table):
        return False
    if _column_exists(conn, table, "local_game_date"):
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN local_game_date DATE")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            return False
        raise
    # Matching index — backtest joins and the temporal_analysis GROUP BY both
    # hit this column. Without an index the first consumer post-migration
    # would trigger a full scan.
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_local_date "
        f"ON {table}(local_game_date)"
    )
    return True


# ─────────────────────────────────────────────
# Backfill
# ─────────────────────────────────────────────

BATCH_SIZE = 2000


def _backfill_from_markets(
    conn: sqlite3.Connection, table: str, report: dict
) -> None:
    """Populate ``local_game_date`` via JOIN on ``markets.commence_time``.

    For every row with ``event_id`` that resolves to a ``markets`` row, we
    have a reliable UTC commence_time → canonical local date. This is the
    primary backfill path for backtest_events / paper_trades.

    Some tables (``backtest_events``) don't carry their own ``home_team``
    column, so we also source ``home_team`` from the markets join. For
    ``game_results`` there's no ``event_id`` column, so this function is a
    no-op for that table (handled by _backfill_from_game_date as
    best-effort).
    """
    if not _table_exists(conn, "markets"):
        return
    if not _column_exists(conn, table, "event_id"):
        return

    # Decide which column carries home_team. Some tables have their own,
    # others must rely on the markets join.
    own_home = _column_exists(conn, table, "home_team")
    home_expr = "t.home_team" if own_home else "m.home_team"

    # Pull candidate rows + their matching market commence_time.
    # Batch to keep memory + WAL bounded on large tables.
    offset = 0
    updated_total = 0
    shifted_days: list[tuple] = []

    while True:
        rows = conn.execute(
            f"""
            SELECT t.rowid, t.event_id, t.sport, {home_expr},
                   t.game_date, m.commence_time
            FROM {table} t
            LEFT JOIN markets m ON m.event_id = t.event_id
            WHERE t.local_game_date IS NULL
              AND m.commence_time IS NOT NULL
            LIMIT ? OFFSET ?
            """,
            (BATCH_SIZE, offset),
        ).fetchall()
        if not rows:
            break

        updates: list[tuple] = []
        for rowid, event_id, sport, home_team, legacy_game_date, commence in rows:
            local = _local_date_from_utc(commence, sport or "", home_team)
            if local is None:
                continue
            local_str = local.isoformat()
            updates.append((local_str, rowid))
            # Track rows where the local date differs from the legacy
            # game_date — these are the ones the old bug was silently
            # miscounting.
            if legacy_game_date and local_str != str(legacy_game_date)[:10]:
                if len(shifted_days) < 20:
                    shifted_days.append(
                        (table, event_id, home_team, legacy_game_date, local_str)
                    )
                report.setdefault("_shifted_count", {}).setdefault(table, 0)
                report["_shifted_count"][table] += 1

        if updates:
            conn.executemany(
                f"UPDATE {table} SET local_game_date = ? WHERE rowid = ?",
                updates,
            )
            updated_total += len(updates)

        # Advance; note we don't shrink the candidate set via a predicate,
        # so LIMIT+OFFSET is safe because the UPDATE just made those rows
        # stop matching the WHERE. But we still advance to guard against
        # rows that don't get updated (null local).
        if len(rows) < BATCH_SIZE:
            break
        offset += len(rows)

    report[f"{table}_backfilled_from_commence"] = updated_total
    if shifted_days:
        report.setdefault("_shifted_samples", []).extend(shifted_days)


def _backfill_from_game_date(
    conn: sqlite3.Connection, table: str, report: dict
) -> None:
    """Last-resort: when commence_time isn't reachable, reuse the existing
    ``game_date`` verbatim.

    Rationale: on ``game_results`` the legacy ``game_date`` was ESPN's
    ET-oriented date — for East-Coast and Central teams that equals the
    local date; for West-Coast late games it's off by one. Until we
    re-fetch ESPN with commence_time attached, this is the best we can do
    and it's NO WORSE than the pre-migration state.

    Only fills rows still NULL. Idempotent.
    """
    if not _table_exists(conn, table):
        return
    if not _column_exists(conn, table, "game_date"):
        return

    cur = conn.execute(
        f"""
        UPDATE {table}
        SET local_game_date = date(game_date)
        WHERE local_game_date IS NULL
          AND game_date IS NOT NULL
        """
    )
    report[f"{table}_backfilled_from_game_date"] = cur.rowcount


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def up(conn: sqlite3.Connection) -> None:
    report: dict = {}

    added_cols = []
    for table in TARGET_TABLES:
        if _add_column(conn, table):
            added_cols.append(table)
    report["columns_added"] = added_cols

    # Primary backfill path — via markets.commence_time JOIN
    for table in ("backtest_events", "paper_trades", "game_contexts"):
        _backfill_from_markets(conn, table, report)

    # Secondary path — copy legacy game_date forward (handles game_results
    # and any rows where markets join missed).
    for table in TARGET_TABLES:
        _backfill_from_game_date(conn, table, report)

    # Count orphans — rows where we still couldn't derive a local_game_date.
    orphans: dict[str, int] = {}
    for table in TARGET_TABLES:
        if not _table_exists(conn, table):
            continue
        (cnt,) = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE local_game_date IS NULL"
        ).fetchone()
        if cnt:
            orphans[table] = cnt
    report["orphans"] = orphans

    logger.info(f"Migration 007 report: {report}")


def down(conn: sqlite3.Connection) -> None:
    # SQLite ALTER TABLE DROP COLUMN requires 3.35+; even then, reverting
    # would lose information that downstream consumers have started
    # depending on. Force manual rollback.
    raise NotImplementedError(
        "Rollback of 007_local_game_dates is manual — consumers depend on "
        "the new column; reverting needs a coordinated downgrade."
    )
