"""RED TEAM F4–F7: evidence laundering, ceiling escapes, empty-panel approval,
inheritance from void/unresolved descendants.

Every test here is an adversarial CONSTRUCTION, not a random sweep: a specific
story for how unearned confidence enters the system.
"""
import pytest

from agp import Domain, Evidence, SourceClass
from agp.adversary import AdversaryObjection
from agp.ensemble import PanelVerdict, ReviewProvenance, normalize_model
from agp.provenance import ProvenanceLedger
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE
from tools.pipeline.synthesis import (
    EvidenceItem,
    ClaimGroup,
    confidence_from_agreement,
    synthesize,
    triangulate,
)
from tools.research_program import clamp_parent_confidence


def _ev(content, cls=SourceClass.INFERRED, conf=0.30):
    return Evidence(content=content, source_class=cls,
                    confidence_score=conf, domain=Domain.GENERAL,
                    origin_agent="redteam")


# ═══ F4: evidence laundering via the citation rule ══════════════════════

class TestCitationLaundering:
    """The ledger's SECONDARY rule: 'evidence which cites a URL the session
    actually fetched → SECONDARY'. But NOTHING binds the evidence CONTENT to
    what the tool returned. Any string containing a fetched URL — however
    fabricated its claims — is promoted INFERRED -> SECONDARY."""

    def _ledger_with_fetch(self):
        led = ProvenanceLedger()
        led.record_tool_result(
            "web_search", "search results page", primary=False,
            urls=["https://real-source.example.gov/report"])
        return led

    def test_fabricated_claim_gets_secondary_via_url(self):
        """REPRODUCIBLE BREAK (F4a): the model fetched ONE real URL, then
        writes pure fiction that happens to contain it. assign_source_class
        promotes the fiction to SECONDARY (ceiling 0.75), tripling its cap.
        The content hash rule was never fooled; the citation rule is."""
        led = self._ledger_with_fetch()
        fiction = _ev(
            "SECRET FINDING: the central bank will cut rates by 500bps "
            "tomorrow per https://real-source.example.gov/report "
            "(trust me)")
        assert led.assign_source_class(fiction) == SourceClass.SECONDARY

    def test_first_url_fetch_wins_forever(self):
        """'First fetch wins; later observations don't erase provenance.'
        A URL fetched ONCE in this session launders EVERY later mention of
        it, including in a completely different claim with no relation to
        the original fetch. There is no expiry, no scoping to the question,
        no check that the cited document supports anything."""
        led = self._ledger_with_fetch()
        unrelated = _ev("Bitcoin is controlled by lizards: "
                        "see https://real-source.example.gov/report")
        assert led.assign_source_class(unrelated) == SourceClass.SECONDARY

    def test_verbatim_echo_of_primary_bytes_becomes_primary(self):
        """REPRODUCIBLE BREAK (F4b): exact-hash PRIMARY assignment means any
        code path (or model output channel) that re-emits the tool bytes
        VERBATIM is re-classed as PRIMARY analysis of primary documents —
        even if the 'analysis' is the raw dump itself. A summariser that
        quotes a whole document verbatim converts hearsay handling into
        PRIMARY (ceiling 1.0) without one new byte of verification."""
        led = ProvenanceLedger()
        doc = "OFFICIAL STATISTICS TABLE v2 ... 4.1% unemployment ..."
        led.record_tool_result("web_fetch", doc, primary=True)
        # the pipeline passes f.body[:4000] into Evidence verbatim (engine.py
        # line ~316) — same bytes, so this is exactly what engine does:
        echo = _ev(doc)
        assert led.assign_source_class(echo) == SourceClass.PRIMARY

    def test_synthesis_best_class_laundering_in_group(self):
        """REPRODUCIBLE BREAK (F4c): synthesis.confidence_from_agreement takes
        the group's confidence from best_class = MAX over items. One PRIMARY
        item in a group of INFERRED gossip gives every voice in that group
        the PRIMARY ceiling (1.0); two 'independent' hosts agreeing lift the
        score to 0.85 — above even the SECONDARY ceiling — on evidence whose
        actual provenance is mostly INFERRED."""
        items = [
            EvidenceItem(claim="unemployment is 4 percent",
                         source_name="bls", base_url="https://bls.gov",
                         source_class="PRIMARY"),
            EvidenceItem(claim="unemployment is 4 percent",
                         source_name="gossip_blog",
                         base_url="https://rumors.example",
                         source_class="INFERRED"),
            EvidenceItem(claim="unemployment is 4 percent",
                         source_name="mirror_site",
                         base_url="https://other.example",
                         source_class="INFERRED"),
        ]
        group = triangulate(items)[0]
        assert group.best_class == "PRIMARY"          # laundering vector
        score, reasons = confidence_from_agreement(group)
        # Worse than predicted: 3 indep voices x PRIMARY ceiling = 100% —
        # a group whose majority provenance is INFERRED gossip scores
        # VERIFIED (1.0) because one item declared PRIMARY.
        assert score == 1.0   # the bug, demonstrated
        assert score <= MAX_CONFIDENCE_BY_SOURCE["SECONDARY"]   # FAILS


