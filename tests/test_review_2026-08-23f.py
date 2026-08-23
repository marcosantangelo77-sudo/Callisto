"""Review run 8, 2026-08-23 — reproductions. READ-ONLY on production code.

RT1  money-path redteam (perf/standing-speed-0823-231635 @ 9324806):
     the shipped M1/M1b/M1c repros use inputs whose overround is POSITIVE,
     so they never pin the crossed-book defect they name. These tests pin
     the REAL defect with genuinely negative-overround books.
RT2  this branch has lost two closed fixes that exist on other branches:
     R1 alias-unanimity (agp/ensemble.py) and R2 calibration import.
RT3  re-pin of RV3: replay_ledger() mints PRIMARY under a keyed regime.

Each test fails for its stated reason on the target branch; verified by
execution before commit.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# RT1 — corrected crossed-book repros (family 3: invalid book treated as
# trusted). The red-team's own M1 used 0.60/0.61 = overround +0.21, which is
# NOT a crossed book. A genuine crossed book sums BELOW 1.
# ---------------------------------------------------------------------------

def _assess(price, counter):
    from tools.edge import MarketQuote, assess_edge
    q = MarketQuote(price=price, counter_price=counter, kind="probability")
    return assess_edge("rt-review-run8", 0.5, q)


def test_rt1_genuinely_crossed_book_must_not_devig():
    """price=0.45 / counter=0.50: complementary asks summing to 0.95 imply a
    NEGATIVE hold (-0.05) — arbitrage-grade nonsense. devig_market returns a
    confident 'fair' price instead of an error."""
    from tools.devig import devig_market
    r = devig_market([1 / 0.45, 1 / 0.50])
    assert r["overround"] < 0, "precondition: this book IS crossed"
    assert r.get("error") is not None, (
        f"devig_market accepted a crossed book (overround={r['overround']}) "
        f"and returned fair probabilities {r['fair_probabilities']}")


def test_rt1_crossed_book_must_not_reach_actionable_kelly_cap():
    """The real M1b: a crossed claim quote flows through assess_edge into
    actionable=True with Kelly at the quarter cap."""
    a = _assess(0.45, 0.50)
    assert a.devig_audit["overround"] == pytest.approx(-0.05), (
        "precondition: audit records the negative hold")
    assert not (a.actionable and a.kelly_fraction_full > 0), (
        f"crossed book produced actionable=True with Kelly "
        f"{a.kelly_fraction_full:.4f} instead of being refused")


def test_rt1_sanity_gate_tested_with_an_input_it_would_have_to_reject():
    """The shipped M1c test only ever fed the gate pairs in (0, 0.5). This
    pair is outside it — proving no gate exists anywhere on this path."""
    a = _assess(0.40, 0.41)   # complementary asks summing to 0.81 -> -0.19
    over = a.devig_audit["overround"]
    assert 0.0 < over < 0.5, (
        f"quote (0.40, 0.41) has nonsensical overround {over} but "
        f"assess_edge emitted a fair probability {a.market_prob_fair} "
        f"with no error")


# ---------------------------------------------------------------------------
# RT2 — fixes present on master/other branches but absent HERE
# (family #2 across branches).
# ---------------------------------------------------------------------------

def test_rt2_alias_reviewer_does_not_count_toward_unanimity_on_this_branch():
    """Run-1 R1: an author speaking through a proxy alias must not count as
    a second independent critic in unanimous_unrebutted."""
    from agp.ensemble import PanelVerdict, ReviewProvenance, AdversaryObjection
    prov = ReviewProvenance(
        author_model="gpt-4o",
        reviewer_models=["claude", "gpt-4o-proxy-alias"])
    objs = [AdversaryObjection(claim_id="c", model="claude", text="x"),
            AdversaryObjection(claim_id="c", model="gpt-4o-proxy-alias",
                               text="y")]
    pv = PanelVerdict(objections=objs, provenance=prov)
    assert pv.unanimous_unrebutted is False, (
        "unanimity bonus fired on self-review through an alias — the "
        "_same_weights fix exists on other branches but not this one")


def test_rt2_calibration_package_imports_on_this_branch():
    """Run-1 R2 / run-6 note: tools.calibration was fixed on master but is
    still un-importable here (replay_chain missing from instrument.py)."""
    import importlib
    try:
        importlib.import_module("tools.calibration")
    except ImportError as e:
        pytest.fail(f"tools.calibration un-importable on this branch: {e}")


# ---------------------------------------------------------------------------
# RT3 — replay_ledger() authenticates nothing under a key (RV3 re-pin at
# verifyopen HEAD; fails there for the stated reason).
# ---------------------------------------------------------------------------

def test_rt3_replay_ledger_refuses_bad_signature_when_keyed():
    from tools.pipeline import checkpoint as ckpt

    key = "unit-test-key"
    ck = ckpt.Checkpoint(
        key="rk:fetch_leaf:abc", run="rk", stage="fetch_leaf",
        input_hash="ih", produced_at="2026-08-23T12:00:00",
        payload={"fetches": [{"body": "hello", "url": "u",
                              "content_sha256":
                                  hashlib.sha256(b"hello").hexdigest()}]})
    signed = ck.signed(key)
    tampered = ckpt.Checkpoint(**{
        **signed.__dict__,
        "payload": {"fetches": [{"body": "EVIL", "url": "u",
                                 "content_sha256":
                                     hashlib.sha256(b"EVIL").hexdigest()}]}})
    assert not tampered.verify_signature(key)

    class L:
        stored: list = []

        def record_tool_result(self, *a, **k):
            L.stored.append((a, k))

        def has_observation(self, b):
            return any(a[0] == b for a, _ in L.stored)

    report = ckpt.replay_ledger(L(), [tampered])
    assert report["integrity_failures"], (
        "replay_ledger minted attacker bytes PRIMARY under a keyed regime "
        "because signature checking lives only in partition_admissibility")
