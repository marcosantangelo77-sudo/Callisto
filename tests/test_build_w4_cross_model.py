"""W4 — CROSS-MODEL ADVERSARIAL REVIEW.

Model distinctness visible in the record; multi-adversary pooling;
disagreement as a measured signal; per-critic calibration keyed by model;
honest degradation to a single backend. Fixtures only — no network.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from hypothesis import given, settings, strategies as st

from agp.adversary import (
    Adversary,
    AdversaryLedger,
    AdversaryObjection,
    TIER_SPECULATIVE_MAX,
)
from agp.ensemble import (
    UNANIMITY_BONUS_PENALTY,
    SELF_REVIEW_CEILING,
    AdversaryPanel,
    DisagreementRecord,
    PanelVerdict,
    ReviewProvenance,
    apply_panel_verdict,
    capture_substantive_disagreement,
    normalize_model,
)


# ── fixtures ────────────────────────────────────────────────────────────────

class StubRouter:
    """Router stand-in; reports its model name like ProviderRouter does."""

    def __init__(self, objections=None, fail=False, model="stub-model"):
        self.objections = objections if objections is not None else []
        self.fail = fail
        self.model = model

    async def complete(self, task_class, messages, schema=None, **kw):
        if self.fail:
            raise RuntimeError("endpoint dead")
        return {"content": json.dumps({"objections": self.objections}),
                "parsed_json": {"objections": self.objections},
                "model": self.model, "tier": "stub", "task_class": task_class}


def _adv(tmp_path, name, objections=None, fail=False):
    return Adversary(StubRouter(objections=objections, fail=fail, model=name),
                     AdversaryLedger(str(tmp_path / f"d-{name}.jsonl")))


def _ob(kind="selection_effect", severity="MAJOR", text="survivorship bias",
        model="m1", claim_id="c1"):
    return AdversaryObjection(claim_id=claim_id, text=text, kind=kind,
                              severity=severity, model=model,
                              claim_domain="FINANCIAL")


# ── 1. MODEL DISTINCTNESS IS VISIBLE ────────────────────────────────────────

def test_self_review_detected_and_capped():
    prov = ReviewProvenance(author_model="qwen3.6:35b",
                            reviewer_models=["openai/qwen3.6:35b"])
    assert prov.mode == "self_review"
    assert prov.ceiling == SELF_REVIEW_CEILING == TIER_SPECULATIVE_MAX


def test_independent_review_has_no_provenance_cap():
    prov = ReviewProvenance(author_model="claude_code", reviewer_models=["gpt-4o"])
    assert prov.mode == "independent_review" and prov.ceiling is None


def test_normalize_model_handles_provider_prefixes_and_build_tags():
    assert normalize_model("OpenAI/GPT-4o-2024-08-06") == "gpt-4o"
    assert normalize_model("gpt-4o") == "gpt-4o"
    # ambiguity resolves conservative: same spelling = same model = self-review
    assert normalize_model("  ") == ""


@given(st.text(min_size=0, max_size=30))
def test_normalize_is_idempotent(name):
    n = normalize_model(name)
    assert normalize_model(n) == n


def test_panel_verdict_reports_self_review_mode(tmp_path):
    panel = AdversaryPanel([_adv(tmp_path, "same-model", objections=[])])
    v = asyncio.run(panel.attack("c1", "claim", ["e"], author_model="same-model"))
    assert v.provenance.mode == "self_review"


def test_self_review_ceiling_actually_clamps():
    v = PanelVerdict(provenance=ReviewProvenance(
        author_model="a", reviewer_models=["a"]))
    score, reason = v.apply(0.95)
    assert score <= SELF_REVIEW_CEILING and "self_review" in reason


def test_independent_clean_review_leaves_high_score_alone():
    v = PanelVerdict(provenance=ReviewProvenance(
        author_model="a", reviewer_models=["b"]))
    score, reason = v.apply(0.95)
    assert score == 0.95 and reason == ""


# ── 2. MULTI-ADVERSARY POOLING ──────────────────────────────────────────────

def test_objections_from_n_adversaries_are_pooled(tmp_path):
    panel = AdversaryPanel([
        _adv(tmp_path, "mA", objections=[
            {"kind": "selection_effect", "severity": "MAJOR", "text": "bias A"}]),
        _adv(tmp_path, "mB", objections=[
            {"kind": "false_positive", "severity": "MINOR", "text": "noise B"}]),
        _adv(tmp_path, "mC"),
    ])
    v = asyncio.run(panel.attack("c1", "claim", ["e"], author_model="author"))
    texts = {o.text for o in v.objections}
    assert texts == {"bias A", "noise B"}
    assert all(o.claim_domain == "" for o in v.objections)  # unset when untagged


def test_unanimous_objection_heavier_than_lone_one(tmp_path):
    lone = _ob(severity="MAJOR", model="mA")
    v1 = PanelVerdict(objections=[lone], provenance=ReviewProvenance(
        author_model="author", reviewer_models=["mA", "mB"]))
    v2 = PanelVerdict(objections=[lone,
                                  _ob(text="bias A (second critic)", severity="MAJOR", model="mB")],
                      provenance=ReviewProvenance(
                          author_model="author", reviewer_models=["mA", "mB"]))
    s1, _ = v1.apply(0.80)
    s2, r2 = v2.apply(0.80)
    assert s2 < s1                       # unanimous weighs more
    assert "unanimous" in r2


def test_blocking_from_any_member_vetoes(tmp_path):
    panel = AdversaryPanel([
        _adv(tmp_path, "mA"),
        _adv(tmp_path, "mB", objections=[
            {"kind": "false_positive", "severity": "BLOCKING", "text": "fatal"}]),
    ])
    v = asyncio.run(panel.attack("c1", "claim", ["e"], author_model="other"))
    score, reason = v.apply(0.9)
    assert reason.startswith("adversary panel veto:") and "fatal" in reason


def test_zero_adversaries_fails_loudly():
    with pytest.raises(ValueError):
        AdversaryPanel([])


def test_panel_member_failure_fails_closed(tmp_path):
    panel = AdversaryPanel([
        _adv(tmp_path, "mA"),
        _adv(tmp_path, "mB", fail=True),
    ])
    v = asyncio.run(panel.attack("c1", "claim", ["e"], author_model="x"))
    assert v.backend_failures == 1
    assert v.has_blocking


# ── 3. DISAGREEMENT AS A MEASURED SIGNAL ────────────────────────────────────

def test_substantive_disagreement_captured_between_independents():
    objs = [_ob(model="critic-A", text="sample only covers survivors"),
            _ob(model="", claim_id="")]      # noise dropped by attribution rules
    recs = capture_substantive_disagreement(
        objs, reviewer_models=["critic-A", "critic-B"], author_model="author")
    assert len(recs) == 1
    r = recs[0]
    assert r.attacking_models == ["critic-A"]
    assert r.non_attacking_models == ["critic-B"]
    assert "representativeness" not in r.describe()  # no invented vocabulary
    assert "sample only covers survivors" in r.describe()


def test_solo_objector_is_not_disagreement():
    """One independent critic objecting where the other is silent IS captured;
    but an objection from the AUTHOR's own model never counts as dissent."""
    solo_author = [_ob(model="author")]
    assert capture_substantive_disagreement(
        solo_author, reviewer_models=["author"], author_model="author") == []