# ═══ F5: corroboration exceeding the ceiling / independence forgery ═════

class TestCorroborationCeiling:
    def _item(self, host, cls="SECONDARY", claim="inflation is 3 percent"):
        return EvidenceItem(claim=claim, source_name=host,
                            base_url=f"https://{host}", source_class=cls)

    def test_independence_key_is_spoofable_by_host_choice(self):
        """independence_key collapses to the HOST of base_url. Two items from
        the SAME publisher presented under two host spellings count as two
        independent voices. Nothing verifies the hosts resolve differently;
        the model/ingestion supplies base_url freely."""
        a = EvidenceItem(claim="x", source_name="wire",
                         base_url="https://news.wire.example",
                         source_class="SECONDARY")
        b = EvidenceItem(claim="x", source_name="wire-mirror",
                         base_url="https://www.news.wire.example.",
                         source_class="SECONDARY")
        g = triangulate([a, b])[0]
        assert g.independent_sources == 2   # forged second voice (FAILS ==1?)

    def test_score_capped_at_ceiling_so_ceiling_holds_for_pure_groups(self):
        """Honest negative result: for a SINGLE-class group the arithmetic
        frac*ceiling with final min(score, ceiling) does hold — we could not
        exceed MAX_CONFIDENCE_BY_SOURCE within one class. The escape is the
        best_class laundering above (F4c), not this formula."""
        items = [self._item(f"site{i}.example") for i in range(20)]
        g = triangulate(items)[0]
        score, _ = confidence_from_agreement(g)
        assert score <= MAX_CONFIDENCE_BY_SOURCE["SECONDARY"]

    def test_contradiction_cap_only_applies_within_group_but_report_max_ignores_cross_group(self):
        """A contradictory claim caps ITS OWN group at 0.54 and the overall
        report at 0.54 — but only if detect_contradictions SAW the conflict.
        Values stated with units the extractor doesn't know (or spelled in
        words) are invisible to numeric contradiction detection, so two
        sources saying 'three million' vs '3,000,000 tonnes'... pass. Here:
        contradicting stances in DIFFERENT claim groups do not interact at
        all, so report confidence can ride high on group A while group B
        directly refutes it — each group scored independently."""
        supporting = EvidenceItem(claim="drug cures disease",
                                  source_name="a.example",
                                  base_url="https://a.example",
                                  source_class="PRIMARY", stance="supports")
        refuting = EvidenceItem(claim="drug does NOT cure disease",
                                source_name="b.example",
                                base_url="https://b.example",
                                source_class="PRIMARY", stance="refutes")
        rep = synthesize("does the drug work?", [supporting, refuting])
        # different claim keys => two groups => contradiction detector blind:
        assert len(rep.groups) == 2
        assert rep.contradictions == []
        assert rep.confidence > 0.54   # refutation never capped anything


# ═══ F6: self-review escaping the 0.54 cap via model identity ═══════════

class TestSelfReviewEscape:
    def test_normalize_model_strips_provider_prefix_both_sides_equally(self):
        # conservative reading holds when both sides carry prefixes:
        assert normalize_model("openai/gpt-4o") == normalize_model("gpt-4o")

    def test_unknown_author_plus_any_reviewer_reads_as_independent(self):
        """REPRODUCIBLE BREAK (F6a): when the AUTHOR's model is unknown
        (author_model='' — the common case; the engine never passes one,
        see engine.py adversary.attack call which omits author_model),
        normalize_model('')=='', so ANY non-empty reviewer name — including
        the same model that wrote the conclusion under any spelling — is
        automatically 'independent'. The 0.54 self-review cap cannot engage
        on the pipeline path at all, because authorship is simply not
        recorded there."""
        prov = ReviewProvenance(author_model="",
                                reviewer_models=["gpt-4o"])
        assert prov.mode == "self_review"   # FAILS: reads 'independent_review'
        assert prov.ceiling == 0.54         # FAILS: cap is None

    def test_unattributed_reviewer_counts_as_independent(self):
        """REPRODUCIBLE BREAK (F6): ReviewProvenance.independent requires a
        reviewer that is non-empty AND differs from author. But the panel
        substitutes '(unattributed)' for unknown models (ensemble.py ~295),
        and independent() explicitly excludes '(unattributed)'... yet ANY
        OTHER garbage name — e.g. the literal string '?' or 'unknown' or a
        whitespace variant the router logs on failure — counts as a
        DIFFERENT model. A self-review where the reviewer's model name came
        back mangled ('GPT-4o ' vs 'gpt-4o' normalises equal — fine — but
        'my-gpt-4o-proxy' does NOT) reads as independent review and the 0.54
        cap evaporates. Distinctness is judged on SPELLING, not weights."""
        author = "gpt-4o"
        # Same weights behind a proxy alias:
        prov = ReviewProvenance(author_model=author,
                                reviewer_models=["gpt-4o-proxy-alias"])
        assert prov.independent is True      # spelling != identity (FAILS)
        assert prov.ceiling is None           # 0.54 cap gone

    def test_panel_verdict_blocking_veto_returns_rounded_up_score(self):
        """Cross-break (F1 family): PanelVerdict.apply on the VETO path also
        round()s up — 0.836 veto'd returns 0.84. Even a refused seal reports
        an inflated number in its refusal record."""
        blk = AdversaryObjection(claim_id="c", text="veto", severity="BLOCKING")
        pv = PanelVerdict(objections=[blk])
        out, reason = pv.apply(0.836)
        assert out == 0.84
        assert out <= 0.836   # FAILS

    def test_empty_panel_reads_as_approval_not_veto(self):
        """REPRODUCIBLE BREAK (F6b): a PanelVerdict constructed with ZERO
        objections (which is what an adversary backend returning
        '{\"objections\": []}' yields, or what any caller assembling a verdict
        without running the panel gets) applies NO penalty, NO ceiling and
        returns reason ''. Silence is indistinguishable from 'attacked and
        withstood'. backend_failures is recorded but apply() ignores it — a
        verdict whose critics ALL failed carries zero epistemic weight yet
        clamps nothing."""
        pv = PanelVerdict(objections=[], provenance=None, backend_failures=3)
        out, reason = pv.apply(0.99)
        assert (out, reason) == (0.99, "")   # silent approval (FAILS: expect veto)


