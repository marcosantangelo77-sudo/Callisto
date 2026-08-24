"""RED TEAM — synthesis & corroboration (method: cross-module + adversarial
constructions, following the property sweep of the previous pass).

Claim under attack (tools/pipeline/synthesis.py header): "corroboration may
raise a score only up to the ceiling the source class already permits; a live
contradiction caps the group at SPECULATIVE (0.54)"; and "Ten documents from
one publisher move nothing; two independent sources agreeing is worth more
than ten dependent ones."

Contract under test:
  S1  A live contradiction on a claim must lower that claim's score to <= 0.54.
  S2  Dependent volume must not raise confidence above what one voice earns.
  S3  The report's overall confidence may not exceed any single honest group.
  S4  A null leaf must never read as an honest literature finding when the
      'sources' were never authoritative.

Findings S1–S3 are demonstrated with runnable failing tests; honest negative
results are pinned at the end. Companion: findings/redteam_retrieval_relevance.md
"""
from __future__ import annotations

import pytest

from tools.pipeline.synthesis import (
    EvidenceItem,
    SynthesisReport,
    classify_null,
    claim_key,
    confidence_from_agreement,
    detect_contradictions,
    extract_values,
    synthesize,
    triangulate,
)


def _item(claim, name, cls="SECONDARY", values=(), stance="",
          url_host=None):
    return EvidenceItem(
        claim=claim, source_name=name,
        base_url=f"https://{url_host or name}.org", source_class=cls,
        values=tuple(values), stance=stance)


# ── S1: contradiction cap evaded by claim phrasing ─────────────────────────

def test_s1_contradiction_hidden_when_values_live_in_claim_text():
    """Numeric disagreement phrased inside the claim text is never seen:
    EvidenceItem.values defaults to () and nothing in the pipeline extracts
    numbers from `claim` (extract_values runs only on fetch bodies). Two
    items stating 60% vs 20% group into ONE group — the keys match because
    '60'/'20' are dropped as sub-3-char tokens — yet no contradiction fires
    and the group scores full agreement confidence."""
    a = _item("TSMC market share is 60 percent", "srcA")
    b = _item("TSMC market share is 20 percent", "srcB")
    rep = synthesize("q", [a, b])
    assert rep.confidence <= 0.54, (
        f"60% vs 20% stated in one grouped claim: confidence "
        f"{rep.confidence}, {len(rep.contradictions)} contradictions")


def test_s1a_contradiction_hidden_by_claim_key_fragmentation():
    """The same numeric value phrased with different words ('sixty' vs
    'twenty') fragments into two separate groups: no contradiction is
    detected anywhere in the report, and synthesize().confidence takes the
    MAX over groups — the disagreement is not surfaced at all."""
    a = _item("TSMC market share is sixty percent", "srcA")
    b = _item("TSMC market share is twenty percent", "srcB")
    rep = synthesize("q", [a, b])
    assert len(rep.groups) == 1 or rep.confidence <= 0.54, (
        f"contradictory claims fragmented into {len(rep.groups)} groups; "
        f"report confidence {rep.confidence} with "
        f"{len(rep.contradictions)} contradictions surfaced")


def test_s1b_contradiction_hidden_by_max_abs_value_cherry_pick():
    """detect_contradictions compares max(values, key=abs) per item. When a
    document mentions several numbers (a 60% share AND $9bn revenue), the
    biggest-magnitude number is picked as 'the' value:
      * genuine agreement on the share can be masked by revenue figures that
        also roughly agree -> NO contradiction where one exists;
      * conversely two fully-agreeing documents become a MAJOR contradiction
        because their unrelated big numbers differ."""
    # genuine contradiction on the share, hidden by agreeing big numbers:
    a = _item("c", "A", "PRIMARY", values=(0.6, 9e9))
    b = _item("c", "B", "PRIMARY", values=(0.2, 9.05e9))
    cons = detect_contradictions(triangulate([a, b])[0])
    assert any(c.kind == "numeric" for c in cons), (
        "share contradiction (0.6 vs 0.2) completely hidden by "
        "similar large revenue numbers")

    # agreement on everything relevant, contradicted by unrelated numbers:
    a2 = _item("c", "A", "PRIMARY", values=(0.6, 5e9))
    b2 = _item("c", "B", "PRIMARY", values=(0.62, 4e9))
    cons2 = detect_contradictions(triangulate([a2, b2])[0])
    assert not cons2, (
        f"agreeing sources turned into {len(cons2)} contradiction(s) by "
        "unrelated numbers mentioned in the same documents")