def test_disagreement_never_raises_anything():
    objs = [_ob(model="A"), _ob(model="B")]
    v = PanelVerdict(objections=objs, provenance=ReviewProvenance(
        author_model="z", reviewer_models=["A", "B"]),
        ensemble_spread_ceiling=TIER_SPECULATIVE_MAX)
    score, _ = v.apply(0.90)
    assert score < 0.90


def test_numeric_spread_ceiling_wired_through(tmp_path):
    from agp.adversary import DISAGREEMENT_CEILING
    v = PanelVerdict(provenance=ReviewProvenance("a", ["b"]))
    clamped, reason, recs = apply_panel_verdict(
        0.92, v, evaluations=[0.9, 0.4])
    assert clamped <= DISAGREEMENT_CEILING and "disagreement" in reason.lower()
    assert recs == []


# ── 4. PER-CRITIC CALIBRATION KEYED BY MODEL ────────────────────────────────

def test_calibration_by_model_separates_critics(tmp_path):
    led = AdversaryLedger(str(tmp_path / "d.jsonl"))
    led.record_objection(_ob(model="harsh-critic", text="h"))
    led.record_objection(_ob(model="soft-critic", text="s"))
    led.record_resolution("c1", claim_was_correct=True)   # both WRONG
    by_model = led.calibration_by_model()
    assert by_model["harsh-critic"]["precision_of_attack"] == 0.0
    assert by_model["soft-critic"]["n_scored"] == 1


def test_per_model_per_domain_profile(tmp_path):
    """'Harsh on financial claims, soft on scientific ones' becomes measurable."""
    led = AdversaryLedger(str(tmp_path / "d.jsonl"))
    for cid, dom, correct in [("f1", "FINANCIAL", False),
                              ("f2", "FINANCIAL", False),
                              ("s1", "TECHNICAL", True)]:
        led.record_objection(AdversaryObjection(cid, "attack", severity="MINOR",
                                                model="m", claim_domain=dom))
        led.record_resolution(cid, claim_was_correct=correct)
    fin = led.calibration_by_model(domain="FINANCIAL")["m"]
    tech = led.calibration_by_model(domain="TECHNICAL")["m"]
    assert fin["precision_of_attack"] == 1.0 and fin["verdict"] == "well_calibrated"
    assert tech["precision_of_attack"] == 0.0 and tech["verdict"] == "too_harsh"


def test_unscoreable_model_shows_insufficient_data_not_a_ratio(tmp_path):
    led = AdversaryLedger(str(tmp_path / "d.jsonl"))
    led.record_objection(_ob(model="quiet", text="q"))
    # no resolution → not in all_resolved → absent entirely (no flattering ratio)
    assert "quiet" not in led.calibration_by_model()
    led.record_resolution("c1", claim_was_correct=True, scoreable=False)
    entry = led.calibration_by_model()["quiet"]
    assert entry["n_scored"] == 0 and entry["verdict"] == "insufficient_data"


