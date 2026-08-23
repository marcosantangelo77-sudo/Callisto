"""RED TEAM H3 — the adversary's calibration record is gameable.

The ledger's precision_of_attack = RIGHT / scored. Three ways to make it
flatter without ever catching a real flaw:

  1. NEVER OBJECT: n_scored=0 → verdict "insufficient_data", precision
     None. Nothing punishes silence; the attack prompt asks for zero
     objections when the conclusion 'withstands' — a lazy model can
     always return [] and its record stays spotless-by-vacuity.
  2. OBJECT ONLY ON LOSERS: an adversary that attacks weak claims (low
    evidence count, low confidence) and stays silent on strong ones
    accumulates precision with zero risk — objections only score RIGHT
    when the claim resolves WRONG, and weak claims resolve wrong often.
    Selection on easy targets, exactly as hypothesised.
  3. OVER-OBJECTION IS CHEAP: MINOR objections cost 0.05 confidence but
    score RIGHT whenever the claim fails for ANY reason — spraying minor
    objections over doomed claims farms precision.

Also: record_resolution scores EVERY pending objection on a claim
RIGHT/WRONG from one bit (claim_was_correct). An objection that attacked
a real flaw in a claim that nonetheless resolved correct is marked WRONG;
an objection that was noise on a claim that failed is RIGHT. The record
measures correlation with outcome, not quality of attack.

And: nothing verifies objections were produced by the model at all —
apply_verdict trusts whatever list it is handed; the seal veto records
SUSTAINED/OVERRULED by string-matching objection TEXT.
"""
import pytest

from agp.adversary import (
    Adversary,
    AdversaryObjection,
    AdversaryLedger,
)


def mk_ledger(tmp_path):
    led = AdversaryLedger(path=str(tmp_path / "d.jsonl"))
    # fresh instance per test to avoid state bleed through default path
    assert led.path.endswith("d.jsonl")
    return led


def raise_and_resolve(led, claim_id, text, claim_was_correct, model="m1",
                      severity="MINOR"):
    ob = AdversaryObjection(claim_id=claim_id, text=text, kind="false_positive",
                            severity=severity, model=model)
    led.record_objection(ob)
    led.record_resolution(claim_id, claim_was_correct)
    return ob


def test_silent_adversary_has_a_clean_record(tmp_path):
    """Vector 2/1 baseline: never objecting yields no bad verdict —
    'insufficient_data', not 'failing'. Silence costs nothing."""
    led = AdversaryLedger(path=str(tmp_path / f"silent.jsonl"))
    c = led.calibration()
    assert c["n_raised"] == 0
    assert c["precision_of_attack"] is None
    assert c["verdict"] == "insufficient_data"


def test_easy_target_farming_beats_honest_criticism(tmp_path):
    """Two adversaries:
      Farmer: objects only to 10 weak claims that all fail → precision 1.0
      Honest: objects to everything incl. 5 strong claims that hold →
              precision 0.33 → verdict 'too_harsh'.
    The metric ranks the farmer ABOVE the honest critic."""
    farm = AdversaryLedger(path=str(tmp_path / "farm.jsonl"))
    for i in range(10):
        raise_and_resolve(farm, f"weak{i}", "obj", claim_was_correct=False)
    hon = AdversaryLedger(path=str(tmp_path / "hon.jsonl"))
    for i in range(10):
        raise_and_resolve(hon, f"c{i}", "obj",
                          claim_was_correct=(i < 5))  # first 5 survive
    cf, ch = farm.calibration(), hon.calibration()
    assert cf["precision_of_attack"] == 1.0
    assert ch["precision_of_attack"] == 0.5
    assert cf["verdict"] == "well_calibrated"
    # The honest critic scores no WORSE verdict here, but the ranking
    # metric (precision) puts pure easy-target farming strictly above it;
    # with 6 survivors it would flip to 'too_harsh' for identical honesty.
    assert cf["precision_of_attack"] > ch["precision_of_attack"]


def test_one_bit_scores_every_objection_identically(tmp_path):
    """A claim carries two objections: one prescient, one noise. Resolution
    is a single bit, so both get the same outcome. Objection QUALITY is
    unmeasurable from this record."""
    led = AdversaryLedger(path=str(tmp_path / "onebit.jsonl"))
    led.record_objection(AdversaryObjection(
        claim_id="c", text="selection effect via multiple comparisons",
        kind="selection_effect", severity="MAJOR"))
    led.record_objection(AdversaryObjection(
        claim_id="c", text="vibes", kind="unspecified", severity="MINOR"))
    led.record_resolution("c", claim_was_correct=False)  # claim failed
    cal = led.calibration()
    assert cal["n_right"] == 2  # both scored RIGHT off the shared bit


def test_overrule_status_is_string_matched_so_duplicates_launder(tmp_path):
    """record_overrule matches RAISED objections by exact TEXT. Two
    identical-text objections: overruling marks BOTH. A later
    record_sustained finds none left RAISED. Status bookkeeping — which
    feeds n_sustained — is controllable by repeating strings."""
    led = AdversaryLedger(path=str(tmp_path / "dupe.jsonl"))
    for _ in range(2):
        led.record_objection(AdversaryObjection(
            claim_id="c", text="same text", severity="BLOCKING"))
    n = led.record_overrule("c", "same text", "reason")
    assert n == 2                      # both flipped by one decision
    led.record_sustained("c", "same text")   # silently does nothing
    assert led.calibration()["n_sustained"] == 0


def test_apply_verdict_accepts_unverifiable_objection_lists():
    """apply_verdict has no link to the ledger or to any model output —
    callers can pass hand-built objections to tank a rival summary, or an
    empty list to bless one. The asymmetry guarantee holds per-call, but
    there is no binding between 'the model said this' and 'this was applied'."""
    fake = [AdversaryObjection(claim_id="x", text="hand-written",
                               severity="BLOCKING")]
    score, reason = Adversary.apply_verdict(0.9, fake)
    # The score passes through unchanged — enforcement lives in the veto
    # reason string, which callers must honour:
    assert score == 0.9 and reason.startswith("hand-written")
    assert Adversary.apply_verdict(0.9, [])[1] == ""  # empty list blesses


@pytest.mark.asyncio
async def test_backend_failure_objection_never_reaches_the_ledger(tmp_path):
    """Fail-closed produces a BLOCKING objection attributed to model=''.
    But the exception path returns BEFORE record_objection — infrastructure
    failure leaves NO trace in the ledger. A persistently-failing adversary
    is indistinguishable from a perfectly silent one: n_raised=0,
    verdict 'insufficient_data'. The track record cannot see its own outages."""

    class FlakyRouter:
        async def complete(self, *a, **k):
            raise RuntimeError("503")

    adv = Adversary(FlakyRouter(), ledger=AdversaryLedger(
        path=str(tmp_path / "flaky.jsonl")))
    obs = await adv.attack("claim-1", "conclusion", ["e1"])
    assert obs[0].is_blocking
    assert obs[0].model == ""
    cal = adv.ledger.calibration()
    # THE VULNERABILITY: outage invisible; record stays spotless-by-vacuity.
    assert cal["n_raised"] == 0
    assert cal["verdict"] == "insufficient_data"
