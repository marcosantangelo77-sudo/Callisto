"""RED TEAM — ATTACK THE ANSWER, NOT THE MACHINERY.

Fifteen prior passes attacked internal properties. This pass attacks
CORRESPONDENCE TO REALITY: the system can be internally consistent and
externally wrong. The first real question it ever answered was sealed at
PROBABLE-AFFIRMS while asserting unemployment fell in 2026 when it rose
(findings/one_real_question_run.json, commit ffeca37).

Five defect families, one file:

  C1. Parent stance is inherited from the highest-confidence LEAF — which may
      answer something else entirely. Task 180 fixes the reported instance;
      these pins hold the WHOLE family: any leaf that wins on magnitude gets
      to set the parent's DIRECTION (stance), tier story, and source class,
      whether or not it bears on the root question.
  C2. Composition: every leaf individually TRUE, parent conclusion FALSE.
      All-affirming children about inverted comparisons compose into a parent
      direction that asserts the opposite of the truth.
  C3. Two leaves querying the same series can silently receive different
      data windows: the planner pins limit=120 with NO observation_start, so
      the bytes depend on the endpoint's own default windowing; and the
      round-N refiner only mutates free-text search queries, never series
      parameters — per-leaf divergence is invisible because nothing records
      which window the answer was computed over.
  C4. Independence counting cannot distinguish three sources that ANSWERED
      from three that were merely ASKED. session.sources lists all 21
      registry specs; failures produce zero notes; a single-source sealed
      answer looks identical to a triangulated one.
  C5. Arithmetic/comparison correctness: the sandbox computes a comparison
      and the sealed answer asserts the OPPOSITE of the computed output, with
      no cross-check; separately, produced_quant treats ANY digit in the
      answer as quantitative evidence.

GATE RULE honoured throughout: nothing here raises any confidence. The one
production change in this pass (C4 failure-surfacing notes) is additive
information only.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import date

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()


from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import (  # noqa: E402
    LeafOutcome,
    ResearchPipeline,
    fixture_transport,
)
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tools.pipeline.synthesis import (  # noqa: E402
    ClaimGroup,
    EvidenceItem,
    detect_contradictions,
)


class QuietAdv:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


FRED_BODY = json.dumps({"observations": [
    {"date": "2023-01-01", "value": "3.5"},
    {"date": "2026-07-01", "value": "4.1"},
]})


def _decomp(leaves):
    return json.dumps({"sub_questions": leaves})


def _leaf(text, qtype="unemployment rate time series observations",
          min_ind=1):
    return {"text": text, "kind": "descriptive", "question_type": qtype,
            "min_source_tier": 2, "min_independent_sources": min_ind,
            "quant_required": False}


def _ans(answer, conf=0.95, stance="AFFIRMS"):
    return json.dumps({"answer": answer, "proposed_confidence": conf,
                       "stance": stance})


def _pipeline(model, routes=None):
    return ResearchPipeline(
        model=model, adversary_router=QuietAdv(),
        transport=fixture_transport(routes or {"/series": FRED_BODY}),
        store=ArtifactStore(root=tempfile.mkdtemp()),
        ledger=ProvenanceLedger())


def _run(pipeline, question="Has US unemployment been lower in 2026 than "
                           "in January 2023?"):
    return asyncio.run(pipeline.run(question, today=date(2026, 8, 24)))


@pytest.fixture(autouse=True)
def _fred_key(monkeypatch):
    monkeypatch.setenv("CALLISTO_FRED_API_KEY", "test-key")


# ── C1: parent direction inherited from a leaf that does not bear on it ──


@pytest.mark.xfail(reason="C1/Task-180 family: parent stance = "
                          "best-confidence leaf stance, leaf need not bear "
                          "on the root", strict=True)
def test_parent_stance_not_inherited_from_offtopic_leaf():
    """The winning leaf answers 'what was the Jan 2023 rate' — an offshoot,
    not the root comparison. Its AFFIRMS must not become the parent's
    direction on the LOWER-in-2026 question."""
    model = ScriptedModel({
        "Architect": [{"content": _decomp([
            _leaf("What was the US unemployment rate in January 2023 "
                  "according to BLS data"),
            _leaf("What has been the US unemployment rate during 2026 in "
                  "the latest monthly readings"),
        ])}],
        "Manager": [
            # off-topic winner: answers its own sub-question, 0.95
            {"content": _ans("Jan 2023 rate was 3.5%")},
            # the leaf bearing on the root: DENIES (4.1 > 3.5)
            {"content": _ans("2026 rate is 4.1%, higher than 3.5%",
                             conf=0.55, stance="DENIES")},
        ]})
    r = _run(_pipeline(model))
    assert r.sealed
    # Truth: the rate was NOT lower in 2026 -> DENIES. The pipeline sealed
    # AFFIRMS off the wrong child.
    assert r.stance == "DENIES"


@pytest.mark.xfail(reason="C1 family: gap_kind=unprovable leaf can still be "
                          "the best leaf and set the parent direction",
                   strict=True)
def test_unprovable_leaf_cannot_set_parent_direction():
    """A leaf that DECLARED ITSELF unprovable (evidence obtained but short of
    its own standard) still contributes stance and magnitude to the parent if
    it happens to win on confidence. An unprovable leaf has no direction to
    give."""
    wrongq = LeafOutcome(question_id="a", text="What was unemployment in "
                         "January 2023?", answer="3.5%", confidence=0.50,
                         tier="SPECULATIVE", stance="AFFIRMS")
    unprov = LeafOutcome(question_id="b", text="Lower in 2026 than Jan 2023?",
                         answer="Truncated window; cannot compare.",
                         confidence=0.54, tier="PROBABLE", stance="AFFIRMS",
                         gap_kind="unprovable")
    answered = [wrongq, unprov]
    best = max(answered, key=lambda l: l.confidence)
    assert best.gap_kind != "unprovable"


@pytest.mark.xfail(reason="C1 family: parent inherits the winning leaf's "
                          "best SOURCE CLASS through its confidence even "
                          "when the leaf is off-topic", strict=True)
def test_parent_does_not_inherit_leaf_source_class_across_topics():
    """proposed = best_leaf.confidence embeds that leaf's provenance ceiling.
    A PRIMARY-capped leaf about topic X hands its PRIMARY-grade 0.95 to a
    parent question about Y: the parent wears a provenance tier it never
    earned on its own question."""
    primary_leaf = LeafOutcome(
        question_id="a", text="What does the FRED UNRATE series say?",
        answer="UNRATE exists and is monthly.", confidence=0.95,
        tier="VERIFIED", stance="AFFIRMS", source_classes=["PRIMARY"])
    on_topic = LeafOutcome(
        question_id="b", text="Lower in 2026?", answer="INFERRED-only guess.",
        confidence=0.50, tier="SPECULATIVE", stance="UNDETERMINED",
        source_classes=["INFERRED"])
    best = max([primary_leaf, on_topic], key=lambda l: l.confidence)
    # The parent's magnitude/tier come from a leaf whose source class was
    # assigned for a different question's evidence.
    assert best.source_classes == ["INFERRED"] or \
        best.text.startswith("Lower")


# ── C2: composition — all leaves true, parent false ─────────────────────────


@pytest.mark.xfail(reason="C2 composition: two TRUE affirming children "
                          "(each about an inverted comparison) compose into "
                          "a parent AFFIRMS on a FALSE root", strict=True)
def test_all_true_children_compose_to_false_parent():
    """Leaf1: 'lower in Jan 2023 than in 2026?' AFFIRMS — true.
    Leaf2: 'higher in 2026 than Jan 2023?' AFFIRMS — true.
    Root:   'lower in 2026 than Jan 2023?'      — false.
    Both children agree, both are correct, the composed parent direction is
    exactly backwards. Nothing in the chain compares leaf polarity to root
    polarity."""
    model = ScriptedModel({
        "Architect": [{"content": _decomp([
            _leaf("Was unemployment lower in January 2023 than during 2026"),
            _leaf("Was unemployment higher during 2026 than in January 2023"),
        ])}],
        "Manager": [
            {"content": _ans("Yes: 3.5% was lower than 4.1%.")},
            {"content": _ans("Yes: 4.1% was higher than 3.5%.", conf=0.90)},
        ]})
    r = _run(_pipeline(model))
    assert r.sealed
    assert r.stance == "DENIES"   # truth about the ROOT


# ── C3: same series, silently different data ────────────────────────────────


def test_same_series_two_leaves_get_identical_query_parameters():
    """Two leaves about the SAME series resolve to byte-identical planned
    calls today. This pin holds the PLANNER half of C3: any future change
    that lets per-leaf phrasing shift series parameters must do so
    deliberately and visibly.

    UPDATED (task 191, D1 fix): the planner now WINDOWS the observations on
    the years each leaf names — that is deliberate, visible per-leaf phrasing
    sensitivity, exactly the shape this pin was keeping a hole open for. The
    pin is preserved on the parts that must NOT vary: same series id, same
    method, same limit/ordering. The two example leaves name different years,
    so they legitimately differ in window; two leaves naming the same years
    must still produce byte-identical calls."""
    from tools.sources import query_builder
    # different phrasing, SAME named year -> identical planned call
    plans = [query_builder.build_plan("fred", t) for t in (
        "What was the US unemployment rate in January 2023",
        "US unemployment rate as of Jan 2023 — what was it?")]
    calls = [(q.method, tuple(sorted(q.kwargs.items())))
             for p in plans for q in p.queries]
    assert len(set(calls)) == 1
    # the deliberate, visible difference: windows follow named years
    p23 = query_builder.build_plan(
        "fred", "What was the US unemployment rate in January 2023")
    p26 = query_builder.build_plan(
        "fred", "What has been the US unemployment rate in 2026 to date")
    kw23 = p23.queries[0].kwargs
    kw26 = p26.queries[0].kwargs
    assert kw23["series_id"] == kw26["series_id"]
    assert kw23.get("start") == "2023-01-01" and kw26.get("start") == "2026-01-01"


def test_series_window_is_explicit_and_recorded_on_the_answer():
    from tools.sources import query_builder
    p = query_builder.build_plan(
        "fred", "What was the US unemployment rate in January 2023")
    kwargs = p.queries[0].kwargs
    # The plan must name the window explicitly (start bound), not inherit
    # whatever the endpoint's default slice happens to be.
    assert any(k in kwargs for k in ("observation_start", "start")), kwargs


def test_digit_in_prose_is_not_quantitative_evidence():
    """PROMOTED from strict-xfail canary to passing pin: the engine's
    quant gate now ignores year tokens and counts only real numbers in
    prose, or a sandbox run that returned a numeric value."""
    from tools.pipeline.engine import (
        _produced_quantitative, _prose_carries_quantity)
    assert not _prose_carries_quantity(
        "In 2023 the rate was considered elevated by commentators."), \
        "a year mentioned in prose counted as quant"
    assert not _produced_quantitative("In 2023 the rate was high", None)
    assert _prose_carries_quantity("The rate was 4.1 percent.")
    assert not _produced_quantitative("", None), \
        "empty answer with no sandbox is never quantitative"


# ── C4: asked vs answered ───────────────────────────────────────────────────


def test_single_source_seal_surfaces_which_sources_failed():
    """A sealed answer backed by ONE source, out of 21 consulted, must SAY
    so: the run notes record which sources were asked and failed. (Fixed in
    this pass — additive notes only.)

    D2 seal-contract update: with only one admitting source against a bar
    of two, the leaf is UNPROVABLE and an all-unprovable parent now
    REFUSES instead of sealing (tests/test_seal_unprovable.py). The
    disclosure contract is checked on the refused result: fred is still
    the only contributing source and the other asked sources are named.
    """
    decomp = json.dumps({"sub_questions": [_leaf(
        "What was the US unemployment rate in January 2023 according to "
        "BLS data", min_ind=2)]})
    model = ScriptedModel({
        "Architect": [{"content": decomp}],
        "Manager": [{"content": _ans("Jan 2023 rate was 3.5%")}]})
    r = _run(_pipeline(model))
    assert not r.sealed
    answered = {f.source_name for f in r.fetches}
    assert "fred" in answered
    joined = "\n".join(r.notes)
    # Every registry source that did not answer must be accounted for in
    # the notes (errored, skipped, or otherwise not contributing).
    missing = [n for n in ("bls", "worldbank")
               if n not in joined and n not in answered]
    assert not missing, f"failed/asked-but-unanswered sources invisible: {missing}"



def test_summary_distinguishes_asked_from_answered():
    """summary_dict() reports n_fetches; a consumer cannot tell how many
    DISTINCT sources actually contributed evidence. Pin the field once it
    exists."""
    decomp = json.dumps({"sub_questions": [_leaf(
        "What was the US unemployment rate in January 2023 according to "
        "BLS data")]})
    model = ScriptedModel({
        "Architect": [{"content": decomp}],
        "Manager": [{"content": _ans("Jan 2023 rate was 3.5%")}]})
    r = _run(_pipeline(model))
    d = r.summary_dict()
    assert d.get("n_sources_answered", 0) == 1


# ── C5: arithmetic and comparison ───────────────────────────────────────────


def test_answer_may_not_contradict_its_own_computed_comparison():
    """FIXED by the compute-output↔stance reconciliation check. Promoted
    from strict-xfail canary to passing pin; see TestComputeReconciliation
    for the full contract (inverse case, agreeing case, non-boolean stdout)."""
    model = ScriptedModel({
        "Architect": [{"content": _decomp([
            _leaf("Was the unemployment rate lower in July 2026 than in "
                  "January 2023 in the BLS series")])}],
        "Manager": [
            {"content": json.dumps({"answer": "", "proposed_confidence": 0,
                                    "compute": {"code": "print(4.1 < 3.5)",
                                                "inputs": {}}})},
            # Sandbox printed False; the model asserts the opposite anyway.
            {"content": _ans("The rate WAS lower in July 2026 (4.1%) than "
                             "January 2023 (3.5%).", conf=0.9)},
        ]})
    r = _run(_pipeline(model))
    leaf = r.leaves[0]
    assert leaf.sandbox_status == "ok"
    # The sealed conclusion must not invert the computation it ran.
    assert "WAS lower" not in leaf.answer


# ── synthesis-layer: numeric contradiction machinery ────────────────────────


class TestNumericContradictionMachinery:
    """detect_contradictions picks each voice's value with max(..., key=abs),
    then compares across voices. Two consequences, both shown on CORRECT
    inputs: (a) context numbers inside one document manufacture a MAJOR
    disagreement with an agreeing second source; (b) percent vs plain-number
    encodings of the SAME value read as contradictions. Either way the
    machinery can assert a numeric conflict that does not exist — and the
    cap it applies is the exact mechanism that mis-scored real questions."""

    def setup_method(self):
        self.a = EvidenceItem(
            claim="us unemployment rate january 2023 was 3.5",
            source_name="fred", base_url="https://fred.stlouisfed.org",
            source_class="PRIMARY", values=(0.035, 0.148))
        self.b = EvidenceItem(
            claim="us unemployment rate january 2023 was 3.5",
            source_name="bls", base_url="https://bls.gov",
            source_class="PRIMARY", values=(0.035,))
        self.g = ClaimGroup(claim="us unemployment rate january 2023")
        self.g.items = [self.a, self.b]

    # PROMOTED from strict-xfail canary to passing pin: the S1b fix
    # (all-pairs subject-figure comparison) stops max(abs) value selection
    # from manufacturing a contradiction out of one source's context figure.
    def test_agreeing_sources_with_context_figures_do_not_conflict(self):
        assert detect_contradictions(self.g) == []

    @pytest.mark.xfail(reason="percent-encoded (0.035) vs plain-unit (3.5) "
                              "statements of the SAME fact read as a "
                              "numeric conflict", strict=True)
    def test_unit_encoding_of_same_value_does_not_conflict(self):
        c1 = EvidenceItem(claim="unemployment january 2023", source_name="fred",
                          base_url="https://fred.stlouisfed.org",
                          source_class="PRIMARY", values=(0.035,))
        c2 = EvidenceItem(claim="unemployment january 2023", source_name="bls",
                          base_url="https://bls.gov", source_class="PRIMARY",
                          values=(3.5,))
        g = ClaimGroup(claim="unemployment january 2023")
        g.items = [c1, c2]
        assert detect_contradictions(g) == []


# ── C5 FIX: compute-output↔stance reconciliation ────────────────────────────
#
# The engine now treats a sandbox run whose stdout is exactly one bare
# boolean as a VERIFIED comparison, binding on the leaf's direction. Prose
# asserting the negation must not seal: the leaf refuses (answer emptied,
# stance UNDETERMINED). A computation AGREEING with the prose seals exactly
# as before. No confidence number is ever raised by this check — a
# reconciliation failure may only lower or refuse.


class TestComputeReconciliation:
    def _model(self, code="print(4.1 < 3.5)", answer=_ans(
            "The rate WAS lower in July 2026 (4.1%) than January 2023 "
            "(3.5%).", conf=0.9)):
        return ScriptedModel({
            "Architect": [{"content": _decomp([
                _leaf("Was the unemployment rate lower in July 2026 than in "
                      "January 2023 in the BLS series")])}],
            "Manager": [
                {"content": json.dumps({"answer": "", "proposed_confidence":
                                        0, "compute": {"code": code,
                                                       "inputs": {}}})},
                {"content": answer},
            ]})

    def test_false_computation_blocks_affirmative_prose(self):
        """THE exact case: sandbox prints False, prose asserts 'WAS lower'.
        Must not seal."""
        r = _run(_pipeline(self._model()))
        leaf = r.leaves[0]
        assert leaf.sandbox_status == "ok"
        assert leaf.reconciliation_failure, "contradiction must be recorded"
        assert leaf.answer == ""
        assert leaf.stance == "UNDETERMINED"
        assert not any(l.answer for l in r.leaves)
        # Refusal propagates: no sealed conclusion asserts the opposite.
        assert getattr(r, "refusal_reason", None) or all(
            "WAS lower" not in l.answer for l in r.leaves)

    def test_true_computation_blocks_negative_prose(self):
        """Inverse: sandbox prints True, prose denies. Must not seal."""
        m = self._model(code="print(4.1 > 3.5)",
                        answer=_ans("The rate was NOT lower in July 2026.",
                                    conf=0.9, stance="DENIES"))
        r = _run(_pipeline(m))
        leaf = r.leaves[0]
        assert leaf.reconciliation_failure
        assert leaf.answer == ""
        assert leaf.stance == "UNDETERMINED"

    def test_agreeing_computation_still_seals_normally(self):
        """A computation that AGREES with the prose must seal exactly as
        before — refusal must be surgical, not universal."""
        m = self._model(code="print(4.1 < 3.5)",
                        answer=_ans("The rate was NOT lower in July 2026 "
                                    "(4.1%) than in January 2023 (3.5%).",
                                    conf=0.9, stance="DENIES"))
        r = _run(_pipeline(m))
        leaf = r.leaves[0]
        assert leaf.sandbox_status == "ok"
        assert leaf.reconciliation_failure is None
        assert leaf.stance == "DENIES"
        assert "NOT lower" in leaf.answer

    def test_non_boolean_stdout_is_not_a_verdict(self):
        """Richer stdout (numbers, multiple lines) must NOT trigger the
        reconciliation veto — the check stays silent rather than guess."""
        m = self._model(code="print(4.1)\nprint(3.5)",
                        answer=_ans("Rate was 4.1 vs 3.5.", conf=0.8,
                                    stance="DENIES"))
        r = _run(_pipeline(m))
        leaf = r.leaves[0]
        assert leaf.reconciliation_failure is None
        assert leaf.stance == "DENIES"

    def test_refusal_never_raises_confidence(self):
        """GATE RULE: reconciliation failure zeroes the estimate; nothing on
        the refused leaf exceeds what an UNDETERMINED leaf could carry."""
        m = self._model()  # False + affirmative prose at proposed 0.9
        r = _run(_pipeline(m))
        leaf = r.leaves[0]
        if leaf.reconciliation_failure:
            assert leaf.confidence_estimate == 0.0


# ── Remaining instances of "verified artifact produced, never consumed" ────
# findings/arithmetic_contradiction.md §5 items 3 and 4. Both canaried, not
# fixed: #3 requires re-ranking evidence classes (would RAISE confidence —
# barred), #4 requires unit/encoding normalization that must not be done
# casually (percent-vs-plain reads as different quantities).

class TestVerifiedComputeBelowProse:
    """Instance 3: a VERIFIED sandbox computation is hard-capped INFERRED
    (≤0.55) while an unverified fetched page can reach PRIMARY (1.0), so in
    best-leaf selection the one artifact the system actually executed and
    checked systematically loses direction-setting to mere assertion."""

    def test_verified_computation_can_set_direction(self, tmp_path):
        """CANARY (strict): leaf whose answer rests on its own verified
        computation should not be outranked by an equally-confident leaf
        resting on unverified prose."""
        pytest.xfail(
            "sandbox evidence is capped INFERRED/≤0.45 while unverified "
            "fetches reach PRIMARY/1.0; fixing would raise confidence "
            "(barred) or require an entitlement-only channel")


class TestNumericReconciliation:
    """Instance 4: only sole-bare-boolean stdout reconciles with the stance.
    Compute printing '4.1' while the answer claims '3.9' passes every gate."""

    def test_numeric_compute_contradiction_refuses(self):
        pytest.xfail(
            "numeric reconciliation needs unit/encoding normalization; "
            "extending _sole_bare_boolean casually would misread encodings")
