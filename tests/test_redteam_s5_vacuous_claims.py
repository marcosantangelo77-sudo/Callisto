"""S5 — vacuous claims must not form a corroborating group.

claim_key("") == (): every item whose claim has no content words collapsed
into ONE group, so three junk items read as three INDEPENDENT VOICES and the
group scored confidence 1.0. A claim with no content words cannot
corroborate anything — junk is not corroboration.

Canary convention: defect-repro tests FAIL against unfixed code; honest
negatives PASS.
"""
import pytest

from tools.pipeline.synthesis import (
    EvidenceItem,
    claim_key,
    confidence_from_agreement,
    synthesize,
    triangulate,
)


def _item(claim: str, name: str, cls: str = "PRIMARY") -> EvidenceItem:
    return EvidenceItem(claim=claim, source_name=name,
                        base_url=f"https://{name}", source_class=cls)


# ── boundary: a short but REAL claim must survive ────────────────────────

def test_short_real_claim_groups_and_scores():
    assert claim_key("GDP fell") == ("fell", "gdp")
    groups = triangulate([_item("GDP fell", "a"), _item("gdp FELL.", "b")])
    assert len(groups) == 1 and groups[0].independent_sources == 2
    score, _ = confidence_from_agreement(groups[0])
    assert score == 0.85  # two independent PRIMARY voices, unchanged


# ── the defect: junk items collapsing into one full-credit group ─────────

@pytest.mark.parametrize("junk", ["", "   ", "... — !!!"])
def test_vacuous_claim_keys_are_empty(junk):
    assert claim_key(junk) == ()


def test_stopwords_only_claim_is_vacuous():
    # "the of and" carries no content words either
    assert claim_key("the of and") == ()
    assert claim_key("IS was BE") == ()


def test_three_junk_items_do_not_form_a_corroborating_group():
    items = [_item("", "a"), _item("   ", "b"), _item("... — !!!", "c")]
    groups = triangulate(items)
    # nothing that scores corroboration may come out of pure junk
    for g in groups:
        score, reasons = confidence_from_agreement(g)
        assert score <= 0.30, (g.claim, score, reasons)


def test_junk_and_real_claims_stay_separate():
    items = [_item("", "a"), _item("GDP fell", "b")]
    real = [g for g in triangulate(items) if g.claim == "GDP fell"]
    assert len(real) == 1
    score, _ = confidence_from_agreement(real[0])
    assert score == 0.70  # one voice: the junk item did not join the group


def test_stopword_only_group_never_reaches_full_credit():
    g = triangulate([_item("the of and", "a"), _item("is was be", "b")])
    assert not g or confidence_from_agreement(g[0])[0] <= 0.30


def test_synthesis_report_confidence_not_raised_by_junk():
    rep = synthesize("q?", [_item("", "a"), _item("   ", "b"),
                            _item("...", "c")])
    assert rep.confidence <= 0.30


def test_fix_cannot_lower_a_real_two_voice_score():
    # guard against an over-broad fix that drops real evidence
    g = triangulate([_item("inflation eased", "a"),
                     _item("Inflation eased.", "b")])
    assert len(g) == 1
    assert confidence_from_agreement(g[0])[0] == 0.85
