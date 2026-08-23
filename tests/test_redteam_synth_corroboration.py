"""RED TEAM — synthesis and corroboration (tools/pipeline/synthesis.py and the
engine/retro paths that consume or should consume it).

Surface choice: no prior redteam file attacks the combiner — the stage where
evidence items become ONE score. The module's own docstring states the
invariants under attack:

  GATE RULE: "nothing here raises any ceiling"; a contradiction "caps the
  group at SPECULATIVE (0.54)"; nulls are classified honestly.

Canary convention (repo standard): each CONFIRMED defect has a test whose
assertion FAILS against current code. Honest negatives PASS and are pinned.
Run:  python3 -m pytest tests/test_redteam_synth_corroboration.py -q
"""
import random

import pytest

from tools.pipeline.synthesis import (
    NULL_LITERATURE,
    NULL_RETRIEVAL,
    ClaimGroup,
    EvidenceItem,
    claim_key,
    classify_null,
    confidence_from_agreement,
    detect_contradictions,
    extract_values,
    synthesize,
    triangulate,
)


def _item(claim="c", i=0, cls="PRIMARY", stance="", values=(),
          source_name=None):
    host = source_name or f"h{i}"
    return EvidenceItem(
        claim=claim, source_name=host,
        base_url=f"https://{host}.example",
        source_class=cls, stance=stance, values=tuple(values))


# ═══ S1 — unanimous REFUTATION scores VERIFIED 1.0 ═══════════════════════

class TestRefutationScoresLikeAgreement:
    """confidence_from_agreement counts INDEPENDENT SOURCES AGREEING. It never
    asks WHAT they agree on. Three independent PRIMARY sources each stating
    stance='refutes' — every voice in the group says the claim is FALSE —
    produce the maximum possible score, identical to three voices supporting
    it. The single most damning evidence structure in the system ('everyone
    independent who speaks on this refutes it') is the highest-scoring one."""

    def _group(self, stance, cls="PRIMARY", n=3):
        return triangulate([
            EvidenceItem(claim="the drug cures the disease",
                         source_name=f"journal{i}", base_url=f"https://j{i}.example",
                         source_class=cls, stance=stance)
            for i in range(n)])[0]

    def test_unanimous_refutes_scores_1_point_0(self):
        g = self._group("refutes")
        score, reasons = confidence_from_agreement(g)
        # THE BUG: refutation unanimity == corroboration unanimity
        assert score == 1.0            # FAILS: must be ~0 (or refused)
        assert score <= 0.54           # FAILS: cannot read as SPECULATIVE+

    def test_refutes_equals_supports_score_for_score(self):
        s, _ = confidence_from_agreement(self._group("supports"))
        r, _ = confidence_from_agreement(self._group("refutes"))
        assert s != r                  # FAILS: identical

    def test_report_confidence_from_all_refuted_group(self):
        rep = synthesize("does the drug work?", [
            EvidenceItem(claim="drug cures disease", source_name=f"h{i}",
                         base_url=f"https://h{i}.example",
                         source_class="PRIMARY", stance="refutes")
            for i in range(3)])
        assert rep.confidence < 0.55   # FAILS: report says 1.0


# ═══ S2 — engine parent confidence ignores synthesis contradictions ══════

