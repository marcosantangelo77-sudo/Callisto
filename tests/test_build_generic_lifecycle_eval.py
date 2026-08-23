"""A domain-general claim can now flow through the lifecycle.

BUILD_MANDATE item 1 promised OutcomeResolver would let "any resolvable
claim enter the lifecycle". Until this pass the resolver read tables that
did not exist and nothing fed its evidence into evaluation or promotion.
These tests drive a REAL general claim — created post-seam, predictions
recorded, outcomes resolved — through evaluate_significance(stage='generic')
and check_promotion_readiness against a real migrated database, and pin the
adapter's honesty rules.
"""

import os

import pytest

from tools.hypothesis import HypothesisManager
from tools.resolvers.base import (
    OUTCOME_INDETERMINATE,
    OUTCOME_NEGATIVE,
    OUTCOME_POSITIVE,
    EvidenceRecord,
    evidence_records_to_eval_rows,
)
from tools.resolvers.generic import record_outcome, record_prediction


async def _migrated_manager(tmpdir: str):
    """Real migrated DB + manager + one general claim."""
    from tools.migrations import apply_pending_migrations
    from tools.schema import ensure_schema

    db_path = os.path.join(tmpdir, "lifecycle.db")
    await ensure_schema(db_path)
    apply_pending_migrations(db_path)
    mgr = HypothesisManager(db_path=db_path)
    await mgr.initialize()
    hid = await mgr.create_hypothesis(
        name="btc_halving_drift",
        thesis="BTC drifts positive in the 90 days after a halving",
        model_config={},
        notes="domain-general",
    )
    return mgr, hid


async def _record_and_resolve(mgr, hid, instances):
    """instances: list of (event_id, predicted, implied, outcome_token|None)."""
    for event_id, pred, implied, token in instances:
        pid = await record_prediction(
            mgr._db, claim_id=hid, event_id=event_id,
            predicted_prob=pred, book_implied_prob=implied,
            context_key=event_id.split("_")[0],
        )
        if token is not None:
            await record_outcome(mgr._db, prediction_id=pid,
                                 resolved_outcome=token)


# ── adapter honesty rules ──────────────────────────────────────────────────

def test_adapter_maps_records_onto_betting_rows():
    recs = [
        EvidenceRecord(event_id="a", predicted_prob=0.70,
                       book_implied_prob=0.50, odds_american=-100,
                       clv_prob_bp=-40, resolved_outcome=OUTCOME_POSITIVE),
        EvidenceRecord(event_id="b", predicted_prob=None,
                       resolved_outcome=OUTCOME_NEGATIVE),
        EvidenceRecord(event_id="c", predicted_prob=0.30,
                       resolved_outcome=OUTCOME_INDETERMINATE),
    ]
    rows = evidence_records_to_eval_rows(recs)
    a, b, c = rows
    assert a["actual_result"] == "won"
    assert a["model_fair_prob"] == 0.70
    # edge is the honest spread between claim and market
    assert a["edge"] == pytest.approx(0.20)
    # clv_implied converts bp -> rate scale
    assert a["clv_implied"] == pytest.approx(-0.004)
    # ev per $1 at -100: p*dec-1 = .70*2-1 = 40%
    assert a["ev_pct"] == pytest.approx(40.0)
    # record b has neither prob nor market: no fabricated numbers
    assert b["actual_result"] == "lost"
    assert "edge" not in b and "model_fair_prob" not in b
    assert c["actual_result"] == "push"


def test_adapter_never_fabricates_edge_without_market():
    recs = [EvidenceRecord(event_id="x", predicted_prob=0.9,
                           resolved_outcome=OUTCOME_POSITIVE)]
    row = evidence_records_to_eval_rows(recs)[0]
    assert "edge" not in row and "ev_pct" not in row


# ── evaluation through the resolver seam ───────────────────────────────────

