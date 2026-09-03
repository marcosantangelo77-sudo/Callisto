"""
tools.hypothesis.significance — statistical evaluation and promotion-readiness.

Split out of tools/hypothesis.py (facade re-exports everything).
``evaluate_significance`` body lives in ``significance_eval``;
``check_promotion_readiness`` body lives in ``significance_ready``.
This mixin keeps thin delegates so ``hasattr`` pins keep passing.

Live promotion readiness stays on ``HypothesisSignificanceMixin`` only —
do not copy ``check_promotion_readiness`` onto ``HypothesisPromotionMixin``.
"""
from __future__ import annotations


class HypothesisSignificanceMixin:
    async def evaluate_significance(
        self, hypothesis_id: str, stage: str = "backtest",
    ) -> dict:
        """
        Run all statistical tests on a hypothesis at a given stage.
        Returns comprehensive significance report.
        """
        from tools.hypothesis.significance_eval import evaluate_significance as _evaluate_significance
        return await _evaluate_significance(self, hypothesis_id, stage)

    async def check_promotion_readiness(
        self,
        hypothesis_id: str,
        *,
        stage_override: str | None = None,
        status_override: str | None = None,
    ) -> dict:
        """Check if a hypothesis meets criteria to advance to next stage.

        Args:
            stage_override: Force evaluation on a different data stage.
                Used by auto_promote when paper_trading hypotheses have
                0 paper trades but sufficient backtest evidence — without
                this, the readiness check would evaluate on empty
                paper_trade data and always fail (the deadlock bug).
            status_override: Evaluate the gate as if the hypothesis were
                in ``status_override`` instead of its real status.  Added
                2026-04-22 for the LIVE-cascade migration script: LIVE
                rows were grandfathered past the new paper→live gates,
                and we need to re-run the exact paper→live gate against
                the current state without flipping the row first.  Does
                not mutate the DB; only affects this evaluation.
        """
        from tools.hypothesis.significance_ready import (
            check_promotion_readiness as _check_promotion_readiness,
        )
        return await _check_promotion_readiness(
            self,
            hypothesis_id,
            stage_override=stage_override,
            status_override=status_override,
        )
