"""RED TEAM — H1/H3/H4: can irrelevant or duplicate evidence reach the set?

Sibling-hunt for the confirmed checkpoint-resume gate bypass: every path by
which bytes become evidence WITHOUT the relevance gate judging them, and
every way one document can masquerade as several.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.pipeline import engine as eng
from tools.pipeline.model import ScriptedModel
from tools.pipeline.retrieval import RelevanceGate, extract_text
from agp.provenance import ProvenanceLedger


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── H1a: engine resume replays stored fetches past the gate ───────────────


def test_resume_replays_fetches_without_reapplying_gate():
    """CONFIRMED SIBLING (the reported defect, reproduced structurally).

    On a resume, engine.run restores fetches from the checkpoint payload:

        fetches = [_fetch_from_payload(r) for r in f_oc.payload["fetches"]]

    and later hands them straight to _answer_leaf. Between restoration and
    ingestion there is NO call to RelevanceGate.judge — replay_ledger only
    verifies sha256(body)==content_sha256 (storage integrity), never gate
    provenance. A poisoned, stale, or legacy checkpoint payload therefore
    injects evidence the live gate would have rejected.
    """
    poison_body = '{"unrelated": "recipe for cake"}'
    ok, cov, why = RelevanceGate().judge(
        "semiconductor supply chain resilience", "",
        json.loads(poison_body))
    assert not ok, "precondition: live gate rejects this body"

    # Exactly the resume-path restore:
    fetch = eng._fetch_from_payload({
        "source_name": "openalex", "url": "https://api.openalex.org/w?x=1",
        "content_sha256": eng._sha(poison_body), "body": poison_body,
        "parsed": json.loads(poison_body), "question_id": "q1"})
    assert isinstance(fetch, eng.FetchResult)

    # Structural proof: no re-gating between restore and _answer_leaf.
    import inspect
    src = inspect.getsource(eng.ResearchPipeline.run)
    seg = src[src.index('f_oc.payload["fetches"]'):src.index("_answer()")]
    assert ".judge(" not in seg and "RelevanceGate" not in seg, (
        "if this fails, the resume path now re-gates — defect closed")

    # Functional proof: replay_ledger accepts the bytes unconditionally.
    from tools.pipeline.checkpoint import Checkpoint, replay_ledger
    led = ProvenanceLedger()
    ck = Checkpoint(key="k", run="r", stage="fetch_leaf", input_hash="h",
                    payload={"fetches": [{
                        "body": poison_body,
                        "url": "https://api.openalex.org/w?x=1",
                        "content_sha256": eng._sha(poison_body),
                        "source_name": "openalex", "primary": True}]})
    assert replay_ledger(led, [ck])["replayed"] == 1
    assert led.is_primary_bytes(poison_body), (
        "H1 CONFIRMED: rejected-irrelevant bytes are PRIMARY in the "
        "resumed run's ledger; assign_source_class will rate them at the "
        "PRIMARY ceiling downstream")


def test_checkpoint_payload_carries_no_admission_proof():
    """replay_ledger verifies storage integrity only. Nothing in the
    checkpoint schema records THAT THE GATE ADMITTED these bytes — no
    relevance score, no verdict hash — so 'admitted' is unfalsifiable on
    replay."""
    from tools.pipeline.checkpoint import Checkpoint
    ck = Checkpoint(key="k", run="r", stage="fetch_leaf", input_hash="h",
                    payload={"fetches": [{"body": "b", "url": "u",
                                          "content_sha256": eng._sha("b"),
                                          "source_name": "s"}]})
    d = json.dumps(ck.to_dict())
    for field in ("relevance", "gate", "coverage", "verdict", "admitted"):
        assert field not in d, (
            f"if '{field}' now appears, admission proof exists — re-check")


def test_legacy_checkpoint_with_no_rejections_field_restores_clean_trace():
    """_trace_from_payload degrades missing fields to empty — its own doc
    says 'never to everything was admitted'. But a LEGACY payload written
    before wave 4 (fetches present, no 'rejections' key) yields a trace
    with zero rejections AND all fetches restored: the resumed run looks
    like a run with nothing rejected, i.e. cleaner than the live run was."""
    from tools.pipeline.engine import _trace_from_payload
    t = _trace_from_payload("q1", {"fetches": [{"source_name": "s"}]})
    assert len(t.rejected) == 0
    assert t.independent_keys == set()
    # The engine then reports n_indep from... trace.independent_keys being
    # empty means it FALLS BACK to counting distinct source names:
    #   n_indep = len({f.source_name for f in fetches}) + sandbox
    # i.e. a legacy resume silently switches to the WEAKER independence
    # rule (per-name, not per-family/host) that I2 removed.


# ── H1b: HTTP 200 with an error body ─────────────────────────────────────


def test_error_body_with_200_passes_status_check_and_may_pass_gate():
    """A source returning 200 wrapping an API error reaches the gate as
    parsed JSON. If the message echoes the query terms (nearly all do:
    'no results found for <query>'), coverage clears the bar and the
    outage page becomes EVIDENCE."""
    q = "will the federal funds rate rise"
    err = {"error": True, "message":
           "query 'federal funds rate rise' returned no data: rate "
           "service unavailable, please retry the federal funds request"}
    ok, cov, why = RelevanceGate(min_coverage=0.25).judge(q, "", err)
    assert ok, (
        f"H1 CONFIRMED: HTTP-200 error body scored {cov:.0%} and would "
        f"enter the evidence set")


@pytest.mark.parametrize("err_text", [
    '{"status": "error", "detail": "invalid api key for works search '
        'about semiconductor supply chain resilience"}',
    '{"results": [], "meta": {"error": "quota exceeded while searching '
        'semiconductor supply chains"}}',
])
def test_error_shapes_are_never_detected_as_errors(err_text):
    ok, _, why = RelevanceGate().judge(
        "search semiconductor supply chains", "", json.loads(err_text))
    assert ok, f"H1 CONFIRMED ({err_text[:40]}...): {why}"


def test_gate_cannot_distinguish_empty_envelope_from_outage():
    """extract_text({'results': []}) == '' -> rejection with reason
    'covers 0%'. Correct per-item — but synthesis.classify_null renders a
    leaf of only such rejections as an HONEST LITERATURE NULL ('sources
    returned only irrelevant material'), when empty envelopes under load
    are the classic signature of a degraded API. See
    test_redteam_retr_selection_nulls.py for the conflation attack."""


# ── H4: keyword stuffing / topic-dismissing abstracts ────────────────────


def test_keyword_stuffing_beats_the_gate():
    """Coverage counts token PRESENCE, not support. A citation list, a
    journal TOC, or SEO spam naming every term passes at full coverage."""
    q = ("what explains the collapse in semiconductor supply chain "
         "resilience during export controls")
    stuffed = ("semiconductor supply chain resilience export controls: "
               "index of cross-references and citations for semiconductor, "
               "supply, chain, resilience, export, controls keywords")
    ok, cov, why = RelevanceGate().judge(q, "", {"title": stuffed})
    assert ok and cov >= 0.6, f"H4 CONFIRMED: stuffing scored {cov:.0%}"


def test_abstract_naming_the_topic_to_dismiss_it_scores_equal_to_a_study():
    """'No such effect exists' mentions every token and scores exactly
    like the paper that measures the effect. Coverage cannot rank."""
    q = "do semiconductor export controls reduce supply chain resilience"
    dismissive = {
        "title": "export controls do not reduce supply chain resilience",
        "abstract": "we examine semiconductor export controls and supply "
                    "chain resilience and conclude the question is "
                    "malformed"}
    ok, cov_d, _ = RelevanceGate().judge(q, "", dismissive)
    direct = {"title": "measuring the causal effect of semiconductor "
                       "export controls on supply chain resilience"}
    ok2, cov_m, _ = RelevanceGate().judge(q, "", direct)
    assert ok and ok2
    assert abs(cov_d - cov_m) < 0.35, "scores diverged — gate can rank"


# ── H3: the same document counted more than once ─────────────────────────


def _registry_two_hosts():
    from tools.sources.base import SourceSpec
    from tools.sources.registry import SourceRegistry, SourceAdapter

    reg = SourceRegistry()
    GOOD = {"results": [{"title": "semiconductor supply chain "
                                   "resilience"}]}

    def mk(name, base):
        spec = SourceSpec(name=name, base_url=base, description="d",
                          answers=("semiconductor supply chain resilience "
                                   "macro data",), tier=1)
        class A:
            def __init__(self, s): self.s = s
            def works_search(self, term="", limit=None):
                data, rec = self.s.get_json(spec.base_url + "/works?q=1")
                return data
        reg.register(SourceAdapter(spec, A))
    mk("openalex", "https://host-a.example")
    mk("gdelt", "https://host-b.example")   # same corpus, other host
    return reg


def test_same_document_mirror_host_counts_as_two_independent_sources():
    """One byte-identical document served by two hosts passes the gate
    twice, yields two FetchResults with the SAME content hash, and sets
    TWO independent keys: min_independent_sources=2 satisfied by one
    document mirrored across a CDN."""
    from tools.pipeline.retrieval import IterativeRetriever
    from agp.research_program import (EvidenceRequirement, QuestionKind,
                                      ResearchQuestion, SourceClassRank)

    body = json.dumps({"results": [{"title": "semiconductor papers "
                                              "supply chain"}]})
    def transport(url, headers):
        return 200, body          # same bytes from either host

    led = ProvenanceLedger()
    r = IterativeRetriever(registry=_registry_two_hosts(), ledger=led,
                           transport=transport, max_rounds=2,
                           max_sources_per_leaf=3,
                           use_planner=False,
                           generic_calls={  # legacy table: same corpus, two hosts
                               "openalex": ("works_search", ("term",),
                                            {"limit": 3}),
                               "gdelt": ("works_search", ("term",),
                                         {"limit": 3}),
                           })
    q = ResearchQuestion(
        text="semiconductor papers supply chain",
        kind=QuestionKind.DESCRIPTIVE, priority=0.5,
        evidence_requirements=EvidenceRequirement(
            min_source_class=SourceClassRank.SECONDARY,
            min_independent_sources=2))
    trace = r.retrieve(q, "papers", min_independent=2)
    if len(trace.admitted) < 2:
        print(f"note: only {len(trace.admitted)} admitted; "
              f"stop={trace.stop_reason}")
    hashes = {f.content_sha256 for f in trace.admitted}
    assert len(hashes) == 1 or len(trace.admitted) < 2
    if len(trace.admitted) >= 2:
        assert len(trace.independent_keys) >= 2, (
            "expected mirror inflation")
        print("H3 CONFIRMED: identical content hash satisfied "
              "min_independent_sources=2:", sorted(trace.independent_keys))


def test_identical_content_hashes_are_never_deduped_anywhere():
    """Structural statement of H3: neither retrieval.py, engine.py nor
    synthesis.py compares content_sha256 across items. The only consumers
    of content_sha256 are provenance bookkeeping and rejection records —
    duplicate detection does not exist."""
    import inspect
    import tools.pipeline.retrieval as R
    import tools.pipeline.engine as E
    import tools.pipeline.synthesis as S
    for mod in (R, E, S):
        src = inspect.getsource(mod)
        dup_lines = [ln.strip() for ln in src.splitlines()
                     if "content_sha256" in ln
                     and any(k in ln.lower()
                             for k in ("dedup", "seen", "duplicat"))]
        assert not dup_lines, (mod.__name__, dup_lines)


def test_truncated_evidence_content_breaks_provenance_and_collapses_docs():
    """engine._answer_leaf stores evidence content=f.body[:4000], but the
    ledger recorded the FULL body. Two consequences, both bad:
      1. assign_source_class(truncated) misses the primary observation ->
         long documents silently downgrade to INFERRED while short ones
         stay PRIMARY;
      2. two DIFFERENT documents identical in their first 4000 bytes
         collapse to identical Evidence.content — one document's
         provenance covers another."""
    led = ProvenanceLedger()
    long_doc = "x" * 5000 + "REAL-DISTINCTIVE-TAIL"
    led.record_tool_result("openalex_fetch", long_doc, primary=True,
                           urls=["https://h/w/1"])
    truncated = long_doc[:4000]
    assert not led.is_primary_bytes(truncated), (
        "CONFIRMED: evidence content the pipeline stores (body[:4000]) "
        "is NOT the bytes the ledger holds — provenance assignment "
        "downgrades every document longer than 4000 chars")
    impostor = "x" * 4000 + "TOTALLY-OTHER-DOCUMENT"
    assert impostor[:4000] == truncated, (
        "CONFIRMED: distinct documents sharing a 4000-char prefix are "
        "indistinguishable as Evidence.content")