@pytest.mark.asyncio
async def test_generic_stage_scores_recorded_evidence(tmp_path):
    mgr, hid = await _migrated_manager(str(tmp_path))
    try:
        await _record_and_resolve(mgr, hid, [
            ("bull_q1", 0.70, 0.50, "yes"),
            ("bull_q2", 0.65, 0.50, "yes"),
            ("bear_q1", 0.60, 0.55, "no"),
            ("flat_q1", 0.50, 0.50, None),          # unresolved
            ("void_q1", 0.50, 0.50, "void"),         # indeterminate
        ])
        report = await mgr.evaluate_significance(hid, stage="generic")
        assert report["stage"] == "generic"
        assert report["sample_size"] == 4       # 3 decided + 1 push
        assert report["unresolved"] == 1
        res = report["results"]
        assert res["wins"] == 2 and res["losses"] == 1 and res["pushes"] == 1
        assert res["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)
        # expected rate = mean market-implied prob over ALL rows carrying one
        # (including unresolved/indeterminate), matching the betting path
        assert res["expected_rate"] == pytest.approx(0.51, abs=1e-3)
        # Brier only over decided rows with a probability
        cal = report["calibration_score"]
        exp_brier = ((1 - 0.70) ** 2 + (0 - 0.65) ** 2 + (0 - 0.60) ** 2) / 3
        assert cal["brier_score"] == pytest.approx(exp_brier, abs=1e-4)
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_generic_stage_empty_is_honest_zero(tmp_path):
    mgr, hid = await _migrated_manager(str(tmp_path))
    try:
        report = await mgr.evaluate_significance(hid, stage="generic")
        assert report["sample_size"] == 0
        assert report["is_significant"] is False
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_sports_stage_still_reads_sports_tables(tmp_path):
    """Regression: the generic seam must not disturb the betting path."""
    from tools.migrations import apply_pending_migrations
    from tools.schema import ensure_schema

    db_path = os.path.join(str(tmp_path), "sports.db")
    await ensure_schema(db_path)
    apply_pending_migrations(db_path)
    mgr = HypothesisManager(db_path=db_path)
    await mgr.initialize()
    try:
        hid = await mgr.create_hypothesis(
            name="nba_system", thesis="t", sport="nba",
            market_type="moneyline", model_config={})
        await mgr._db.execute(
            "INSERT INTO paper_trades (hypothesis_id, actual_result, sport, market) "
            "VALUES (?, 'won', 'nba', 'moneyline')", (hid,))
        await mgr._db.commit()
        report = await mgr.evaluate_significance(hid, stage="paper_trade")
        assert report.get("results", {}).get("wins") == 1
    finally:
        await mgr.close()


# ── promotion readiness through the lifecycle ──────────────────────────────

@pytest.mark.asyncio
async def test_general_claim_readiness_draft_to_backtesting(tmp_path):
    mgr, hid = await _migrated_manager(str(tmp_path))
    try:
        r = await mgr.check_promotion_readiness(hid)
        assert r["ready"] is True
        assert r["next_stage"] == "backtesting"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_general_claim_insufficient_sample_blocks_promotion(tmp_path):
    mgr, hid = await _migrated_manager(str(tmp_path))
    try:
        await _record_and_resolve(mgr, hid, [
            ("only_one", 0.70, 0.50, "yes"),
        ])
        await mgr.update_status(hid, "backtesting")
        r = await mgr.check_promotion_readiness(hid)
        assert r["ready"] is False
        joined = "\n".join(r["checks"])
        assert "1/5 signals" in joined
        assert "signal-edge distribution N/A for generic evidence" in joined
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_general_claim_promotes_to_forward_testing(tmp_path):
    """The end-to-end proof: five clean forward-tests carry a general claim
    through backtesting→paper_trading with every gate honestly applied."""
    mgr, hid = await _migrated_manager(str(tmp_path))
    try:
        await _record_and_resolve(mgr, hid, [
            ("ctxA_1", 0.72, 0.50, "yes"),
            ("ctxB_2", 0.68, 0.50, "yes"),
            ("ctxC_3", 0.71, 0.52, "yes"),
            ("ctxD_4", 0.66, 0.51, "yes"),
            ("ctxE_5", 0.70, 0.49, "yes"),
        ])
        await mgr.update_status(hid, "backtesting")
        r = await mgr.check_promotion_readiness(hid)
        assert r["next_stage"] == "paper_trading"
        joined = "\n".join(r["checks"])
        assert "5/5 signals" in joined
        assert any(c.startswith("PASS") for c in r["checks"])
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_live_gate_counts_resolved_forward_tests_not_paper_trades(tmp_path):
    """The paper→live hard gate reads the claim's own resolution record for
    general claims — never the sports table."""
    mgr, hid = await _migrated_manager(str(tmp_path))
    try:
        await _record_and_resolve(mgr, hid, [
            ("ctxA_1", 0.72, 0.50, "yes"),
            ("ctxA_2", 0.68, 0.50, "yes"),   # same context as ctxA_1
        ])
        await mgr.update_status(hid, "paper_trading")
        r = await mgr.check_promotion_readiness(hid)
        joined = "\n".join(r["checks"])
        assert "resolved forward-tests" in joined
        assert "2/10 resolved forward-tests" in joined
        assert r["ready"] is False
        assert "single_context_sample" in joined
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_general_claim_reaches_live_with_clean_record(tmp_path):
    """The full proof: 10 resolved forward-tests across two contexts, all
    positive CLV, time served — a general claim earns 'live' through the
    same gates a sports hypothesis faces."""
    from datetime import datetime, timedelta, timezone

    mgr, hid = await _migrated_manager(str(tmp_path))
    try:
        instances = []
        for i in range(1, 13):
            ctx = f"ctx{chr(ord('A') + i % 2)}"   # two alternating contexts
            instances.append((f"{ctx}_{i}", 0.70, 0.50, "yes"))
        for event_id, pred, implied, token in instances:
            pid = await record_prediction(
                mgr._db, claim_id=hid, event_id=event_id,
                predicted_prob=pred, book_implied_prob=implied,
                clv_prob_bp=60.0,                     # +0.6% devigged close
                context_key=ctx_key(event_id),
            )
            await record_outcome(mgr._db, prediction_id=pid,
                                 resolved_outcome=token)
        await mgr.update_status(hid, "paper_trading")
        back = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        await mgr._db.execute(
            "UPDATE hypotheses SET promoted_at = ? WHERE hypothesis_id = ?",
            (back, hid))
        await mgr._db.commit()

        r = await mgr.check_promotion_readiness(hid)
        assert r["ready"] is True, r["checks"]
        assert r["next_stage"] == "live"
        joined = "\n".join(r["checks"])
        assert "12/10 resolved forward-tests" in joined
        assert "PASS: CLV positive-rate" in joined
        assert "PASS: context_diversity" in joined
        assert "portfolio_correlation N/A for general claims" in joined
        assert "pre-live simulation N/A for general claims" in joined
    finally:
        await mgr.close()


def ctx_key(event_id: str) -> str:
    return event_id.split("_")[0]