class TestEngineIgnoresContradictions:
    """engine.run advances through CONTRADICTION_CHECK but nothing populates
    session.contradictions on the pipeline path (only orchestrator.py:1103
    calls add_contradiction). The parent conclusion's confidence is the MAX
    leaf confidence; leaf confidence (_answer_leaf) never consults
    detect_contradictions either. A pipeline run with screaming numeric
    contradictions between its own leaves seals at the best leaf's score."""

    def test_pipeline_contradiction_step_is_a_no_op(self):
        import inspect
        callers = []
        for mod in ("tools.pipeline.engine", "tools.pipeline.retro"):
            m = __import__(mod, fromlist=["x"])
            callers.append("add_contradiction" in inspect.getsource(m))
        # the only production caller of AGPSession.add_contradiction is
        # orchestrator.py; neither pipeline module ever adds one, so the
        # CONTRADICTION_CHECK step the pipeline advances through is dead:
        assert any(callers) is True     # FAILS: both False -> dead step

    def test_synthesize_contradiction_never_reaches_engine_leaf_score(self):
        # Direct demonstration at the data level: two leaves, one supports one
        # refutes the same underlying fact via differently-keyed claims (the
        # known cross-group blind spot) — engine takes max leaf confidence.
        sup = EvidenceItem(claim="inflation is 4 percent",
                           source_name="a", base_url="https://a.example",
                           source_class="SECONDARY")
        ref = EvidenceItem(claim="inflation is 9 percent",
                           source_name="b", base_url="https://b.example",
                           source_class="SECONDARY")
        rep = synthesize("what is inflation?", [sup, ref])
        # even when detection WORKS inside one group...
        assert len(rep.groups) == 1
        assert len(rep.contradictions) == 1
        # ...the capped number lives only in SynthesisReport; engine._answer_leaf
        # computes out.confidence from best_class + requirements + adversary and
        # never reads this. Documented by absence: engine.py has zero references
        # to SynthesisReport / group_confidences.
        import tools.pipeline.engine as E
        esrc = inspect.getsource(E) if (inspect := __import__("inspect")) else ""
        assert "SynthesisReport" in esrc or "synthesize(" in esrc   # FAILS


# ═══ S3 — round() raises across tier boundaries in the live engine ═══════

class TestEngineRoundCrossesTierBoundary:
    """The historical rounding bug (round(0.836,2)==0.84), fixed via
    floor_conf in thresholds/adversary/research_program — but engine.py:466
    still does round(max(0.0, clamped), 2). clamped can be any float up to
    the ceiling; round() maps 0.5455 -> 0.55 = TIER_PROBABLE_MIN, minting a
    PROBABLE tier the raw score did not earn. Same duplicated-logic class as
    F1/F2: the fix landed in the shared helper but not this call site."""
    from agp.thresholds import TIER_CORROBORATED_MIN, TIER_PROBABLE_MIN

    @pytest.mark.parametrize("raw,minted_tier_floor", [
        (0.5455, TIER_PROBABLE_MIN),
        (0.54999, TIER_PROBABLE_MIN),
        (0.74551, TIER_CORROBORATED_MIN),
        (0.749999, TIER_CORROBORATED_MIN),
    ])
    def test_round_mints_tier_not_earned(self, raw, minted_tier_floor):
        from agp import ConfidenceTier
        out = round(max(0.0, raw), 2)          # engine.py:466 verbatim
        assert out < minted_tier_floor          # FAILS: round crosses up

    def test_random_sweep_finds_upward_rounds(self):
        rng = random.Random(20260823)
        ups = [v for v in (rng.uniform(0.30, 1.0) for _ in range(20000))
               if round(v, 2) > v]
        assert not ups                          # FAILS: ~thousands of cases


# ═══ S4 — contradiction detection false negatives ════════════════════════

