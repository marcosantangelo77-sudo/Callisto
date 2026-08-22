"""Tests for the Adversary — fourth AGP role (R3 build wave 2)."""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from agp import AGPSession, AGPSealRefused, Domain, Evidence, SessionStep, SourceClass
from agp.adversary import (
    Adversary,
    AdversaryLedger,
    AdversaryObjection,
    AGPRole,
    DISAGREEMENT_CEILING,
    MILD_DISAGREEMENT_CEILING,
    clamp_with_ensemble,
    ensemble_ceiling,
    install_adversary,
)


def _ev(content="BTC spot ETF inflows hit $1.2B in March", cls=SourceClass.PRIMARY,
        conf=0.8, domain=Domain.FINANCIAL):
    return Evidence(content=content, source_class=cls, confidence_score=conf,
                    domain=domain, origin_agent="manager")


def _ready_session(query="Is Bitcoin a good buy right now?", conf=0.85):
    s = AGPSession(query)
    s.domain = Domain.FINANCIAL
    for step in (SessionStep.ASSIGN_DOMAIN, SessionStep.SOURCE_ENUMERATION,
                 SessionStep.PRIMARY_COLLECTION, SessionStep.CONTRADICTION_CHECK,
                 SessionStep.SYNTHESIS, SessionStep.SESSION_CLOSE):
        s.advance_to(step)
    s.add_evidence(_ev())
    from agp import SessionSummary
    s.summary = SessionSummary(
        scope=query, domain=Domain.FINANCIAL, conclusion="BTC is a buy",
        confidence_score=conf, evidence_count=1, contradiction_count=0)
    return s


class StubRouter:
    """Router stand-in exposing the ProviderRouter.complete surface."""

    def __init__(self, objections=None, fail=False, model="stub-model"):
        self.objections = objections if objections is not None else []
        self.fail = fail
        self.model = model
        self.calls = []

    async def complete(self, task_class, messages, schema=None, **kw):
        self.calls.append({"task_class": task_class, "messages": messages})
        if self.fail:
            raise RuntimeError("endpoint dead")
        return {"content": json.dumps({"objections": self.objections}),
                "parsed_json": {"objections": self.objections},
                "model": self.model, "tier": "stub", "task_class": task_class}


# ── 1. the attack itself ──────────────────────────────────────────────────

def test_attack_produces_and_records_objections(tmp_path):
    led = AdversaryLedger(str(tmp_path / "d.jsonl"))
    router = StubRouter(objections=[
        {"kind": "selection_effect", "severity": "MAJOR",
         "text": "ETF inflow data only covers surviving products — survivorship bias"},
        {"kind": "alternative_explanation", "severity": "MINOR",
         "text": "inflows coincide with quarter-end rebalancing, not demand"},
    ])
    adv = Adversary(router, led)
    obs = asyncio.run(adv.attack("c1", "BTC is a buy", ["inflows $1.2B"]))
    assert len(obs) == 2
    assert all(o.status == "RAISED" for o in obs)
    assert led.objections_for("c1") == obs
    assert router.calls[0]["task_class"] == "adversarial_review"
    assert router.calls[0]["messages"][0]["role"] == "system"


def test_attack_backend_failure_fails_closed(tmp_path):
    adv = Adversary(StubRouter(fail=True), AdversaryLedger(str(tmp_path / "d.jsonl")))
    obs = asyncio.run(adv.attack("c1", "BTC is a buy", ["e"]))
    assert len(obs) == 1 and obs[0].is_blocking


def test_attack_prompt_is_domain_general(tmp_path):
    """Same machinery for betting, Bitcoin, and materials science."""
    led = AdversaryLedger(str(tmp_path / "d.jsonl"))
    router = StubRouter(objections=[])
    adv = Adversary(router, led)
    for conclusion, ev in [
        ("Bills cover the spread", ["Bills 8-2 ATS this season"]),
        ("Perovskite tandem cells degrade <1%/yr", ["T80 lifetime 1200h at 65C"]),
        ("BTC is a buy", ["inflows $1.2B"]),
    ]:
        asyncio.run(adv.attack("cx", conclusion, ev))
    # No domain vocabulary anywhere in the attack prompt.
    blob = json.dumps(router.calls[-1]["messages"])
    for word in ("bet", "bitcoin", "btc", "perovskite", "spread"):
        assert word not in router.calls[-1]["messages"][0]["content"].lower()


# ── 2. asymmetry: adversary can only lower ───────────────────────────────

def test_apply_verdict_never_raises():
    objs = [AdversaryObjection(claim_id="c", text="x", severity="MINOR")]
    score, reason = Adversary.apply_verdict(0.50, objs)
    assert score == 0.45 and reason
    # No objections → unchanged, never a bonus.
    score, reason = Adversary.apply_verdict(0.50, [])
    assert score == 0.50 and reason == ""
    assert Adversary.apply_verdict(0.50, objs)[0] <= 0.50


def test_blocking_objection_vetoes():
    objs = [AdversaryObjection(claim_id="c", text="fatal flaw", severity="BLOCKING")]
    score, reason = Adversary.apply_verdict(0.95, objs)
    assert reason == "fatal flaw"  # veto; score untouched because seal refuses


def test_penalty_floor_at_zero():
    objs = [AdversaryObjection(claim_id="c", text="a", severity="MAJOR"),
            AdversaryObjection(claim_id="c", text="b", severity="MAJOR"),
            AdversaryObjection(claim_id="c", text="c", severity="MAJOR"),
            AdversaryObjection(claim_id="c", text="d", severity="MAJOR")]
    score, _ = Adversary.apply_verdict(0.40, objs)
    assert score == 0.0


