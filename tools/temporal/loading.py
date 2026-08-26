"""Data loading: SQLite -> Polars DataFrames."""

import os
import sqlite3
from typing import Optional

import polars as pl
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Open a read-only SQLite connection (sync — Polars is sync)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def load_game_results(
    db_path: str = DB_PATH,
    sport: Optional[str] = None,
    date_range: Optional[tuple[str, str]] = None,
) -> pl.DataFrame:
    """
    Load game_results into a Polars DataFrame.

    Args:
        db_path: Path to SQLite database.
        sport: Filter by sport key (e.g. 'basketball_nba').
        date_range: Tuple of (start_date, end_date) as 'YYYY-MM-DD' strings.

    Returns:
        Polars DataFrame with columns: id, sport, game_date, home_team, away_team,
        home_score, away_score, total_score, spread_result, winner, source.
    """
    conn = _connect(db_path)
    query = "SELECT * FROM game_results WHERE 1=1"
    params = []

    if sport:
        query += " AND sport = ?"
        params.append(sport)
    if date_range:
        query += " AND game_date >= ? AND game_date <= ?"
        params.extend(date_range)

    query += " ORDER BY game_date"

    cursor = conn.execute(query, params)
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return pl.DataFrame(schema={
            "id": pl.Int64, "sport": pl.Utf8, "game_date": pl.Utf8,
            "home_team": pl.Utf8, "away_team": pl.Utf8,
            "home_score": pl.Int64, "away_score": pl.Int64,
            "total_score": pl.Int64, "spread_result": pl.Float64,
            "winner": pl.Utf8, "source": pl.Utf8,
        })

    data = {col: [row[i] for row in rows] for i, col in enumerate(columns)}
    df = pl.DataFrame(data)

    # Ensure game_date is string for consistent handling
    if "game_date" in df.columns:
        df = df.with_columns(pl.col("game_date").cast(pl.Utf8))

    return df


def load_odds_snapshots(
    db_path: str = DB_PATH,
    sport: Optional[str] = None,
    date_range: Optional[tuple[str, str]] = None,
) -> pl.DataFrame:
    """
    Load odds_snapshots_v2 joined with markets into a Polars DataFrame.

    Returns columns: snapshot_id, market_id, book_id, outcome_name,
    price_american, price_decimal, point, snapshot_time, sport,
    event_name, home_team, away_team, commence_time, market_type.
    """
    conn = _connect(db_path)
    query = """
        SELECT s.snapshot_id, s.market_id, s.book_id, s.outcome_name,
               s.price_american, s.price_decimal, s.point, s.snapshot_time,
               m.sport, m.event_name, m.home_team, m.away_team,
               m.commence_time, m.market_type
        FROM odds_snapshots_v2 s
        JOIN markets m ON s.market_id = m.market_id
        WHERE 1=1
    """
    params = []

    if sport:
        query += " AND m.sport = ?"
        params.append(sport)
    if date_range:
        query += " AND DATE(s.snapshot_time) >= ? AND DATE(s.snapshot_time) <= ?"
        params.extend(date_range)

    query += " ORDER BY s.snapshot_time"

    cursor = conn.execute(query, params)
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return pl.DataFrame()

    data = {col: [row[i] for row in rows] for i, col in enumerate(columns)}
    return pl.DataFrame(data)


def load_player_stats(
    db_path: str = DB_PATH,
    sport: Optional[str] = None,
    date_range: Optional[tuple[str, str]] = None,
) -> pl.DataFrame:
    """
    Load player_stats into a Polars DataFrame.

    Returns columns: id, sport, event_id, game_date, player_name, team,
    stat_type, stat_value, minutes_played, source, created_at.
    """
    conn = _connect(db_path)
    query = "SELECT * FROM player_stats WHERE 1=1"
    params = []

    if sport:
        query += " AND sport = ?"
        params.append(sport)
    if date_range:
        query += " AND game_date >= ? AND game_date <= ?"
        params.extend(date_range)

    query += " ORDER BY game_date"

    cursor = conn.execute(query, params)
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return pl.DataFrame()

    data = {col: [row[i] for row in rows] for i, col in enumerate(columns)}
    return pl.DataFrame(data)


def load_backtest_events(
    db_path: str = DB_PATH,
    run_id: Optional[str] = None,
) -> pl.DataFrame:
    """
    Load backtest_events into a Polars DataFrame.

    Args:
        db_path: Path to SQLite database.
        run_id: Filter to a specific backtest run.

    Returns:
        Polars DataFrame with all backtest_events columns.
    """
    conn = _connect(db_path)
    query = "SELECT * FROM backtest_events WHERE 1=1"
    params = []

    if run_id:
        query += " AND run_id = ?"
        params.append(run_id)

    query += " ORDER BY game_date"

    cursor = conn.execute(query, params)
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return pl.DataFrame()

    data = {col: [row[i] for row in rows] for i, col in enumerate(columns)}
    return pl.DataFrame(data)