def test_attack_records_claim_domain_for_routing(tmp_path):
    adv = _adv(tmp_path, "mX", objections=[
        {"kind": "selection_effect", "severity": "MINOR", "text": "t"}])
    obs = asyncio.run(adv.attack("c9", "concl", ["e"]))
    assert obs[0].claim_domain == ""
    # domain tagging happens at the panel layer:
    panel = AdversaryPanel([adv])
    v = asyncio.run(panel.attack("c9", "concl", ["e"], claim_domain="FINANCIAL"))
    assert all(o.claim_domain == "FINANCIAL" for o in v.objections)


# ── 5. HONEST DEGRADATION ───────────────────────────────────────────────────

def test_single_self_model_backend_still_works_and_marks_itself(tmp_path):
    panel = AdversaryPanel([_adv(tmp_path, "only-model")])
    v = asyncio.run(panel.attack("c1", "claim", ["e"], author_model="only-model"))
    assert v.provenance.mode == "self_review"
    score, reason = v.apply(0.85)          # clean review still caps honestly
    assert score <= SELF_REVIEW_CEILING


def test_single_independent_backend_still_works(tmp_path):
    panel = AdversaryPanel([_adv(tmp_path, "different-model")])
    v = asyncio.run(panel.attack("c1", "claim", ["e"], author_model="author-model"))
    assert v.provenance.mode == "independent_review"
    score, reason = v.apply(0.85)
    assert score == 0.85 and reason == ""


def test_no_backends_reported_honestly_when_unattributed(tmp_path):
    """Reviewer model unknown → '(unattributed)', conservative: not counted as
    independent corroboration of the author."""
    panel = AdversaryPanel([_adv(tmp_path, "", objections=[])])
    v = asyncio.run(panel.attack("c1", "claim", ["e"], author_model="author"))
    assert v.provenance.reviewer_models == ["(unattributed)"]
    assert v.provenance.mode == "self_review"


# ── PROPERTY-BASED ASYMMETRY INVARIANTS ─────────────────────────────────────

sev = st.sampled_from(["BLOCKING", "MAJOR", "MINOR"])
kinds = st.sampled_from(["refuting_evidence", "alternative_explanation",
                         "selection_effect", "false_positive"])
models = st.sampled_from(["auth", "ind1", "ind2", "ind3", ""])


@st.composite
def verdict_strategy(draw):
    n = draw(st.integers(min_value=0, max_value=6))
    objs = [draw(st.builds(AdversaryObjection,
                           claim_id=st.just("p"),
                           text=st.just(f"obj-{i}"),
                           kind=kinds, severity=sev, model=models))
            for i in range(n)]
    reviewers = draw(st.lists(models, min_size=0, max_size=4, unique=True))
    spread = draw(st.one_of(st.none(),
                            st.floats(0, 1, allow_nan=False, allow_infinity=False)))
    return PanelVerdict(
        objections=objs,
        provenance=ReviewProvenance("auth", reviewers),
        disagreements=[],
        ensemble_spread_ceiling=spread)


score_strategy = st.floats(0, 1, allow_nan=False, allow_infinity=False)


@settings(max_examples=500, deadline=None)
@given(score=score_strategy, v=verdict_strategy())
def test_property_panel_apply_never_raises(score, v):
    out, _ = v.apply(score)
    assert out <= max(0.0, min(1.0, round(score, 2))) + 1e-12


@settings(max_examples=500, deadline=None)
@given(score=score_strategy, v=verdict_strategy())
def test_property_output_bounded_and_reason_consistent(score, v):
    out, reason = v.apply(score)
    assert 0.0 <= out <= 1.0
    if v.has_blocking:
        assert reason  # a veto always says why
    elif out >= round(max(0.0, min(1.0, score)), 2):
        # nothing was subtracted → there must be no penalty path fired
        assert not v.unanimous_unrebutted or v.objections == []
        assert v.provenance.ceiling is None or score <= SELF_REVIEW_CEILING
    else:
        assert isinstance(reason, str)


@settings(max_examples=300, deadline=None)
@given(evals=st.lists(st.floats(0, 1, allow_nan=False, allow_infinity=False),
                      min_size=0, max_size=8),
       score=score_strategy)
def test_property_apply_panel_verdict_downward_only(evals, score):
    v = PanelVerdict(provenance=ReviewProvenance("a", []))
    out, _, _ = apply_panel_verdict(score, v, evaluations=evals)
    assert out <= max(0.0, min(1.0, score)) + 1e-12


@settings(max_examples=200, deadline=None)
@given(a=st.text(min_size=0, max_size=20), b=st.lists(st.text(min_size=0, max_size=20), max_size=5))
def test_property_provenance_ceiling_only_applies_to_self_review(a, b):
    prov = ReviewProvenance(a, b)
    if prov.mode == "self_review":
        assert prov.ceiling == SELF_REVIEW_CEILING
    else:
        assert prov.ceiling is None
