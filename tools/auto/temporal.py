"""Training/backtest temporal-overlap check extracted from ResearchLoop.

``ResearchLoop._check_temporal_overlap`` stays on the facade as a thin
wrapper so existing tests that call the classmethod keep working.

Does not import ``tools.autonomous``. Does not arm live betting.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional


def check_temporal_overlap(model_config: dict) -> Optional[str]:
    """Check if training and backtest periods overlap.

    Returns an error message or None. A string ``model_config`` is parsed
    as JSON; garbage input is a no-op (None), matching the historical
    ResearchLoop helper.
    """
    if isinstance(model_config, str):
        try:
            model_config = json.loads(model_config)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(model_config, dict):
        return None

    training_end = model_config.get("training_period_end")
    backtest_start = model_config.get("backtest_period_start")

    if not training_end or not backtest_start:
        return None  # Can't check without both dates

    try:
        te = datetime.strptime(str(training_end), "%Y-%m-%d").date()
        bs = datetime.strptime(str(backtest_start), "%Y-%m-%d").date()
        if bs <= te:
            return (
                f"TEMPORAL OVERLAP: backtest starts {bs} but training ends {te}. "
                f"Backtest results are contaminated by training data."
            )
    except ValueError:
        pass

    return None
