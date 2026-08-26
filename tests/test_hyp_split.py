"""
tests/test_hyp_split.py — pins for the tools/hypothesis package split.

The former single-module tools/hypothesis.py is now a facade over
tools/hypothesis/ (config, stats, significance, promote, store, sharpening,
manager). These tests pin the required public surface and — critically —
the auto_promote DIAGNOSE-ONLY behavior w.r.t. edge_threshold and
signal_generated.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "tools" / "hypothesis"


def _pkg_source() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(PKG.glob("*.py"))
    )


# ── public API ────────────────────────────────────────────────────────────────


def test_facade_import_hypothesis_manager():
    from tools.hypothesis import HypothesisManager  # noqa: F401


def test_stage_order_unchanged():
    from tools.hypothesis import STAGE_ORDER
    assert list(STAGE_ORDER) == [
        "draft", "backtesting", "paper_trading", "live", "retired",
    ]


def test_promotion_gates_and_helpers_reexported():
    from tools.hypothesis import (
        PROMOTION_GATES,
        validate_model_config,
        binomial_pvalue,
        ttest_one_sample,
        z_score,
        sharpe_ratio,
        max_drawdown,
        calibration_bins,
    )
    assert "backtesting→paper_trading" in PROMOTION_GATES
    assert "paper_trading→live" in PROMOTION_GATES


def test_manager_assembled_from_mixins():
    from tools.hypothesis.manager import HypothesisManager
    from tools.hypothesis.store import HypothesisStoreMixin
    from tools.hypothesis.significance import HypothesisSignificanceMixin
    from tools.hypothesis.promote import HypothesisPromotionMixin
    assert issubclass(HypothesisManager, (
        HypothesisStoreMixin,
        HypothesisSignificanceMixin,
        HypothesisPromotionMixin,
    ))


def test_facade_is_materially_shrunk():
    """tools/hypothesis.py must now be a mostly-imports/re-exports facade."""
    facade = (REPO / "tools" / "hypothesis.py").read_text(encoding="utf-8")
    pkg_src = _pkg_source()
    assert len(pkg_src) > len(facade) * 3, (
        "package should hold the bulk of the former module"
    )


# ── auto_promote diagnose-only pin (source level) ─────────────────────────────


def test_auto_promote_never_writes_edge_threshold_or_signal_generated():
    """auto_promote may LOG a threshold diagnosis and HOLD, but must never
    write to hypotheses.edge_threshold or paper_trades.signal_generated."""
    promote_src = (PKG / "promote.py").read_text(encoding="utf-8")
    start = promote_src.index("async def auto_promote")
    end = promote_src.index("\n    async def ", start + 10)
    body = promote_src[start:end]

    # No UPDATE/SET touching edge_threshold or signal_generated anywhere in
    # auto_promote. The only UPDATEs permitted are model_config eval_cycles.
    assert "edge_threshold =" not in body.replace("edge_threshold ==", "")
    assert "SET edge_threshold" not in body
    assert "SET signal_generated" not in body
    assert "UPDATE paper_trades" not in body
    # every UPDATE in the body must target model_config only
    for line in body.splitlines():
        if "UPDATE" in line:
            assert "model_config = ?" in line or "SET status" in line, line


def test_diagnose_helper_is_read_only():
    src = (PKG / "promote.py").read_text(encoding="utf-8")
    start = src.index("async def _diagnose_edge_threshold")
    end = src.index("\n    async def ", start + 10)
    body = src[start:end]
    assert "UPDATE" not in body
    assert "INSERT" not in body
    assert "threshold_too_high" in body
    # it recommends, it does not apply
    assert "recommended_threshold" in body
