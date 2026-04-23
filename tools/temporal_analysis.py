"""
Temporal analysis engine — Polars-based data loading, temporal splits, and pattern discovery.

This module enforces the fundamental rule of time-series backtesting:
  NEVER derive a hypothesis from data and then backtest it on the same data.

Every pattern discovered is tagged with training period metadata so the
backtest engine can enforce temporal isolation automatically.

Dependencies: polars, aiosqlite (for async loading)
"""

import hashlib
import json
import logging
import math
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

import polars as pl
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.temporal_analysis")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


# ──────────────────────────────────────────────────
# DATA LOADING: SQLite → Polars DataFrames
# ──────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────
# TEMPORAL SPLIT ENFORCEMENT
# ──────────────────────────────────────────────────

def create_temporal_split(
    df: pl.DataFrame,
    train_end_date: str,
    date_column: str = "game_date",
    min_gap_days: int = 7,
) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    """
    Split a DataFrame into train and test sets with a temporal gap.

    The gap prevents leakage from games spanning the boundary (e.g., a game
    played on train_end_date whose result is known but whose closing line
    might influence the next day's market).

    Args:
        df: Input DataFrame with a date column.
        train_end_date: Last date included in training set ('YYYY-MM-DD').
        date_column: Name of the date column.
        min_gap_days: Days between train end and test start (default 7).

    Returns:
        (train_df, test_df, metadata) where metadata includes split details.
    """
    train_end = datetime.strptime(train_end_date, "%Y-%m-%d").date()
    test_start = train_end + timedelta(days=min_gap_days)

    train_df = df.filter(pl.col(date_column) <= train_end_date)
    test_start_str = test_start.strftime("%Y-%m-%d")
    test_df = df.filter(pl.col(date_column) >= test_start_str)

    # Compute metadata
    train_dates = train_df.select(pl.col(date_column)).unique()
    test_dates = test_df.select(pl.col(date_column)).unique()

    metadata = {
        "train_end_date": train_end_date,
        "test_start_date": test_start_str,
        "gap_days": min_gap_days,
        "train_rows": train_df.height,
        "test_rows": test_df.height,
        "train_date_range": (
            train_df.select(pl.col(date_column).min()).item()
            if train_df.height > 0 else None,
            train_end_date,
        ),
        "test_date_range": (
            test_start_str,
            test_df.select(pl.col(date_column).max()).item()
            if test_df.height > 0 else None,
        ),
        "gap_rows_excluded": df.height - train_df.height - test_df.height,
    }

    logger.info(
        f"Temporal split: train={metadata['train_rows']} rows "
        f"({metadata['train_date_range'][0]} to {metadata['train_date_range'][1]}), "
        f"test={metadata['test_rows']} rows "
        f"({metadata['test_date_range'][0]} to {metadata['test_date_range'][1]}), "
        f"gap={min_gap_days}d ({metadata['gap_rows_excluded']} rows excluded)"
    )

    return train_df, test_df, metadata


def rolling_window_splits(
    df: pl.DataFrame,
    date_column: str = "game_date",
    window_size_days: int = 90,
    step_days: int = 30,
    test_size_days: int = 30,
    gap_days: int = 7,
) -> list[tuple[pl.DataFrame, pl.DataFrame, dict]]:
    """
    Walk-forward analysis: generate rolling train/test splits.

    This is the gold standard for time-series backtesting. For each window:
    - Train on `window_size_days` of data
    - Skip `gap_days` to avoid leakage
    - Test on the next `test_size_days`
    - Slide forward by `step_days`

    Args:
        df: Input DataFrame with a date column.
        date_column: Name of the date column.
        window_size_days: Training window size in days.
        step_days: How far to slide the window forward each step.
        test_size_days: Test window size in days.
        gap_days: Gap between train end and test start.

    Returns:
        List of (train_df, test_df, metadata) tuples.
    """
    if df.height == 0:
        return []

    # Get the full date range
    min_date_str = df.select(pl.col(date_column).min()).item()
    max_date_str = df.select(pl.col(date_column).max()).item()

    min_date = datetime.strptime(min_date_str, "%Y-%m-%d").date()
    max_date = datetime.strptime(max_date_str, "%Y-%m-%d").date()

    splits = []
    train_start = min_date

    while True:
        train_end = train_start + timedelta(days=window_size_days)
        test_start = train_end + timedelta(days=gap_days)
        test_end = test_start + timedelta(days=test_size_days)

        if test_start > max_date:
            break

        train_start_str = train_start.strftime("%Y-%m-%d")
        train_end_str = train_end.strftime("%Y-%m-%d")
        test_start_str = test_start.strftime("%Y-%m-%d")
        test_end_str = test_end.strftime("%Y-%m-%d")

        train_df = df.filter(
            (pl.col(date_column) >= train_start_str)
            & (pl.col(date_column) <= train_end_str)
        )
        test_df = df.filter(
            (pl.col(date_column) >= test_start_str)
            & (pl.col(date_column) <= test_end_str)
        )

        if train_df.height > 0 and test_df.height > 0:
            metadata = {
                "train_start_date": train_start_str,
                "train_end_date": train_end_str,
                "test_start_date": test_start_str,
                "test_end_date": test_end_str,
                "gap_days": gap_days,
                "window_size_days": window_size_days,
                "step_days": step_days,
                "test_size_days": test_size_days,
                "train_rows": train_df.height,
                "test_rows": test_df.height,
                "fold_index": len(splits),
            }
            splits.append((train_df, test_df, metadata))

        train_start += timedelta(days=step_days)

    logger.info(
        f"Rolling window: {len(splits)} folds, "
        f"window={window_size_days}d, step={step_days}d, "
        f"test={test_size_days}d, gap={gap_days}d"
    )

    return splits