class TestContradictionBlindSpots:
    """detect_contradictions picks ONE value per independence unit:
    by_ikey.setdefault (first stated wins) then max(values, key=abs).
    Both choices hide disagreements."""

    def test_self_contradicting_publisher_is_one_voice(self):
        """A publisher stating two wildly different numbers contributes ONE
        voice — whichever value came FIRST in its text. Its internal
        disagreement is dropped, and if the first value agrees with the
        counterpart the pair reads as unanimous."""
        a = _item(i=0, values=(100.0,))
        agreeing_first = _item(i=1, values=(100.0, 10.0))
        disagreeing_only = _item(i=1, values=(10.0,))
        assert detect_contradictions(triangulate([a, disagreeing_only])[0])
        # same publisher, honest figure second: NO contradiction detected,
        # group scores as full agreement:
        cons = detect_contradictions(triangulate([a, agreeing_first])[0])
        assert cons                              # FAILS: none detected
        score, _ = confidence_from_agreement(
            triangulate([a, agreeing_first])[0],
            detect_contradictions(triangulate([a, agreeing_first])[0]))
        assert score <= 0.54                     # FAILS: 0.85 (2 indep voices)

    def test_stance_conflict_suppressed_by_one_two_faced_source(self):
        """`not (set(sup) & set(ref))`: a SINGLE independence unit holding BOTH
        stances disables stance-contradiction detection for the whole group —
        even with five clean supporters vs five clean refuters."""
        items = ([EvidenceItem(claim="c", source_name=f"s{i}",
                               base_url=f"https://s{i}.example",
                               source_class="SECONDARY", stance="supports")
                  for i in range(5)]
                 + [EvidenceItem(claim="c", source_name=f"r{i}",
                                 base_url=f"https://r{i}.example",
                                 source_class="SECONDARY", stance="refutes")
                    for i in range(5)]
                 + [EvidenceItem(claim="c", source_name="fence",
                                 base_url="https://fence.example",
                                 source_class="SECONDARY", stance="supports"),
                    EvidenceItem(claim="c", source_name="fence",
                                 base_url="https://fence.example",
                                 source_class="SECONDARY", stance="refutes")])
        cons = detect_contradictions(triangulate(items)[0])
        stance_cons = [c for c in cons if c.kind == "stance"]
        assert stance_cons                      # FAILS: none detected

    def test_word_numbers_invisible_to_numeric_detection(self):
        """'three million' vs '3 million' — spelled-out magnitudes carry no
        value, so a 3x numeric dispute between PRIMARY sources detects as
        nothing and the group keeps full corroboration credit."""
        a = _item(i=0, cls="PRIMARY")
        b = _item(i=1, cls="PRIMARY")
        a.claim = b.claim = "reserves are three million tonnes"
        assert extract_values(a.claim) == ()     # extractor blind (by design?)
        g = triangulate([a, b])[0]
        assert detect_contradictions(g) == []    # silent pass


# ═══ S5 — junk claims form a full-credit group ═══════════════════════════

class TestEmptyClaimGroup:
    """claim_key("") == (). Every item with an empty/whitespace/punctuation-
    only claim collapses into ONE group and earns mutual 'corroboration'.
    Claim text comes from the model/extractor; nothing rejects vacuous ones."""

    def test_empty_claims_corroborate_each_other(self):
        junk = [_item(claim="", i=i) for i in range(3)]
        rep = synthesize("q", junk)
        assert len(rep.groups) == 1
        assert rep.confidence == 1.0            # FAILS: junk is not corroboration
        assert rep.confidence <= 0.30           # FAILS

    def test_punctuation_only_claim_joins_the_void(self):
        assert claim_key("... !!!") == ()
        assert claim_key("—") == claim_key("")


# ═══ S6 — retrodiction binary reads like a horoscope ═════════════════════

class TestLeansYesSentinelScan:
    """retro.PipelineResearcher._leans_yes decides P(True)=0.5±conf/2 by
    substring scan over the WHOLE conclusion — which embeds every leaf's
    question TEXT. Any negation phrase anywhere flips the sign of the
    prediction; absence of the six phrases means YES regardless of content."""

    def _leans(self, conclusion):  # pragma: no cover — removed helper stub
        raise NotImplementedError

    def test_question_text_flips_prediction(self):
        from tools.pipeline.retro import PipelineResearcher
        # A leaf QUESTION containing a listed phrase flips the sign of the
        # whole binary prediction, regardless of what the answers said:
        concl = ("Does the drug NOT improve survival?\n"
                 "- [PROBABLE 0.55] trial question: mortality fell 30 percent "
                 "in the treated arm; evidence is strong")
        assert PipelineResearcher._leans_yes(concl) is False   # FAILS: True

    def test_affirmative_wording_of_negative_answer_scores_yes(self):
        from tools.pipeline.retro import PipelineResearcher
        # Any negative answer not phrased with one of the six magic phrases
        # reads as YES and pushes P(True) UP by conf/2:
        assert PipelineResearcher._leans_yes(
            "we found no support whatsoever for the claim") is False  # FAILS: True
        assert PipelineResearcher._leans_yes(
            "the evidence argues against the hypothesis") is False    # FAILS: True