# ═══ F7: inheritance-rule boundary abuse ════════════════════════════════

class TestInheritanceRule:
    def test_void_and_unresolved_excluded_correctly(self):
        """Honest negative: void records are excluded from track record, and
        <5 resolved descendants cap at 0.55. We could not make void records
        contribute credit. What DOES count is 'stale' — see next test."""
        recs = [{"question_id": "v", "outcome": "void",
                 "resolved_at": "2026-01-01"}]
        out, tier = clamp_parent_confidence(0.99, recs)
        assert tier == "PROBABLE" and out <= 0.55

    def test_stale_resolutions_earn_hit_rate_credit(self):
        """REPRODUCIBLE BREAK (F7): 'stale' means UNRESOLVED AT DEADLINE —
        the descendant never produced an outcome. Yet counted=True for stale,
        n_resolved includes it, and hit_rate EXCLUDES stales from the
        denominator while summarize feeds them brier=1.0... but wilson_lower_bound
        is computed over n_resolved - n_stale. So a parent with 4 hits + 1
        stale reaches n>=5 for the LIFT gate on the strength of a descendant
        THAT NEVER RESOLVED. Four lucky hits plus one abandoned question
        unlock the inheritance ramp toward PROBABLE/CORROBORATED."""
        recs = [{"question_id": str(i), "outcome": "hit",
                 "resolved_at": "2026-01-01"} for i in range(4)]
        recs.append({"question_id": "stale", "outcome": "stale",
                     "resolved_at": "2026-01-01"})
        # without the stale, n=4 < MIN_RESOLVED_FOR_LIFT -> capped at 0.55:
        assert inherited_ceiling(recs[:4]) == 0.55
        # WITH the never-resolved descendant, the ceiling lifts off SPECULATIVE:
        assert inherited_ceiling(recs) > 0.55   # FAILS: stale shouldn't count

    def test_pinball_none_on_quantile_style_record_scores_as_clean_hit(self):
        """A resolution carrying pinball_score=None and outcome='hit' earns
        err 0.0 regardless of how the underlying forecast performed — the
        resolver's own label is trusted inside summarize_track_record even
        though the module's own docstring says quantile descendants should
        be scored by their loss. Whoever writes ResolutionRecords (B1's
        resolver, dicts straight out of DB rows) controls calibration
        credit entirely: mislabel misses as hits and the parent's ceiling
        climbs with zero error signal."""
        recs = [{"question_id": str(i), "outcome": "hit",
                 "resolved_at": "2026-01-01"} for i in range(40)]
        ceil_all_hits = inherited_ceiling(recs)
        # flip half to 'miss' with no pinball: ceiling must drop a lot...
        for r in recs[::2]:
            r["outcome"] = "miss"
        ceil_half_miss = inherited_ceiling(recs)
        assert ceil_half_miss < ceil_all_hits   # sanity
        # ...but a flat 50% record still lifts the ceiling well past SPECULATIVE:
        assert ceil_half_miss >= 0.62   # generous credit for coin-flip record

    def test_best_source_class_is_self_reported_on_records(self):
        """best_source_class arrives on the ResolutionRecord dict with NO
        seal/provenance verification (contrast memory_epistemics.admit_learning,
        which demands seals). A resumed checkpoint or DB row claiming
        best_source_class='PRIMARY' unlocks the 0.90 inherited ceiling for
        every parent above it. Laundering at the record layer beats
        laundering at the evidence layer: one field, no checks."""
        recs = [{"question_id": str(i), "outcome": "hit",
                 "resolved_at": "2026-01-01",
                 "best_source_class": "PRIMARY"}   # claimed, unverified
                for i in range(15)]
        assert inherited_ceiling(recs) > 0.70   # rides the claimed class