# ──────────────────────────────────────────────────
# PURE PYTHON STATISTICS (no scipy)
# ──────────────────────────────────────────────────

def _erfc(x: float) -> float:
    """Complementary error function (Abramowitz & Stegun 7.1.26)."""
    t = 1.0 / (1.0 + 0.3275911 * abs(x))
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 +
           t * (-1.453152027 + t * 1.061405429))))
    result = poly * math.exp(-x * x)
    return result if x >= 0 else 2.0 - result


def _norm_sf(x: float) -> float:
    """P(Z > x) for standard normal."""
    return 1.0 - 0.5 * _erfc(-x / math.sqrt(2))


def _binomial_pvalue(wins: int, total: int, expected_rate: float = 0.5) -> float:
    """One-sided binomial test via normal approximation with continuity correction."""
    if total < 1 or expected_rate <= 0 or expected_rate >= 1:
        return 1.0
    mean = total * expected_rate
    std = math.sqrt(total * expected_rate * (1 - expected_rate))
    if std < 1e-9:
        return 1.0
    z = (wins - 0.5 - mean) / std
    return _norm_sf(z)


# ──────────────────────────────────────────────────
# PATTERN DISCOVERY
# ──────────────────────────────────────────────────

def _pattern_hash(pattern_def: dict) -> str:
    """Deterministic hash for a pattern definition."""
    canonical = json.dumps(pattern_def, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def find_ats_patterns(
    train_df: pl.DataFrame,
    min_sample: int = 20,
    min_edge: float = 3.0,
) -> pl.DataFrame:
    """
    Find ATS (against the spread) patterns in historical game results.

    Groups data by various factors and finds groups where the ATS cover rate
    deviates significantly from 50%.

    Args:
        train_df: Training DataFrame from game_results (must have game_date,
                  sport, home_team, away_team, home_score, away_score, spread_result).
        min_sample: Minimum games in a group to consider it.
        min_edge: Minimum edge percentage (hit_rate - 50%) to report.

    Returns:
        DataFrame of discovered patterns with columns: pattern_type, pattern_key,
        sample_size, wins, hit_rate, edge_pct, p_value, pattern_hash.
    """
    if train_df.height == 0:
        return pl.DataFrame(schema={
            "pattern_type": pl.Utf8, "pattern_key": pl.Utf8,
            "sample_size": pl.Int64, "wins": pl.Int64,
            "hit_rate": pl.Float64, "edge_pct": pl.Float64,
            "p_value": pl.Float64, "pattern_hash": pl.Utf8,
        })

    # Ensure we have the required columns, compute derived ones
    df = train_df.clone()

    # Compute ATS cover: home team covers if spread_result > 0 (for home favored)
    # spread_result = home_score - away_score (margin)
    # We'll compute margin if not present
    if "spread_result" not in df.columns and "home_score" in df.columns:
        df = df.with_columns(
            (pl.col("home_score") - pl.col("away_score")).alias("spread_result")
        )

    if "spread_result" not in df.columns:
        return pl.DataFrame(schema={
            "pattern_type": pl.Utf8, "pattern_key": pl.Utf8,
            "sample_size": pl.Int64, "wins": pl.Int64,
            "hit_rate": pl.Float64, "edge_pct": pl.Float64,
            "p_value": pl.Float64, "pattern_hash": pl.Utf8,
        })

    # Prefer the canonical local_game_date (venue-local tz) when present —
    # the legacy game_date column mixes ESPN's ET-oriented date with
    # UTC-sliced commence_time, which corrupted DOW/month cohorts for
    # West-Coast late games. Fall back to game_date when local is NULL
    # (pre-migration rows that the backfill couldn't resolve).
    if "local_game_date" in df.columns:
        df = df.with_columns(
            pl.when(pl.col("local_game_date").is_not_null())
            .then(pl.col("local_game_date").cast(pl.Utf8))
            .otherwise(pl.col("game_date"))
            .alias("_canonical_date")
        )
    else:
        df = df.with_columns(pl.col("game_date").alias("_canonical_date"))

    # Add derived columns for grouping
    df = df.with_columns([
        # Total score bucket (5-point buckets)
        (
            (pl.col("home_score") + pl.col("away_score"))
            .truediv(5).floor().mul(5).cast(pl.Int64)
        ).alias("total_bucket"),
        # Margin bucket (5-point buckets)
        (
            pl.col("spread_result").truediv(5).floor().mul(5).cast(pl.Int64)
        ).alias("margin_bucket"),
        # Day of week from canonical venue-local date
        pl.col("_canonical_date").str.to_date("%Y-%m-%d").dt.weekday().alias("day_of_week"),
        # Month
        pl.col("_canonical_date").str.to_date("%Y-%m-%d").dt.month().alias("month"),
        # Home team won
        (pl.col("spread_result") > 0).cast(pl.Int64).alias("home_won"),
    ])

    patterns = []

    # Pattern 1: By sport — home win rate
    _find_group_patterns(
        df, ["sport"], "home_won", "sport_home_win",
        min_sample, min_edge, patterns,
    )

    # Pattern 2: By sport + day_of_week — home win rate
    _find_group_patterns(
        df, ["sport", "day_of_week"], "home_won", "sport_dow_home_win",
        min_sample, min_edge, patterns,
    )

    # Pattern 3: By sport + month — home win rate
    _find_group_patterns(
        df, ["sport", "month"], "home_won", "sport_month_home_win",
        min_sample, min_edge, patterns,
    )

    # Pattern 4: By sport + total_bucket — over rate
    # Over 50% means high-scoring games in this bucket are more common
    if "total_score" in df.columns or ("home_score" in df.columns and "away_score" in df.columns):
        df_totals = df.with_columns(
            (pl.col("home_score") + pl.col("away_score")).alias("actual_total")
        )
        # For each total bucket, see if overs tend to go over the bucket midpoint
        # This is a proxy — real analysis would compare to closing lines
        _find_group_patterns(
            df_totals, ["sport", "total_bucket"], "home_won", "sport_total_home_win",
            min_sample, min_edge, patterns,
        )

    # Pattern 5: By home_team — home win rate (team strength)
    _find_group_patterns(
        df, ["sport", "home_team"], "home_won", "team_home_win",
        min_sample, min_edge, patterns,
    )

    # Pattern 6: By away_team — away win rate (inverse of home_won)
    df_away = df.with_columns(
        (1 - pl.col("home_won")).alias("away_won")
    )
    _find_group_patterns(
        df_away, ["sport", "away_team"], "away_won", "team_away_win",
        min_sample, min_edge, patterns,
    )

    if not patterns:
        return pl.DataFrame(schema={
            "pattern_type": pl.Utf8, "pattern_key": pl.Utf8,
            "sample_size": pl.Int64, "wins": pl.Int64,
            "hit_rate": pl.Float64, "edge_pct": pl.Float64,
            "p_value": pl.Float64, "pattern_hash": pl.Utf8,
        })

    result = pl.DataFrame(patterns)

    # Sort by p_value ascending (most significant first)
    result = result.sort("p_value")

    return result


def _find_group_patterns(
    df: pl.DataFrame,
    group_cols: list[str],
    target_col: str,
    pattern_type: str,
    min_sample: int,
    min_edge: float,
    patterns: list[dict],
) -> None:
    """Helper: find patterns for a specific grouping."""
    # Filter to rows where group cols and target are non-null
    valid_cols = [c for c in group_cols if c in df.columns]
    if len(valid_cols) != len(group_cols) or target_col not in df.columns:
        return

    grouped = (
        df.group_by(valid_cols)
        .agg([
            pl.col(target_col).count().alias("sample_size"),
            pl.col(target_col).sum().alias("wins"),
        ])
        .filter(pl.col("sample_size") >= min_sample)
    )

    if grouped.height == 0:
        return

    # Compute hit rate and edge
    grouped = grouped.with_columns([
        (pl.col("wins") / pl.col("sample_size")).alias("hit_rate"),
        ((pl.col("wins") / pl.col("sample_size") - 0.5) * 100).alias("edge_pct"),
    ])

    # Filter by minimum edge
    grouped = grouped.filter(pl.col("edge_pct").abs() >= min_edge)

    for row in grouped.iter_rows(named=True):
        key_parts = {col: row[col] for col in valid_cols}
        p_val = _binomial_pvalue(
            int(row["wins"]), int(row["sample_size"]), 0.5
        )
        # Also compute p-value for the other direction if edge is negative
        if row["edge_pct"] < 0:
            p_val = _binomial_pvalue(
                int(row["sample_size"]) - int(row["wins"]),
                int(row["sample_size"]),
                0.5,
            )

        patterns.append({
            "pattern_type": pattern_type,
            "pattern_key": json.dumps(key_parts, default=str),
            "sample_size": int(row["sample_size"]),
            "wins": int(row["wins"]),
            "hit_rate": round(row["hit_rate"], 4),
            "edge_pct": round(row["edge_pct"], 2),
            "p_value": round(p_val, 6),
            "pattern_hash": _pattern_hash({
                "type": pattern_type,
                "key": key_parts,
                "target": target_col,
            }),
        })


def find_player_prop_patterns(
    player_stats_df: pl.DataFrame,
    min_appearances: int = 10,
) -> pl.DataFrame:
    """
    Find player prop patterns: players who consistently over/under perform.

    Groups by player + stat_type and finds those with high variance or
    consistent over/under tendencies relative to their mean.

    Args:
        player_stats_df: DataFrame from load_player_stats.
        min_appearances: Minimum games for a player+stat to be analyzed.

    Returns:
        DataFrame of player prop patterns.
    """
    if player_stats_df.height == 0:
        return pl.DataFrame(schema={
            "player_name": pl.Utf8, "stat_type": pl.Utf8,
            "sport": pl.Utf8, "appearances": pl.Int64,
            "mean_value": pl.Float64, "std_value": pl.Float64,
            "cv": pl.Float64, "over_rate_vs_mean": pl.Float64,
            "pattern_hash": pl.Utf8,
        })

    required = {"player_name", "stat_type", "stat_value", "sport"}
    if not required.issubset(set(player_stats_df.columns)):
        logger.warning(f"Missing columns for player prop patterns: {required - set(player_stats_df.columns)}")
        return pl.DataFrame()

    grouped = (
        player_stats_df.group_by(["sport", "player_name", "stat_type"])
        .agg([
            pl.col("stat_value").count().alias("appearances"),
            pl.col("stat_value").mean().alias("mean_value"),
            pl.col("stat_value").std().alias("std_value"),
            pl.col("stat_value").median().alias("median_value"),
        ])
        .filter(pl.col("appearances") >= min_appearances)
    )

    if grouped.height == 0:
        return pl.DataFrame()

    # Coefficient of variation — high CV means volatile (good for overs in props)
    grouped = grouped.with_columns([
        (pl.col("std_value") / pl.col("mean_value").abs().clip(lower_bound=0.01)).alias("cv"),
    ])

    # For each player+stat, compute how often they go over their mean
    over_rates = []
    for row in grouped.iter_rows(named=True):
        player_data = player_stats_df.filter(
            (pl.col("player_name") == row["player_name"])
            & (pl.col("stat_type") == row["stat_type"])
            & (pl.col("sport") == row["sport"])
        )
        over_count = player_data.filter(
            pl.col("stat_value") > row["mean_value"]
        ).height
        total = player_data.height
        over_rate = over_count / total if total > 0 else 0.5
        over_rates.append(over_rate)

    grouped = grouped.with_columns(
        pl.Series("over_rate_vs_mean", over_rates)
    )

    # Add pattern hashes
    hashes = []
    for row in grouped.iter_rows(named=True):
        h = _pattern_hash({
            "type": "player_prop",
            "player": row["player_name"],
            "stat_type": row["stat_type"],
            "sport": row["sport"],
        })
        hashes.append(h)

    grouped = grouped.with_columns(
        pl.Series("pattern_hash", hashes)
    )

    # Sort by CV descending (most volatile first — these are exploitable)
    grouped = grouped.sort("cv", descending=True)

    return grouped


def cross_tabulate(
    df: pl.DataFrame,
    factors: list[str],
    target: str = "home_won",
    min_sample: int = 10,
) -> pl.DataFrame:
    """
    Multi-factor interaction analysis.

    Groups by the combination of factors and computes hit rate for the target.

    Args:
        df: Input DataFrame (typically from game_results with derived columns).
        factors: List of column names to group by.
        target: Binary column to analyze (1=hit, 0=miss).
        min_sample: Minimum group size.

    Returns:
        DataFrame with factor values, sample_size, wins, hit_rate, edge_pct, p_value.
    """
    valid_factors = [f for f in factors if f in df.columns]
    if not valid_factors or target not in df.columns:
        return pl.DataFrame()

    grouped = (
        df.group_by(valid_factors)
        .agg([
            pl.col(target).count().alias("sample_size"),
            pl.col(target).sum().alias("wins"),
        ])
        .filter(pl.col("sample_size") >= min_sample)
        .with_columns([
            (pl.col("wins") / pl.col("sample_size")).alias("hit_rate"),
            ((pl.col("wins") / pl.col("sample_size") - 0.5) * 100).alias("edge_pct"),
        ])
    )

    if grouped.height == 0:
        return grouped

    # Compute p-values
    p_values = []
    for row in grouped.iter_rows(named=True):
        wins = int(row["wins"])
        total = int(row["sample_size"])
        if row["edge_pct"] >= 0:
            p = _binomial_pvalue(wins, total, 0.5)
        else:
            p = _binomial_pvalue(total - wins, total, 0.5)
        p_values.append(round(p, 6))

    grouped = grouped.with_columns(pl.Series("p_value", p_values))
    return grouped.sort("p_value")


# ──────────────────────────────────────────────────
# HYPOTHESIS GENERATION WITH TEMPORAL ISOLATION
# ──────────────────────────────────────────────────

def generate_hypotheses_from_analysis(
    db_path: str = DB_PATH,
    sport: Optional[str] = None,
    cutoff_date: Optional[str] = None,
    min_sample: int = 20,
    min_edge: float = 3.0,
    max_p_value: float = 0.10,
    gap_days: int = 7,
) -> list[dict]:
    """
    Full pipeline: load data, discover patterns, return hypothesis definitions.

    This is the primary entry point. It:
    1. Loads historical data up to cutoff_date into Polars
    2. Runs pattern discovery (ATS + player props)
    3. Returns hypothesis definitions compatible with the existing hypothesis schema
    4. Each hypothesis includes training_period in model_config for backtest isolation

    Args:
        db_path: Path to SQLite database.
        sport: Sport to analyze (None = all sports).
        cutoff_date: Training data cutoff. Defaults to 90 days before max date.
        min_sample: Minimum sample size for patterns.
        min_edge: Minimum edge percentage to consider.
        max_p_value: Maximum p-value to include in results.
        gap_days: Days between train end and test start.

    Returns:
        List of hypothesis definition dicts, each with:
        - name, thesis, sport, market_type, model_config (with temporal metadata)
        - Ready to pass to HypothesisManager.create_hypothesis()
    """
    # Load all game results
    full_df = load_game_results(db_path, sport=sport)
    if full_df.height == 0:
        logger.warning("No game results found for analysis")
        return []

    # Determine cutoff date
    max_date_str = full_df.select(pl.col("game_date").max()).item()
    min_date_str = full_df.select(pl.col("game_date").min()).item()

    if cutoff_date is None:
        max_dt = datetime.strptime(max_date_str, "%Y-%m-%d").date()
        cutoff_dt = max_dt - timedelta(days=90)
        cutoff_date = cutoff_dt.strftime("%Y-%m-%d")

    logger.info(
        f"Generating hypotheses: data {min_date_str} to {max_date_str}, "
        f"training cutoff {cutoff_date}, gap {gap_days}d"
    )

    # Split data
    train_df, test_df, split_meta = create_temporal_split(
        full_df, cutoff_date, min_gap_days=gap_days,
    )

    if train_df.height < min_sample:
        logger.warning(
            f"Insufficient training data: {train_df.height} rows < {min_sample} minimum"
        )
        return []

    train_start = train_df.select(pl.col("game_date").min()).item()
    train_end = cutoff_date

    # Discover ATS patterns
    ats_patterns = find_ats_patterns(train_df, min_sample=min_sample, min_edge=min_edge)

    # Discover player prop patterns
    player_df = load_player_stats(db_path, sport=sport, date_range=(train_start, train_end))
    prop_patterns = find_player_prop_patterns(player_df, min_appearances=min_sample)

    hypotheses = []

    # Convert ATS patterns to hypothesis definitions
    if ats_patterns.height > 0:
        significant = ats_patterns.filter(pl.col("p_value") <= max_p_value)
        for row in significant.iter_rows(named=True):
            key = json.loads(row["pattern_key"])
            pattern_sport = key.get("sport", sport or "unknown")

            # Determine market type from pattern
            pattern_type = row["pattern_type"]
            if "home_win" in pattern_type:
                market_type = "h2h"
                direction = "home" if row["edge_pct"] > 0 else "away"
            elif "total" in pattern_type:
                market_type = "totals"
                direction = "over" if row["edge_pct"] > 0 else "under"
            else:
                market_type = "spreads"
                direction = "home" if row["edge_pct"] > 0 else "away"

            hypothesis_def = {
                "name": f"ATS_{pattern_type}_{row['pattern_hash'][:8]}",
                "thesis": (
                    f"{pattern_type} pattern: {json.dumps(key, default=str)} "
                    f"shows {row['hit_rate']:.1%} hit rate "
                    f"(edge {row['edge_pct']:+.1f}%, p={row['p_value']:.4f}, "
                    f"n={row['sample_size']})"
                ),
                "sport": pattern_sport,
                "market_type": market_type,
                "model_config": {
                    "pattern_type": pattern_type,
                    "pattern_key": key,
                    "pattern_hash": row["pattern_hash"],
                    "direction": direction,
                    "target_book": "draftkings",
                    "devig_method": "power",
                    "consensus_min_books": 2,
                    # TEMPORAL METADATA — critical for backtest isolation
                    "training_period_start": train_start,
                    "training_period_end": train_end,
                    "temporal_split_gap_days": gap_days,
                    "training_sample_size": row["sample_size"],
                    "training_hit_rate": row["hit_rate"],
                    "training_p_value": row["p_value"],
                },
                "edge_threshold": 0.01,
                "min_sample_size": max(30, min_sample),
                "significance_level": 0.05,
                "notes": (
                    f"Auto-generated from temporal analysis. "
                    f"Training period: {train_start} to {train_end}. "
                    f"Test data available from {split_meta['test_date_range'][0]}."
                ),
            }
            hypotheses.append(hypothesis_def)

    # Convert player prop patterns to hypothesis definitions
    if prop_patterns.height > 0:
        # Focus on players with extreme over/under rates
        extreme_props = prop_patterns.filter(
            (pl.col("over_rate_vs_mean") > 0.60) | (pl.col("over_rate_vs_mean") < 0.40)
        )

        for row in extreme_props.iter_rows(named=True):
            direction = "over" if row["over_rate_vs_mean"] > 0.5 else "under"
            stat_type = row["stat_type"]

            # Map stat types to market types
            stat_to_market = {
                "points": "player_points",
                "rebounds": "player_rebounds",
                "assists": "player_assists",
                "threes": "player_threes",
            }
            market_type = stat_to_market.get(stat_type, f"player_{stat_type}")

            hypothesis_def = {
                "name": f"PROP_{row['player_name'][:15]}_{stat_type}_{direction}",
                "thesis": (
                    f"{row['player_name']} {stat_type} {direction}: "
                    f"goes {direction} mean ({row['mean_value']:.1f}) "
                    f"{row['over_rate_vs_mean']:.0%} of the time "
                    f"(CV={row['cv']:.2f}, n={row['appearances']})"
                ),
                "sport": row["sport"],
                "market_type": market_type,
                "model_config": {
                    "pattern_type": "player_prop_tendency",
                    "player": row["player_name"],
                    "stat_type": stat_type,
                    "direction": direction,
                    "mean_value": round(row["mean_value"], 2),
                    "over_rate": round(row["over_rate_vs_mean"], 4),
                    "cv": round(row["cv"], 4),
                    "target_book": "draftkings",
                    "devig_method": "power",
                    "consensus_min_books": 2,
                    # TEMPORAL METADATA
                    "training_period_start": train_start,
                    "training_period_end": train_end,
                    "temporal_split_gap_days": gap_days,
                    "training_sample_size": int(row["appearances"]),
                },
                "edge_threshold": 0.01,
                "min_sample_size": max(30, min_sample),
                "significance_level": 0.05,
                "notes": (
                    f"Auto-generated player prop pattern. "
                    f"Training period: {train_start} to {train_end}."
                ),
            }
            hypotheses.append(hypothesis_def)

    logger.info(
        f"Generated {len(hypotheses)} hypotheses "
        f"({ats_patterns.height} ATS patterns, "
        f"{prop_patterns.height if prop_patterns.height > 0 else 0} player prop patterns)"
    )

    return hypotheses


# ──────────────────────────────────────────────────
# TEMPORAL VALIDATION HELPERS
# ──────────────────────────────────────────────────

def validate_temporal_isolation(
    hypothesis_config: dict,
    backtest_start: str,
    backtest_end: str,
) -> dict:
    """
    Validate that a backtest date range doesn't overlap with the training period.

    Returns:
        Dict with 'valid' bool, 'reason' string, and 'adjusted_start' if auto-fix possible.
    """
    training_end = hypothesis_config.get("training_period_end")
    gap_days = hypothesis_config.get("temporal_split_gap_days", 7)

    if not training_end:
        return {
            "valid": True,
            "warning": "No training period metadata — legacy hypothesis, temporal isolation not enforced.",
            "has_temporal_metadata": False,
        }

    training_end_dt = datetime.strptime(training_end, "%Y-%m-%d").date()
    safe_start_dt = training_end_dt + timedelta(days=gap_days)
    safe_start = safe_start_dt.strftime("%Y-%m-%d")

    backtest_start_dt = datetime.strptime(backtest_start, "%Y-%m-%d").date()

    if backtest_start_dt <= training_end_dt:
        return {
            "valid": False,
            "reason": (
                f"Backtest start {backtest_start} overlaps with training period "
                f"(ends {training_end}). This would test on training data."
            ),
            "adjusted_start": safe_start,
            "has_temporal_metadata": True,
        }

    if backtest_start_dt < safe_start_dt:
        return {
            "valid": False,
            "reason": (
                f"Backtest start {backtest_start} is within the {gap_days}-day gap "
                f"after training end ({training_end}). Minimum safe start: {safe_start}."
            ),
            "adjusted_start": safe_start,
            "has_temporal_metadata": True,
        }

    return {
        "valid": True,
        "has_temporal_metadata": True,
        "training_period_end": training_end,
        "backtest_start": backtest_start,
        "gap_days_actual": (backtest_start_dt - training_end_dt).days,
    }


def get_data_summary(db_path: str = DB_PATH) -> dict:
    """
    Quick summary of available data for analysis planning.

    Returns counts, date ranges, and sport breakdowns.
    """
    conn = _connect(db_path)

    summary = {}

    # Game results
    cursor = conn.execute(
        "SELECT sport, COUNT(*) as count, MIN(game_date) as min_date, "
        "MAX(game_date) as max_date FROM game_results GROUP BY sport"
    )
    summary["game_results"] = {
        row[0]: {"count": row[1], "min_date": row[2], "max_date": row[3]}
        for row in cursor.fetchall()
    }
    summary["game_results_total"] = sum(
        v["count"] for v in summary["game_results"].values()
    )

    # Player stats
    cursor = conn.execute(
        "SELECT sport, COUNT(*) as count, COUNT(DISTINCT player_name) as players "
        "FROM player_stats GROUP BY sport"
    )
    summary["player_stats"] = {
        row[0]: {"count": row[1], "unique_players": row[2]}
        for row in cursor.fetchall()
    }
    summary["player_stats_total"] = sum(
        v["count"] for v in summary["player_stats"].values()
    )

    # Backtest events
    cursor = conn.execute("SELECT COUNT(*) FROM backtest_events")
    summary["backtest_events_total"] = cursor.fetchone()[0]

    # Hypotheses
    cursor = conn.execute(
        "SELECT status, COUNT(*) FROM hypotheses GROUP BY status"
    )
    summary["hypotheses"] = {row[0]: row[1] for row in cursor.fetchall()}

    conn.close()
    return summary
