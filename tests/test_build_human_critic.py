"""BUILD — THE USER AS A SCORED CRITIC (agp/human_critic.py).

Human dissent enters the SAME ledger with the SAME record shape as a model
critic's; the same asymmetry applies (lower/veto only, agreement never
raises); sustained/overruled tracked, resolution-scored RIGHT/WRONG,
calibration reported per domain exactly as calibration_by_model does for
models; overriding the system is a decision with a reason, never a silent edit.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from agp.adversary import AdversaryLedger, AdversaryObjection
from agp.human_critic import (
    HumanCritic,
    HumanOverride,
    OverrideLog,
    apply_human_verdict,
    clamp_with_human_agreement,
    human_critic_key,
    make_human_objection,
)


@pytest.fixture()
def hc(tmp_path):
    return HumanCritic(
        critic="owner",
        ledger=AdversaryLedger(str(tmp_path / "dissent.jsonl")),
        overrides=OverrideLog(str(tmp_path / "overrides.jsonl")))


def _model_ob(claim_id="c1", severity="MINOR", text="model worry",
              domain="FINANCIAL"):
    return AdversaryObjection(claim_id=claim_id, text=text,
                              severity=severity, model="gpt-x",
                              claim_domain=domain)


# ── 1. same structure, same ledger ──────────────────────────────────────────

def test_objection_shares_model_record_shape(hc):
    ob = hc.object_to("c1", "sample too small", severity="MAJOR",
                      kind="selection_effect", axis="survivorship",
                      claim_domain="FINANCIAL")
    assert isinstance(ob, AdversaryObjection)
    assert ob.claim_id == "c1" and ob.text and ob.severity == "MAJOR"
    assert ob.kind == "selection_effect"
    assert ob.model == human_critic_key("owner")
    assert "[axis: survivorship]" in ob.text


def test_objection_lands_in_shared_ledger_alongside_models(tmp_path):
    ledger = AdversaryLedger(str(tmp_path / "d.jsonl"))
    hc = HumanCritic(critic="owner", ledger=ledger)
    ledger.record_objection(_model_ob())
    hc.object_to("c1", "my objection", claim_domain="FINANCIAL")
    assert len(ledger.objections_for("c1")) == 2
    # persisted as JSONL, reloadable
    again = AdversaryLedger(str(tmp_path / "d.jsonl"))
    assert len(again.objections_for("c1")) == 2


def test_validation_rejects_empty_text_and_bad_severity(hc):
    with pytest.raises(ValueError):
        make_human_objection("c1", "   ")
    with pytest.raises(ValueError):
        make_human_objection("c1", "x", severity="CATASTROPHIC")


# ── 2. asymmetry: lower or veto only; agreement never raises ───────────────

def test_blocking_human_objection_vetoes_seal(hc):
    ob = hc.object_to("c1", "the base rate contradicts this",
                      severity="BLOCKING")
    score, reason = apply_human_verdict(0.80, [ob])
    assert score == 0.80 and reason == ob.text  # truthy reason → seal refused


def test_non_blocking_objections_only_lower(hc):
    ob = hc.object_to("c1", "minor concern", severity="MINOR")
    score, _ = apply_human_verdict(0.60, [ob])
    assert score < 0.60


def test_agreement_never_raises_above_provenance():
    assert clamp_with_human_agreement(0.95, 0.55, human_agrees=True) == 0.55
    assert clamp_with_human_agreement(0.40, 0.55, human_agrees=True) == 0.40


# ── 3. track record + per-domain calibration ────────────────────────────────

def test_sustained_overruled_tracked_and_resolution_scored(hc):
    hc.object_to("c1", "obj A", severity="BLOCKING")
    hc.object_to("c1", "obj B", severity="MINOR")
    hc.sustain("c1", "obj A")
    hc.concede("c1", "obj B", "evidence attached later covers it")
    statuses = [o.status for o in hc.ledger.objections_for("c1")]
    assert statuses.count("SUSTAINED") == 1
    assert statuses.count("OVERRULED") == 1
    # claim turns out WRONG → objections were RIGHT
    hc.record_resolution("c1", claim_was_correct=False)
    mine = hc.my_calibration()
    assert mine["n_scored"] == 2 and mine["n_right"] == 2
    assert mine["precision_of_attack"] == 1.0


def test_per_domain_calibration_mirrors_calibration_by_model(hc):
    # FINANCIAL: owner right twice; TECHNICAL: owner wrong once.
    hc.object_to("f1", "fin obj", claim_domain="FINANCIAL")
    hc.object_to("f2", "fin obj2", claim_domain="FINANCIAL")
    hc.object_to("t1", "tech obj", claim_domain="TECHNICAL")
    hc.record_resolution("f1", False)   # claim wrong → objection RIGHT
    hc.record_resolution("f2", False)
    hc.record_resolution("t1", True)    # claim right → objection WRONG
    by_domain = hc.calibration_by_domain()
    assert by_domain["FINANCIAL"]["n_right"] == 2
    assert by_domain["FINANCIAL"]["verdict"] == "well_calibrated"
    assert by_domain["TECHNICAL"]["n_right"] == 0
    assert by_domain["TECHNICAL"]["verdict"] == "too_harsh"
    # identical machinery files him alongside models:
    hc.ledger.record_resolution("f1", False)
    m = hc.ledger.calibration_by_model(domain="FINANCIAL")
    assert m["human:owner"]["n_right"] == 2
    assert m["human:owner"]["verdict"] == "well_calibrated"


def test_unresolved_claims_report_insufficient_data_not_a_ratio(hc):
    hc.object_to("c1", "early objection")
    cal = hc.my_calibration()
    assert cal["n_raised"] == 1 and cal["n_scored"] == 0
    assert cal["precision_of_attack"] is None
    assert cal["verdict"] == "insufficient_data"


def test_ledger_survives_restart_with_scores(tmp_path):
    path = str(tmp_path / "d.jsonl")
    h1 = HumanCritic(ledger=AdversaryLedger(path))
    h1.object_to("c1", "it will be wrong", claim_domain="SIGNAL")
    h1.record_resolution("c1", False)
    h2 = HumanCritic(ledger=AdversaryLedger(path))
    assert h2.my_calibration()["n_right"] == 1


# ── 4. overriding the system is a decision, not an edit ────────────────────

def test_override_requires_reason(tmp_path):
    log = OverrideLog(str(tmp_path / "o.jsonl"))
    with pytest.raises(ValueError):
        log.record(HumanOverride(claim_id="c1", action="forced_seal", reason=""))
    ov = HumanOverride(claim_id="c1", action="forced_seal",
                       reason="new primary evidence arrived post-seal",
                       confidence_at_decision=0.62)
    log.record(ov)
    assert log.for_claim("c1")[0].reason.startswith("new primary")


def test_concede_writes_override_record_with_reason(hc):
    hc.object_to("c1", "worry", severity="MINOR")
    n = hc.concede("c1", "worry", "sealed anyway: penalty applied")
    assert n == 1
    recs = hc.overrides.for_claim("c1")
    assert len(recs) == 1 and recs[0].action == "dismissed_objection"


def test_forced_seal_past_sustained_model_objection_is_logged(hc):
    hc.ledger.record_objection(_model_ob(severity="BLOCKING"))
    hc.override_system("c1", "forced_seal",
                       reason="I have private knowledge the source missed",
                       confidence_at_decision=0.70)
    assert len(hc.overrides.for_claim("c1")) == 1


def test_override_log_is_append_only_jsonl(tmp_path):
    log = OverrideLog(str(tmp_path / "o.jsonl"))
    for i in range(3):
        log.record(HumanOverride(claim_id=f"c{i}", action="forced_seal",
                                 reason=f"reason {i}"))
    lines = open(log.path).read().strip().splitlines()
    assert len(lines) == 3 and all(json.loads(l)["reason"] for l in lines)


# ── CLI smoke ───────────────────────────────────────────────────────────────

def test_cli_roundtrip(tmp_path, capsys):
    from agp.human_critic import main
    sd = str(tmp_path / "state")
    main(["--state-dir", sd, "--critic", "owner", "object", "c1",
          "the sample is biased", "--severity", "major",
          "--kind", "selection_effect", "--axis", "survivorship",
          "--domain", "FINANCIAL"])
    capsys.readouterr()
    main(["--state-dir", sd, "veto", "c1", "the sample is biased "
          "[axis: survivorship]"])
    main(["--state-dir", sd, "resolve", "c1", "incorrect"])
    main(["--state-dir", sd, "calibrate"])
    capsys.readouterr()
    # state persisted; re-read it directly
    hc2 = HumanCritic(ledger=AdversaryLedger(
        os.path.join(sd, "adversary_dissent.jsonl")))
    assert hc2.my_calibration()["n_right"] == 1
