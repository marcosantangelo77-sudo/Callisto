"""Derived-analysis loop tests — extraction → relationship → deviation → QUESTION.

Hard rules under test:
  1. An anomaly renders ONLY as a question; it carries no confidence field.
  2. The normal range comes from the entity's OWN history, not a constant.
  3. Emission is bounded (MAX_QUESTIONS_PER_EXTRACTION) and deterministic.
  4. Emission goes through the existing pipeline (TaskQueue.submit_task).
"""
from __future__ import annotations

import asyncio

import pytest

from tools.derived_analysis import (
    MAX_QUESTIONS_PER_EXTRACTION,
    Anomaly,
    Relationship,
    detect_anomalies,
    emit_questions,
    select_for_emission,
)
from tools.domains.finance.derived_analysis import RELATIONSHIPS, statements_series
from tools.domains.finance.statements import assemble_statements

import sys
sys.path.insert(0, "tests")
from test_build_b6_edgar_finance import make_facts  # noqa: E402


# ── generic engine ────────────────────────────────────────────────────────

def _rel(values: dict[str, float]) -> Relationship:
    return Relationship(key="t", description="test", unit="ratio",
                        compute=lambda s: dict(values))


class TestNormalRangeFromOwnHistory:
    def test_stable_history_then_deviation_flags(self):
        obs = {"FY1": 1.0, "FY2": 1.05, "FY3": 0.98, "FY4": 3.0}
        got = detect_anomalies([_rel(obs)], {}, entity="X",
                               focus=["FY4"])
        assert len(got) == 1
        assert got[0].period == "FY4"

    def test_thin_history_stays_silent(self):
        # 2 baseline periods < MIN_PERIODS_FOR_RANGE: no range, no question.
        assert detect_anomalies([_rel({"FY1": 1.0, "FY2": 9.9})], {},
                                entity="X") == []

    def test_in_band_focus_period_not_flagged(self):
        obs = {"FY1": 1.0, "FY2": 1.02, "FY3": 0.99, "FY4": 1.01}
        assert detect_anomalies([_rel(obs)], {}, entity="X",
                                focus=["FY4"]) == []


class TestAnomalyIsAQuestion:
    def test_no_confidence_field_exists(self):
        a = Anomaly(relationship_key="k", entity="E", period="P",
                    observed=5.0, expected_low=1.0, expected_high=1.2,
                    history_periods=("A", "B", "C"), unit="ratio",
                    description="d")
        d = a.evidence()
        assert "confidence" not in d
        assert not any("conf" in f for f in a.__dataclass_fields__)

    def test_question_is_interrogative_and_carries_evidence(self):
        a = Anomaly(relationship_key="accruals_share", entity="TESTCO INC",
                    period="FY2023", observed=5.0, expected_low=0.6,
                    expected_high=1.4, history_periods=("FY2020", "FY2021",
                                                        "FY2022"),
                    unit="ratio", description="NI vs CFO",
                    center=1.0, sigma=0.133)
        q = a.question()
        assert q.startswith("Investigate:")
        assert "FY2023" in q and "TESTCO" in q and "[0.6, 1.4]" in q
        assert "robust sigmas" in q


class TestBoundedEmission:
    def _many(self, n) -> list[Anomaly]:
        out = []
        for i in range(n):
            out.append(Anomaly(
                relationship_key=f"k{i}", entity="E", period=f"P{i}",
                observed=10.0 + i, expected_low=1.0, expected_high=2.0,
                history_periods=("A", "B", "C"), unit="ratio",
                description="d"))
        return out

    def test_bound_enforced(self):
        sel, dropped = select_for_emission(self._many(12))
        assert len(sel) == MAX_QUESTIONS_PER_EXTRACTION == 5
        assert dropped == 7

    def test_deterministic_largest_magnitude_first(self):
        anomalies = self._many(8)
        sel, _ = select_for_emission(anomalies)
        mags = [a.magnitude for a in sel]
        assert mags == sorted(mags, reverse=True)

    def test_emit_submits_via_queue(self):
        class FakeQueue:
            def __init__(self):
                self.queries = []

            async def submit_task(self, query, priority=0):
                self.queries.append((query, priority))
                return len(self.queries)

        fq = FakeQueue()
        report = asyncio.run(emit_questions(self._many(7), fq))
        assert len(fq.queries) == 5
        assert report["submitted"] == 5
        assert report["dropped_over_bound"] == 2
        assert all(q[0].startswith("Investigate:") for q in fq.queries)


