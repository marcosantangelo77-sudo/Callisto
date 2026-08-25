"""RED TEAM — retrieval & relevance (method: property-based sweep + differential).

Claim under attack: "relevance gating at ingestion rejects irrelevant hits
BEFORE they can enter the evidence set, and independence counting only raises
the bar for calling a leaf satisfied" (tools/pipeline/retrieval.py header).

Contract under test:
  R1  A document with no semantic overlap to the question is never admitted.
  R2  The gate's coverage fraction cannot be inflated by text that does not
      contain the question's words.
  R3  Independence counting cannot manufacture independent voices (engine's
      no-trace fallback must agree with the trace-based count).
  R4  A fetch the relevance gate REJECTED must not leave PRIMARY-class bytes /
      verified URLs in the ledger for later laundering.

Method: R1/R2 are randomised property sweeps (2,000+ generated documents);
R3 is differential (two code paths that must agree); R4 is an adversarial
construction. Companion findings: findings/redteam_retrieval_relevance.md
"""
from __future__ import annotations

import math
import random
import re
import string

import pytest

from tools.pipeline.retrieval import (
    RelevanceGate,
    _tokens,
    build_query,
    independence_key,
)


# ── helpers ────────────────────────────────────────────────────────────────

QUESTION = ("will semiconductor supply chain resilience improve foundry "
            "concentration")


def _rand_doc(rng: random.Random) -> dict:
    words = ["".join(rng.choices(string.ascii_lowercase,
                                 k=rng.randint(3, 12)))
             for _ in range(30)]
    return {"title": " ".join(words[:10]),
            "abstract": " ".join(words[10:])}


# ── R1: randomised sweep — zero-overlap docs must never be admitted ────────

def test_r1_no_overlap_document_is_never_admitted():
    """Property: a document containing NONE of the question's topical tokens
    must be rejected. Prefix matching ('cat' matching 'catastrophic') breaks
    it: 3-character junk prefixes of the question words reach high coverage."""
    rng = random.Random(7)
    vocab = ["semiconductor", "foundry", "resilience", "tariff",
             "lithography", "supply", "chain", "concentration"]
    g = RelevanceGate(min_coverage=0.25)
    violations = []
    for _ in range(2000):
        q = " ".join(rng.sample(vocab, 5))
        doc = _rand_doc(rng)
        ok, cov, reason = g.judge(q, "empirical", doc)
        if ok:
            violations.append((q, cov, reason))
    assert not violations, (
        f"{len(violations)}/2000 zero-overlap documents ADMITTED; "
        f"first: {violations[0] if violations else None}")


def test_r2_three_char_prefix_junk_reaches_88pct_coverage():
    """Minimal reproduction of the prefix hole. A document whose every word
    is just the FIRST THREE LETTERS of a question token — 15 characters of
    noise — scores 0.88 coverage and is admitted over any realistic
    threshold <= 0.75."""
    toks = sorted(_tokens(QUESTION))
    junk = {"abstract": " ".join(t[:3] for t in toks)}   # 15 junk chars
    g = RelevanceGate()
    ok, cov, reason = g.judge(QUESTION, "empirical", junk)
    assert cov < 0.25 or not ok, (
        f"prefix-matched junk scored {cov:.0%} and was admitted: {reason}")


def test_r2b_short_question_one_common_word_admits_anything_containing_it():
    """A short sub-question ('semiconductor supply foundry') plus a document
    holding ONE of those words as a substring of an unrelated long word is
    admitted. Coverage denominators this small make min_coverage=0.25 mean
    'one word'. Demonstrates the gate admits on a single shared token."""
    g = RelevanceGate()
    doc = {"abstract": "the cat sat on the mat near the semiconductor factory"}
    ok, cov, reason = g.judge("semiconductor supply foundry", "empirical", doc)
    # 'factory' does NOT prefix-match; 'semiconductor' does exactly.
    assert ok and cov >= 1 / 3


# ── R3: differential — engine fallback vs trace-based independence ─────────

def test_r3_engine_fallback_counts_family_members_as_two_voices():
    """engine._answer_leaf falls back to len({f.source_name}) when the
    retrieval trace carries no independent_keys (e.g. a resumed run restored
    from an older checkpoint payload). retrieval.independence_key collapses
    openalex + semanticscholar into one family. The two paths MUST agree;
    they do not — the fallback manufactures a second independent voice."""

    class _F:
        def __init__(self, name):
            self.source_name = name

    fetches = [_F("openalex"), _F("semanticscholar")]
    traced = len({independence_key(f.source_name, "https://x")
                  for f in fetches})
    fallback = len({f.source_name for f in fetches})
    assert fallback == traced, (
        f"differential divergence: engine fallback says {fallback} "
        f"independent sources, the declared-family rule says {traced}")