# ── 3. seal-path wiring via the existing seal_veto hook ──────────────────

def test_clean_conclusion_seals_with_no_change(tmp_path):
    s = _ready_session(conf=0.80)
    adv = Adversary(StubRouter(objections=[]), AdversaryLedger(str(tmp_path / "d.jsonl")))
    install_adversary(s, adv)
    h = s.seal()
    assert h and s.summary.confidence_score == 0.80  # never raised


def test_nonblocking_objections_lower_confidence_and_seal(tmp_path):
    s = _ready_session(conf=0.80)
    adv = Adversary(StubRouter(objections=[
        {"kind": "selection_effect", "severity": "MAJOR", "text": "survivorship"}]),
        AdversaryLedger(str(tmp_path / "d.jsonl")))
    install_adversary(s, adv)
    s.seal()
    assert s.summary.confidence_score == pytest.approx(0.65)
    assert any("adversary" in o for o in s.manager_objections)


def test_blocking_objection_refuses_seal(tmp_path):
    s = _ready_session()
    led = AdversaryLedger(str(tmp_path / "d.jsonl"))
    adv = Adversary(StubRouter(objections=[
        {"kind": "false_positive", "severity": "BLOCKING", "text": "data is synthetic"}]), led)
    install_adversary(s, adv)
    with pytest.raises(AGPSealRefused, match="adversary veto: data is synthetic"):
        s.seal()
    assert all(o.status == "SUSTAINED" for o in led.objections_for(s.session_id))


def test_router_crash_at_seal_fails_closed(tmp_path):
    s = _ready_session()
    install_adversary(s, Adversary(StubRouter(fail=True),
                                   AdversaryLedger(str(tmp_path / "d.jsonl"))))
    with pytest.raises(AGPSealRefused):
        s.seal()


def test_overruled_objections_are_logged(tmp_path):
    s = _ready_session(conf=0.80)
    led = AdversaryLedger(str(tmp_path / "d.jsonl"))
    install_adversary(s, Adversary(StubRouter(objections=[
        {"kind": "selection_effect", "severity": "MINOR", "text": "small n"}]), led))
    s.seal()
    obs = led.objections_for(s.session_id)
    assert obs[0].status == "OVERRULED"
    assert "sealed over objection" in obs[0].overrule_reasoning


# ── 4. scored track record + calibration ─────────────────────────────────

def test_resolution_scores_objections(tmp_path):
    led = AdversaryLedger(str(tmp_path / "d.jsonl"))
    led.record_objection(AdversaryObjection("c1", "it will lose", severity="MAJOR"))
    led.record_objection(AdversaryObjection("c2", "bad base rate", severity="MAJOR"))
    led.record_resolution("c1", claim_was_correct=False)   # claim lost → objection RIGHT
    led.record_resolution("c2", claim_was_correct=True)    # claim won → objection WRONG
    calib = led.calibration()
    assert calib["n_scored"] == 2 and calib["n_right"] == 1
    assert calib["precision_of_attack"] == 0.5


def test_ledger_survives_restart(tmp_path):
    p = str(tmp_path / "d.jsonl")
    led1 = AdversaryLedger(p)
    led1.record_objection(AdversaryObjection("c1", "survivorship", severity="MAJOR"))
    led2 = AdversaryLedger(p)
    assert led2.objections_for("c1")[0].text == "survivorship"


def test_calibration_flags_insufficient_data_and_too_harsh(tmp_path):
    p = str(tmp_path / "d.jsonl")
    led = AdversaryLedger(p)
    assert led.calibration()["verdict"] == "insufficient_data"
    for i in range(4):  # critic wrong 3 of 4 times → too harsh
        led.record_objection(AdversaryObjection(f"c{i}", "noise", severity="MINOR"))
        led.record_resolution(f"c{i}", claim_was_correct=(i != 0))
    assert led.calibration()["verdict"] == "too_harsh"


# ── 5. ensemble disagreement as a confidence input ────────────────────────

def test_wide_disagreement_caps_below_probable():
    assert ensemble_ceiling([0.9, 0.4, 0.85]) == DISAGREEMENT_CEILING
    score, reason = clamp_with_ensemble(0.90, [0.9, 0.4])
    assert score == DISAGREEMENT_CEILING and "disagreement" in reason


def test_mild_disagreement_caps_at_mild():
    assert ensemble_ceiling([0.75, 0.55]) == MILD_DISAGREEMENT_CEILING


def test_agreement_never_raises():
    assert ensemble_ceiling([0.9, 0.88, 0.91]) is None
    assert clamp_with_ensemble(0.9, [0.9, 0.88]) == (0.9, "")


def test_single_evaluation_no_signal():
    assert ensemble_ceiling([0.9]) is None
    assert ensemble_ceiling([]) is None


def test_role_task_classes_cover_four_roles():
    roles = {AGPRole.ARCHITECT, AGPRole.MANAGER, AGPRole.SENTINEL, AGPRole.ADVERSARY}
    assert set(AGPRole.ROLE_TASK_CLASSES) == roles
    # Adversary routes through a declared router task class, never a model name.
    assert AGPRole.ROLE_TASK_CLASSES[AGPRole.ADVERSARY] == ["adversarial_review"]