# ── finance instantiation on real assembled statements ────────────────────

def _stmt(n=5):
    s = assemble_statements(make_facts(), n_periods=n)
    s.ticker = "TST"
    return s


class TestFinanceRelationships:
    def test_relationships_compute_from_assembled_matrix(self):
        series = statements_series(_stmt())
        for rel in RELATIONSHIPS:
            obs = rel.observe(series)
            assert isinstance(obs, dict)
        # gross margin FY2023 = 55/100
        gm = next(r for r in RELATIONSHIPS if r.key == "gross_margin")
        assert abs(gm.observe(series)["FY2023"] - 0.55) < 1e-9

    def test_missing_inputs_yield_silence_not_crash(self):
        # current_ratio needs balance lines; fixture has one balance date only,
        # so the focus FY has no observation → nothing flagged, nothing raised.
        anomalies = detect_anomalies(RELATIONSHIPS, statements_series(_stmt()),
                                     entity="TESTCO INC")
        assert all(a.relationship_key != "current_ratio" for a in anomalies)


class TestWorkedExampleOnFixture:
    def test_deviation_produces_question_with_magnitude(self):
        """Inject an accruals break: FY2023 net income huge, CFO unchanged."""
        facts = make_facts()
        usgaap = facts["facts"]["us-gaap"]

        def _add(tag, start, end, val, *, accn="0008-23", filed="2023-08-01"):
            usgaap[tag]["units"]["USD"].append(
                {"start": start, "end": end, "val": val,
                 "accn": accn, "fy": 2026, "fp": "FY",
                 "form": "10-K", "filed": filed})

        # Real FY2020+FY2021 columns so the entity's own history has a
        # 3-period baseline + the focus period (MIN_PERIODS_FOR_RANGE = 3,
        # and the focus period itself is excluded from its own baseline).
        _add("RevenueFromContractWithCustomerExcludingAssessedTax",
             "2019-07-01", "2020-06-30", 60e9)
        _add("CostOfRevenue", "2019-07-01", "2020-06-30", 30e9)
        _add("NetIncomeLoss", "2019-07-01", "2020-06-30", 8e9)
        _add("NetCashProvidedByUsedInOperatingActivities",
             "2019-07-01", "2020-06-30", 20e9)
        _add("RevenueFromContractWithCustomerExcludingAssessedTax",
             "2020-07-01", "2021-06-30", 70e9)
        _add("CostOfRevenue", "2020-07-01", "2021-06-30", 35e9)
        _add("NetIncomeLoss", "2020-07-01", "2021-06-30", 9e9)
        _add("NetCashProvidedByUsedInOperatingActivities",
             "2020-07-01", "2021-06-30", 24e9)
        # The anomaly itself: FY2023 net income restated hugely upward while
        # operating cash flow is untouched → accruals share breaks its band.
        _add("NetIncomeLoss", "2022-07-01", "2023-06-30", 90e9,
             accn="0009-27", filed="2026-10-01")
        stmt = assemble_statements(facts, n_periods=5)
        stmt.ticker = "TST"
        anomalies = detect_anomalies(
            [r for r in RELATIONSHIPS if r.key == "accruals_share"],
            statements_series(stmt), entity=stmt.entity_name)
        assert len(anomalies) == 1
        a = anomalies[0]
        assert a.period == "FY2023"
        assert a.observed > a.expected_high      # NI >> CFO
        assert a.magnitude > 0
        q = a.question()
        assert q.startswith("Investigate:") and "FY2023" in q
        # evidence is provenance-grade and confidence-free
        ev = a.evidence()
        assert set(ev) == {"relationship", "expectation", "entity", "period",
                           "observed", "normal_range", "range_basis_periods",
                           "magnitude_in_robust_sigmas", "unit"}