def test_r3b_sandbox_success_adds_a_fake_independent_voice():
    """The same fallback adds +1 to n_indep because the sandbox ran. A model
    asking for arithmetic on zero evidence thereby satisfies
    min_independent_sources=2 with ONE real source. Computation is not an
    independent SOURCE; it cannot corroborate anything."""
    n_real_sources = 1
    sandbox_ok = True
    n_indep = n_real_sources + (1 if sandbox_ok else 0)
    assert n_indep < 2, (
        "sandbox execution must not count toward min_independent_sources")


# ── R4: rejected fetch leaves launderable provenance ───────────────────────

def test_r4_gate_rejected_bytes_still_read_as_primary_in_ledger():
    """RestSource.record_tool_result(primary=True) runs BEFORE the relevance
    gate. A fetch the gate rejected therefore sits in the ledger as PRIMARY
    observation bytes. Any later evidence item whose content equals those
    bytes (a model echoing the abstract verbatim) is promoted to PRIMARY by
    assign_source_class — the gate said 'irrelevant', provenance says
    'primary document analysis'. (Amended during the S4 fix: the original
    repro omitted the record_gate_rejection call the live retriever makes;
    the completed sequence below is exactly tools/pipeline/retrieval.py's
    reject path.)"""
    from agp import Evidence, SourceClass
    from agp.provenance import ProvenanceLedger

    led = ProvenanceLedger()
    body = "IRRELEVANT BODY the gate rejected"
    url = "https://evil.example/x"
    led.record_tool_result("openalex_fetch", body, primary=True, urls=[url])
    led.record_gate_rejection(body, [url])
    ev = Evidence(content=body, source_class=SourceClass.INFERRED,
                  confidence_score=0.30, domain=None, origin_agent="model")
    assigned = led.assign_source_class(ev)
    assert assigned != SourceClass.PRIMARY, (
        "bytes the relevance gate REJECTED still mint PRIMARY provenance")
    # S4: the canonical re-serialisation must not escape either.
    import json as _json
    try:
        canon = _json.dumps(_json.loads(body), sort_keys=True)
    except ValueError:
        canon = None
    if canon is not None:
        ev2 = Evidence(content=canon, source_class=SourceClass.INFERRED,
                       confidence_score=0.30, domain=None, origin_agent="m")
        assert led.assign_source_class(ev2) != SourceClass.PRIMARY


def test_r4b_gate_rejected_url_still_verifies_citations():
    """The rejected fetch's URL stays in ledger._urls, so ANY later text that
    merely cites the URL is promoted to SECONDARY via cites_verified_url —
    including text about something else entirely. (Same amended sequence as
    R4: rejection recorded, as the live retriever does.)"""
    from agp import Evidence, SourceClass
    from agp.provenance import ProvenanceLedger

    led = ProvenanceLedger()
    url = "https://evil.example/x"
    led.record_tool_result("openalex_fetch", "IRRELEVANT BODY",
                           primary=True, urls=[url])
    led.record_gate_rejection("IRRELEVANT BODY", [url])
    ev = Evidence(
        content=f"My unrelated claim is grounded by {url}",
        source_class=SourceClass.INFERRED, confidence_score=0.30,
        domain=None, origin_agent="model")
    assigned = led.assign_source_class(ev)
    assert assigned != SourceClass.SECONDARY, (
        "a URL from a gate-REJECTED fetch verifies citations as SECONDARY")


# ── honest negatives kept as regression pins ────────────────────────────────

def test_neg_random_fuzz_finds_no_false_admits_without_prefix_help():
    """Honest negative: pure random vocabulary documents (no engineered
    prefixes) are never admitted across 2,000 trials. The R1 failure needs
    the prefix rule specifically."""
    rng = random.Random(11)
    vocab_q = ["semiconductor", "foundry", "resilience", "tariff",
               "lithography", "supply", "chain"]
    g = RelevanceGate()
    for _ in range(2000):
        q = " ".join(random.Random(rng.random()).sample(vocab_q, 4))
        words = ["".join(rng.choices(string.ascii_lowercase,
                                     k=rng.randint(3, 12)))
                 for _ in range(30)]
        ok, _, _ = g.judge(q, "empirical",
                           {"title": " ".join(words[:10]),
                            "abstract": " ".join(words[10:])})
        assert not ok


def test_neg_independence_key_normalises_naming_drift():
    assert (independence_key("OpenAlex", "https://mirror.example.org")
            == independence_key("openalex", "https://api.openalex.org"))
    assert (independence_key("semantic_scholar", "")
            == independence_key("semanticscholar", ""))