def test_s1c_stance_contradiction_requires_explicit_labels():
    """Stance defaults to '' (unspecified). A refuting item facing only
    unspecified-stance items raises NO contradiction, so a PRIMARY voice plus
    an INFERRED refutation reads as full agreement at 0.85. Silence is
    treated as support."""
    sup = _item("c", "A", "PRIMARY")                    # stance ""
    ref = _item("c", "B", "INFERRED", stance="refutes")
    rep = synthesize("q", [sup, ref])
    assert rep.confidence <= 0.54, (
        f"unstated stance treated as support: confidence {rep.confidence}, "
        f"{len(rep.contradictions)} contradictions")


# ── S2: dependent volume / best-class contagion ────────────────────────────

def test_s2_one_primary_item_lifts_the_whole_group_ceiling():
    """best_class is the MAX over items. One PRIMARY mention grouped with
    weak INFERRED items grants the whole group a 1.0 ceiling; with three
    independence units the group scores 1.0 even though two of three voices
    are INFERRED/SECONDARY."""
    g = triangulate([
        _item("X is 5", "a", "INFERRED"),
        _item("X is 5", "b", "SECONDARY"),
        _item("X is 5", "c", "PRIMARY"),
    ])[0]
    score, reasons = confidence_from_agreement(g)
    assert score <= 0.75, (
        f"one PRIMARY item lifted a mixed-quality group to {score}")


def test_s2b_ten_mirrors_of_one_primary_doc_score_1_0():
    """Ten copies of one PRIMARY document under ten distinct source names
    (mirrors of the same document) count as TEN independent voices and reach
    the maximum confidence 1.0 — exactly the 'volume is corroboration'
    failure the module docstring disclaims, because independence keys off
    the declared source NAME/host, which mirrors control."""
    items = [_item("TSMC has 60% share", f"mirror{i}", "PRIMARY")
             for i in range(10)]
    g = triangulate(items)[0]
    score, _ = confidence_from_agreement(g)
    assert score <= 0.70, (
        f"ten name-swapped copies of ONE document scored {score} "
        f"(independent_sources={g.independent_sources})")


# ── S3: overall confidence is a max over groups ────────────────────────────

def test_s3_report_confidence_is_max_over_groups():
    """synthesize() sets report.confidence = max(group scores). One lucky
    PRIMARY group outweighs every other group regardless of how poorly the
    rest of the question was supported; the parent conclusion then inherits
    this via engine's best-leaf rule. Overall confidence should reflect the
    whole evidence structure, not its best corner."""
    good = _item("primary says x is 5", "gov", "PRIMARY")
    junk = _item("unrelated y is 7", "blog", "INFERRED")
    junk2 = _item("another z is 9", "blog2", "INFERRED")
    rep = synthesize("a broad question with several sub-claims",
                     [good, junk, junk2])
    assert rep.confidence <= 0.55, (
        f"overall confidence {rep.confidence} driven entirely by the single "
        "best group while the rest of the evidence is INFERRED filler")


# ── S4: hostile mirror manufactures an 'honest literature null' ────────────

def test_s4_gate_rejections_from_unauthoritative_sources_read_as_literature_null():
    """classify_null treats 'rejected with reasons from reachable sources'
    as NULL_LITERATURE — 'the literature does not address it'. But reachability
    is just HTTP 200: a hostile or low-quality mirror answering irrelevant
    content to every query produces a verdict that reads as an authoritative
    absence-of-evidence finding, with no record of WHO was asked."""
    class R:
        def __init__(s, n):
            s.source_name = n
            s.reason = "covers 0%"
            s.relevance_score = 0.0

    class T:
        rejected = [R("evil-mirror"), R("evil-mirror2")]
        rounds = [{"sources": [
            {"name": "evil-mirror", "rejected": "..."},
            {"name": "evil-mirror2", "rejected": "..."}], "admitted": 0}]
        stop_reason = "terminator: stagnant"

    v = classify_null(T())
    assert v.status != "literature_null" or "authoritative" in v.explanation \
        or "registry-selected" in v.explanation, (
        "gate rejections from arbitrary hosts reported as an authoritative "
        "literature null without naming the sources' standing")


# ── honest negatives kept as regression pins ────────────────────────────────

def test_neg_percent_normalisation_keeps_units_comparable():
    assert extract_values("60%") == (0.6,)
    assert extract_values("0.6") == (0.6,)
    assert extract_values("1,234 billion") == (1234e9,)


def test_neg_within_tolerance_values_do_not_contradict():
    a = _item("c", "A", "PRIMARY", values=(0.60,))
    b = _item("c", "B", "PRIMARY", values=(0.62,))
    assert not detect_contradictions(triangulate([a, b])[0])


def test_neg_identical_claim_key_groups_for_contradiction():
    assert claim_key("tsmc market share percent") \
        == claim_key("tsmc market share percent")
