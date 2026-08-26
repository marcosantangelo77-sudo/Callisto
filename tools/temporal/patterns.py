"""Pattern discovery: ATS group patterns, player props, cross-tabulation."""

import hashlib
import json
import logging
import math

import polars as pl

from tools.temporal.stats import _binomial_pvalue

logger = logging.getLogger("callisto.temporal_analysis")

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
            "p_value": pl.Float64,
            "p_value_adj": pl.Float64,
            "n_tests": pl.Int64,
            "pattern_hash": pl.Utf8,
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
            "p_value": pl.Float64,
            "p_value_adj": pl.Float64,
            "n_tests": pl.Int64,
            "pattern_hash": pl.Utf8,
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

    # Apply Bonferroni across the entire group-by grid (≥6 parametric tests).
    # Discovery phase p-hacking: report both raw and adjusted p so downstream
    # hypothesis generation uses the corrected value as its significance gate.
    _bonferroni_finalize(patterns)

    if not patterns:
        return pl.DataFrame(schema={
            "pattern_type": pl.Utf8, "pattern_key": pl.Utf8,
            "sample_size": pl.Int64, "wins": pl.Int64,
            "hit_rate": pl.Float64, "edge_pct": pl.Float64,
            "p_value": pl.Float64,
            "p_value_adj": pl.Float64,
            "n_tests": pl.Int64,
            "pattern_hash": pl.Utf8,
        })

    result = pl.DataFrame(patterns)

    # Sort by adjusted p ascending (most significant after correction first)
    result = result.sort("p_value_adj")

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
    """Helper: find patterns for a specific grouping.

    Each row returned carries BOTH the raw binomial p-value (``p_value``)
    and an unfinalized Bonferroni-adjusted p (``p_value_adj_raw = p*k``).
    The caller (``find_ats_patterns``) does the final correction across
    the entire group-by grid (see ``_bonferroni_finalize``) since only
    the caller knows the total number of tests performed.
    """
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
            # p_value_adj filled in by caller once total-tests k is known
            "p_value_adj": None,
            "pattern_hash": _pattern_hash({
                "type": pattern_type,
                "key": key_parts,
                "target": target_col,
            }),
        })


def _bonferroni_finalize(patterns: list[dict]) -> None:
    """Apply Bonferroni correction across all tests performed in the grid.

    Discovery-phase p-hacking: ≥6 parametric group-bys across sport×dow,
    sport×month, sport×total_bucket, teams, etc. each test gets its own
    hypothesis, so the family-wise rate scales linearly with k.  We report
    both raw (``p_value``) and adjusted (``p_value_adj = min(1, p*k)``)
    so downstream hypothesis generation can use the corrected value as
    its gate.
    """
    k = len(patterns)
    if k <= 1:
        for p in patterns:
            p["p_value_adj"] = p["p_value"]
            p["n_tests"] = k
        return
    for p in patterns:
        raw = float(p.get("p_value") or 1.0)
        p["p_value_adj"] = round(min(1.0, raw * k), 6)
        p["n_tests"] = k


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

