"""Temporal isolation validation helpers."""

from datetime import datetime, timedelta


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
