"""Hypothesis generation with temporal isolation + validation helpers."""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import polars as pl
from dotenv import load_dotenv

from tools.temporal.loading import (
    DB_PATH,
    _connect,
    load_game_results,
    load_player_stats,
)
from tools.temporal.patterns import find_ats_patterns, find_player_prop_patterns
from tools.temporal.splits import create_temporal_split

load_dotenv()

logger = logging.getLogger("callisto.temporal_analysis")

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
