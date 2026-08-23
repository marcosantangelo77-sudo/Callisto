"""Regression tests for the AdversaryLedger reload defect (improve_agp_core).

Before the fix: a ledger reloaded from disk replayed EVERY journal line as a
separate objection, so calibration() reported n_raised inflated by the
lifecycle length and n_scored=0 / verdict=insufficient_data — the track
record that distinguishes a real critic from a rubber stamp was blank in any
fresh process. The correct read existed only inside agp.human_critic.
"""
import json

import pytest

from agp.adversary import AdversaryLedger, AdversaryObjection


def _raise(led, claim_id="c1", text="sample too small", sev="MINOR"):
    led.record_objection(AdversaryObjection(
        claim_id=claim_id, text=text, severity=sev))


def test_reloaded_ledger_preserves_lifecycle_and_scores(tmp_path):
    path = str(tmp_path / "d.jsonl")
    led = AdversaryLedger(path=path)
    _raise(led)
    n = led.record_overrule("c1", "sample too small", "n=400 is adequate")
    assert n == 1
    led.record_resolution("c1", claim_was_correct=True)  # claim right → objection WRONG

    fresh = AdversaryLedger(path=path)
    cal = fresh.calibration()
    # distinct objections, not journal lines
    assert cal["n_raised"] == 1
    assert cal["n_sustained"] == 0
    assert cal["n_scored"] == 1
    assert cal["n_right"] == 0
    assert cal["verdict"] == "too_harsh"
    resolved = fresh.all_resolved()
    assert [(o.status, o.outcome) for o in resolved] == [("OVERRULED", "WRONG")]


def test_reloaded_matches_in_memory(tmp_path):
    path = str(tmp_path / "d.jsonl")
    led = AdversaryLedger(path=path)
    _raise(led, "a", "obj A")
    _raise(led, "b", "obj B", sev="MAJOR")
    led.record_overrule("a", "obj A", "reason")
    led.record_resolution("a", claim_was_correct=False)  # claim wrong → RIGHT
    led.record_sustained("b", "obj B")

    fresh = AdversaryLedger(path=path)
    assert fresh.calibration() == led.calibration()


def test_journal_stays_append_only_with_history(tmp_path):
    """The fix changes the READ model only; the file still holds every
    lifecycle copy so the history remains auditable by eye."""
    path = str(tmp_path / "d.jsonl")
    led = AdversaryLedger(path=path)
    _raise(led)
    led.record_overrule("c1", "sample too small", "why")
    led.record_resolution("c1", claim_was_correct=True)
    lines = [json.loads(ln) for ln in
             open(path, encoding="utf-8").read().splitlines() if ln.strip()]
    assert len(lines) == 3  # raise + overruled-copy + resolved-copy


def test_calibration_by_model_after_reload(tmp_path):
    path = str(tmp_path / "d.jsonl")
    led = AdversaryLedger(path=path)
    ob = AdversaryObjection(claim_id="c", text="t", severity="MINOR",
                            model="m1")
    led.record_objection(ob)
    led.record_overrule("c", "t", "r")
    led.record_resolution("c", claim_was_correct=True)

    fresh = AdversaryLedger(path=path)
    by_model = fresh.calibration_by_model()
    assert by_model["m1"]["n_scored"] == 1
    assert by_model["m1"]["precision_of_attack"] == 0.0


def test_human_critic_delegates_to_shared_ledger(tmp_path):
    from agp.human_critic import HumanCritic
    led = AdversaryLedger(str(tmp_path / "shared.jsonl"))
    hc = HumanCritic(critic="owner", ledger=led)
    hc.object_to("c9", "the base rate contradicts this")
    hc.concede("c9", "the base rate contradicts this", "checked; it does not")
    hc.record_resolution("c9", claim_was_correct=True)

    cal = hc.my_calibration()
    assert cal["n_raised"] == 1
    assert cal["n_scored"] == 1
    assert cal["n_right"] == 0

    # and a fresh critic over the same file sees the same record
    fresh = HumanCritic(critic="owner",
                        ledger=AdversaryLedger(str(tmp_path / "shared.jsonl")))
    assert fresh.my_calibration() == cal
