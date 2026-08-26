"""Source-contract test: BetExecutor.compute_stake imports canonical Kelly only."""

import inspect

from tools.bet_executor import BetExecutor


def _compute_stake_source() -> str:
    return inspect.getsource(BetExecutor.compute_stake)


def test_compute_stake_does_not_import_kelly_full_from_sizing():
    src = _compute_stake_source()
    assert "from tools.sizing import" not in src or not any(
        name in src.split("from tools.sizing import")[1].split("\n")[0]
        for name in ("kelly_full", "kelly_fractional", "kelly_dynamic")
    ), "compute_stake must not import kelly_full/kelly_fractional/kelly_dynamic from tools.sizing"
    assert "kelly_full" not in src.replace(
        "from tools.kelly import", ""
    ) or "tools.sizing" not in src, "no kelly_full import from sizing"


def test_compute_stake_mentions_canonical_tools_kelly():
    src = _compute_stake_source()
    assert "from tools.kelly import" in src
    assert "tools.kelly" in src


def test_push_aware_helpers_may_come_from_sizing():
    # push-aware helpers (kelly_with_push, uncertainty_adjusted_kelly) are
    # allowed to remain imported from tools.sizing.
    src = _compute_stake_source()
    assert "from tools.sizing import kelly_with_push, uncertainty_adjusted_kelly" in src
