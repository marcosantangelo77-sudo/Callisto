"""cmefedfut — CME ZQ settlements + FedWatch-style derived probabilities.

Same hard rule as test_build_r4_sources.py: NO network socket. All fetches
run on RestSource's injectable transport with canned payloads. These tests
pin the two provenance classes separately (PRIMARY settlements vs INFERRED
probabilities), the W5 date guard, and the wiring onto
RetrodictionQuestion.market_implied.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers.no_socket import NoSocket  # noqa: E402

_guard = NoSocket()
_guard.install()

import pytest  # noqa: E402

from tools.retrodiction.questions import (  # noqa: E402
    RetrodictionQuestion,
    QuestionType,
)
from tools.sources.base import RestSource, _RateLimiter, PROVENANCE_TIERS  # noqa: E402


class FakeTransport:
    def __init__(self):
        self.routes = {}
        self.calls = []

    def stage(self, url, payload):
        self.routes[url] = payload if isinstance(payload, str) else json.dumps(payload)

    def __call__(self, url, headers):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"un-staged URL fetched: {url}")
        return 200, self.routes[url]


def make_adapter(routes):
    from tools.sources.cmefedfut import SPEC, CmeFedFutAdapter

    t = FakeTransport()
    for u, p in routes.items():
        t.stage(u, p)
    src = RestSource(SPEC, ledger=None, transport=t, _limiter=_RateLimiter(0.0))
    return CmeFedFutAdapter(src), t


SETTLE_URL = ("https://www.cmegroup.com/CmeWS/mvc/Settlements/TradeDate/305/"
              "?tradeDate=20241101&pageSize=500")


def _payload():
    return {
        "settlements": [
            {"product": "ZQZ24", "settle": "95:80"},  # Dec-24: avg EFFR 4.20%
            {"product": "GEZ4", "settle": "96:00"},   # non-ZQ, filtered out
            {"product": "ZQH25", "settle": ""},       # unpriced month dropped
        ],
    }


def test_spec_is_honest_and_market_price_tier():
    from tools.sources.cmefedfut import SPEC

    assert SPEC.tier == 3
    assert SPEC.tier == 3 and PROVENANCE_TIERS[3] == "market prices"
    assert any("implied" in a.lower() for a in SPEC.answers)
    assert any("realised" in c or "outcomes" in c for c in SPEC.cannot_answer)
    assert SPEC.cannot_answer, "empty cannot_answer overstates coverage"
    assert not SPEC.key_env_var, "must stay keyless/read-only"


def test_settlements_primary_recorded():
    ad, t = make_adapter({SETTLE_URL: _payload()})
    out = ad.settlements("20241101")
    assert out["trade_date"] == "20241101"
    assert out["_fetch"]["sha256"]
    assert len(out["settlements"]) == 3
    # the exact wire body is what got hashed (PRIMARY payload)
    assert t.routes[SETTLE_URL]


def test_zq_curve_filters_non_zq_and_unpriced():
    ad, _t = make_adapter({SETTLE_URL: _payload()})
    curve = ad.zq_curve("20241101")
    assert set(curve) == {"ZQZ24", "_fetch", "_trade_date"}
    assert curve["ZQZ24"]["price"] == pytest.approx(95.80)
    assert curve["ZQZ24"]["expected_effr"] == pytest.approx(4.20)


def test_derived_probability_fedwatch_methodology():
    """Dec-24 contract at settle 95:80 → avg EFFR 4.20%. Meeting 18 Dec 2024,
    current upper bound 4.25%, December has 31 days: 17 before + 14 after.
    post = (4.20*31 − 4.25*17)/14 = 4.1393% → change −0.1107% → probability
    of a cut ≈ 0.1107/25bp ≈ 0.4429 (published FedWatch methodology)."""
    ad, _t = make_adapter({SETTLE_URL: _payload()})
    d = ad.implied_probability("2024-12-18", 4.25, trade_date="20241101")
    assert d is not None
    assert d["direction"] == "cut"
    post = (4.20 * 31 - 4.25 * 17) / 14
    assert d["probability_of_change"] == pytest.approx(
        (4.25 - post) / 0.25, abs=1e-4)
    assert 0.0 <= d["probability_of_change"] <= 1.0
    # INFERRED must cite its PRIMARY parent with trade date + hash
    assert d["provenance_class"] == "INFERRED"
    assert d["derived_from"]["class"] == "PRIMARY"
    assert d["derived_from"]["trade_date"] == "20241101"
    assert d["derived_from"]["fetch"]["sha256"]


def test_meeting_outside_contract_months_returns_none():
    """Honest absence: no fabricated benchmark when no ZQ month covers it."""
    ad, _t = make_adapter({SETTLE_URL: _payload()})
    assert ad.implied_probability("2025-03-19", 4.50,
                                  trade_date="20241101") is None


def test_bad_trade_date_rejected_before_fetch():
    ad, t = make_adapter({})
    with pytest.raises(ValueError):
        ad.settlements("not-a-date")
    assert t.calls == []


# ── W5 date guard ──────────────────────────────────────────────────────────

def _derived(trade_date="20241101", prob=0.34):
    return {"probability_of_change": prob,
            "derived_from": {"class": "PRIMARY", "trade_date": trade_date,
                             "fetch": {"sha256": "x"}}}


def _q(qid, claim):
    return RetrodictionQuestion(
        question_id=qid,
        text="Will the FOMC cut the target range?",
        domain="MACRO", question_type=QuestionType.EVENT_OUTCOME,
        claim_date=claim, resolution_date=date(2024, 12, 31),
        answer_binary=True)


def test_attach_sets_market_implied():
    q = _q("a1", date(2024, 11, 15))
    skipped = __import__("tools.sources.cmefedfut",
                         fromlist=["attach_from_derived"]).attach_from_derived(
                             [q], {"a1": _derived()})
    assert not skipped
    assert q.market_implied == pytest.approx(0.34)


def test_attach_refuses_benchmark_dated_after_claim():
    """A settlement ON the claim date cannot have fed the question — refuse.
    This is the mis-dated-benchmark guard; it fails closed."""
    q = _q("a2", date(2024, 11, 1))          # same day as the settlement
    mod = __import__("tools.sources.cmefedfut", fromlist=["attach"])
    skipped = mod.attach_from_derived([q], {"a2": _derived()})
    assert q.market_implied is None
    assert "refusing" in skipped["a2"]


def test_attach_refuses_unprovenanced_unless_allowed():
    q = _q("a3", date(2024, 11, 15))
    mod = __import__("tools.sources.cmefedfut", fromlist=["attach"])
    skipped = mod.attach_from_derived(
        [q], {"a3": {"probability_of_change": 0.9}})
    assert q.market_implied is None and "provenance" in skipped["a3"]
    skipped = mod.attach_from_derived(
        [q], {"a3": {"probability_of_change": 0.9}},
        allow_no_provenance=True)
    assert q.market_implied == pytest.approx(0.9)


def test_attach_rejects_out_of_range():
    q = _q("a4", date(2024, 11, 15))
    mod = __import__("tools.sources.cmefedfut", fromlist=["attach"])
    skipped = mod.attach_from_derived(
        [q], {"a4": {"probability_of_change": 1.7,
                     "derived_from": {"trade_date": "20241101"}}})
    assert q.market_implied is None


def test_live_client_opt_in_gated():
    """No socket can be opened without explicit opt-in env var."""
    from tools.sources.cmefedfut import make_adapter as mk

    saved = os.environ.pop("CALLISTO_ENABLE_NETWORK", None)
    try:
        with pytest.raises(RuntimeError, match="opt-in"):
            mk()
    finally:
        if saved is not None:
            os.environ["CALLISTO_ENABLE_NETWORK"] = saved
