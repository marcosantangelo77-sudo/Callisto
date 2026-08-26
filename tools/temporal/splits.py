"""Temporal split enforcement: train/test splits with leakage-prevention gaps."""

import logging
from datetime import datetime, timedelta

import polars as pl

logger = logging.getLogger("callisto.temporal_analysis")

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