# ═══ S7 — classify_null trusts a rejected list nobody verified ═══════════

class TestNullClassificationSpoofable:
    """classify_null derives 'honest literature null' from trace fields. A
    trace with rounds=[] but a non-empty rejected list — trivially produced
    by a crashed/misbuilt trace, or a resumed payload — classifies as
    LITERATURE_NULL ('sources were queried...') though NOTHING was queried.
    The exact conflation the module exists to prevent, reachable without
    touching the rejected reasons themselves."""

    def _trace(self, rounds, rejected, stop=""):
        class R: pass
        recs = []
        for r in rejected:
            o = R(); o.source_name, o.reason, o.relevance_score = r
            recs.append(o)
        class T: pass
        t = T(); t.rounds = rounds; t.rejected = recs; t.stop_reason = stop
        return t

    def test_no_rounds_but_rejections_reads_literature_null(self):
        v = classify_null(self._trace(rounds=[], rejected=[("fred", "off topic", 0.1)],
                                      stop="budget"))
        assert v.status == NULL_RETRIEVAL       # FAILS: NULL_LITERATURE
        assert v.is_honest_null is False        # FAILS

    def test_honest_path_still_works(self):
        # pin: gate rejections WITH real rounds remain a literature null
        v = classify_null(self._trace(
            rounds=[{"sources": [{"name": "openalex", "admitted": 0,
                                  "rejected": 2}]}],
            rejected=[("openalex", "irrelevant", 0.2)]))
        assert v.status == NULL_LITERATURE


# ═══ HONEST NEGATIVES (pass; pinned as regression) ═══════════════════════

class TestHeldInvariants:
    def test_contradiction_never_raises_score_random_sweep(self):
        rng = random.Random(7)
        for _ in range(5000):
            n = rng.randint(1, 6)
            cls = rng.choice(["PRIMARY", "SECONDARY", "SIGNAL", "INFERRED"])
            items = [_item(i=i, cls=cls,
                           values=(rng.uniform(-100, 100),)) for i in range(n)]
            g = triangulate(items)[0]
            cons = detect_contradictions(g)
            s_wo, _ = confidence_from_agreement(g, ())
            s_w, _ = confidence_from_agreement(g, cons)
            if cons:
                assert s_w <= min(s_wo, 0.54) + 1e-12
            else:
                assert abs(s_w - s_wo) < 1e-12

    def test_volume_from_one_publisher_is_worth_one_voice(self):
        items = [_item(i=0) for _ in range(25)]      # same host, 25 copies
        g = triangulate(items)[0]
        assert g.independent_sources == 1
        score, _ = confidence_from_agreement(g)
        solo, _ = confidence_from_agreement(triangulate([items[0]])[0])
        assert score == solo

    def test_score_never_exceeds_own_class_ceiling_for_single_class_groups(self):
        rng = random.Random(11)
        for cls, ceil_ in (("PRIMARY", 1.0), ("SECONDARY", 0.75),
                           ("SIGNAL", 0.55), ("INFERRED", 0.55)):
            for _ in range(200):
                n = rng.randint(1, 8)
                g = triangulate([_item(i=k, cls=cls) for k in range(n)])[0]
                s, _ = confidence_from_agreement(g)
                assert s <= ceil_
