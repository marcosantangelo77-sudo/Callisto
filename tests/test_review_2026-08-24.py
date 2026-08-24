"""REVIEW RUN 11 (2026-08-24, reviewer: ox-alpha) — failing reproductions.

Every test here FAILS against master 96e09c9 as committed. Each documents one
verified defect found reviewing the batch3 merges (crossrun-memory,
cmefedfut/market-implied, fix/broken-sources, laundering/artifact remainder)
and the money/claims paths they re-exposed. No production code was edited.

Families per PATTERNS.md:
  F1  verification layer that never runs / cannot fail
  F2  fix landed in one copy while another keeps the bug
  F3  absence treated as success
  F6  rounding whose error direction raises the number
  F7  tests passing for the wrong reason / red pins merged red
"""
from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tests.helpers.no_socket import NoSocket
_nosocket = NoSocket()
_nosocket.install()


# ===========================================================================
# CRITICAL R11-1 — ClaimStore hash chain does not detect tampering of the
# LAST journal line (and a single-entry journal is entirely unprotected).
# Family #1: the "chain check" only compares entry["prev"] to the digest of
# the PREVIOUS line; nothing ever verifies a line's own recorded state.
# agp/claims.py::load — rewriting history in the newest entry loads cleanly.
# Repro of tests/test_lifecycle_claim.py failure that has been red since
# ba0a63c merged it.
# ===========================================================================

def _make_claim_with_journal(tmp_path: Path, n_saves: int):
    from agp.claims import ClaimStore, Claim
    from agp.preregistration import Criteria, Preregistration
    from agp.research_program import Domain
    from tools.pipeline.model import ScriptedModel  # noqa: F401  (env pin)
    crit = Criteria(confirm_markers=["replicated effect observed"],
                    refute_markers=["effect absent"],
                    min_evidence_items=2, min_source_class="SECONDARY")
    pre = Preregistration(query="Does the mechanism hold?", criteria=crit)
    pre.seal()
    from agp.claims import Claim as C
    c = C(text="Durable claim")
    c.seal_preregistration(pre)
    from agp.evidence import Evidence  # noqa: F401
    return store_claim(c, tmp_path, n_saves)


def _store_claim(claim, tmp_path: Path, n_saves: int):
    from agp.claims import ClaimStore
    store = ClaimStore(str(tmp_path / "claims"))
    store.save(claim)
    loaded = store.load(claim.claim_id)
    for _ in range(n_saves - 1):
        loaded.attach_evidence(
            __import__("agp", fromlist=["Evidence"]).Evidence(
                content="additional observation",
                source_class=__import__("agp", fromlist=["SourceClass"]).SourceClass.INFERRED,
                confidence_score=0.5,
                domain=__import__("agp", fromlist=["Domain"]).Domain.GENERAL,
                origin_agent="test"),
            note="more")
        store.save(loaded)
        loaded = store.load(claim.claim_id)
    return store, claim


@pytest.mark.filterwarnings("ignore")
def test_r11_01_last_line_tamper_is_detected(tmp_path):
    """Tampering with the LATEST journal entry must raise on load.

    Current behaviour: load() replays happily with confidence=0.95 /
    status='confirmed' — exactly the self-flattering history rewrite the
    module docstring claims is impossible ('tampered claim never silently
    loads'). A single-entry journal (the common case for a fresh claim) has
    NO protection at all."""
    from agp.claims import Claim, ClaimError, ClaimStore
    from agp.preregistration import Criteria, Preregistration
    crit = Criteria(confirm_markers=["m"], refute_markers=["r"],
                    min_evidence_items=2, min_source_class="INFERRED")
    pre = Preregistration(query="q", criteria=crit)
    pre.seal()
    c = Claim(text="Durable claim")
    c.seal_preregistration(pre)

    store = ClaimStore(str(tmp_path / "claims"))
    store.save(c)                      # single-line journal
    path = Path(store._journal_path(c.claim_id))
    lines = path.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["state"]["confidence"] = 0.95
    entry["state"]["status"] = "confirmed"
    lines[0] = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ClaimError, match="amper"):
        store.load(c.claim_id)


@pytest.mark.filterwarnings("ignore")
def test_r11_02_multi_entry_last_line_tamper_also_slips_through(tmp_path):
    """Even with a two-line journal, editing the LAST line is undetected:
    only line N+1's 'prev' field could catch line N's rewrite, and there is
    no line N+1."""
    from agp import Domain, Evidence, SourceClass
    from agp.claims import Claim, ClaimError, ClaimStore
    from agp.preregistration import Criteria, Preregistration

    crit = Criteria(confirm_markers=["m"], refute_markers=["r"],
                    min_evidence_items=2, min_source_class="INFERRED")
    pre = Preregistration(query="q2", criteria=crit)
    pre.seal()
    c = Claim(text="Two-entry claim")
    c.seal_preregistration(pre)
    store = ClaimStore(str(tmp_path / "claims"))
    store.save(c)
    loaded = store.load(c.claim_id)
    loaded.attach_evidence(
        Evidence(content="second observation",
                 source_class=SourceClass.INFERRED,
                 confidence_score=0.5, domain=Domain.GENERAL,
                 origin_agent="test"),
        note="n2")
    store.save(loaded)

    path = Path(store._journal_path(c.claim_id))
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    e = json.loads(lines[1])
    e["state"]["confidence"] = 0.99          # historical inflation
    lines[1] = json.dumps(e, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ClaimError, match="amper"):
        store.load(c.claim_id)


# ===========================================================================
# HIGH R11-3 — prereg scoring silently uses AMENDED criteria by default.
# agp/preregistration.py::score — `using_amended` is True whenever ANY
# amendment exists, and effective_criteria returns the amendment even when
# the caller passed nothing. Scoring "against the seal" after an amend()
# therefore scores against post-hoc criteria while labelling the divergence.
# Red since ba0a63c merged test_lifecycle_claim.py; still red on 96e09c9.
# ===========================================================================

def test_r11_03_default_score_uses_sealed_originals_not_amendment():
    from agp.preregistration import Criteria, Preregistration, Verdict
    crit = Criteria(confirm_markers=["replicated effect observed"],
                    refute_markers=["effect absent in replication"],
                    ambiguous_markers=["conflicting results"],
                    min_evidence_items=2, min_source_class="SECONDARY")
    p = Preregistration(query="Does the mechanism hold?", criteria=crit)
    p.seal()
    p.amend(Criteria(confirm_markers=["new protocol confirms"],
                     refute_markers=["effect absent"],
                     min_evidence_items=1),
            reason="field protocol changed mid-study")

    out = p.score(observed_text="replicated effect observed",
                  evidence_count=2, best_source_class="SECONDARY")
    assert out.verdict is Verdict.CONFIRMED, (
        f"scoring WITHOUT naming criteria used the amendment "
        f"(used_amended={out.used_amendment}): {out.divergences[:1]}")
    assert out.used_amendment is False


# ===========================================================================
# HIGH R11-4 — cmefedfut W5 publication-date guard bypassed by its own
# convenience wrapper. attach_market_implied() wraps bare floats with
# allow_no_provenance=True, so an UNPROVENANCED benchmark dated anywhen
# attaches freely; separately, provenanced values attach when the question
# carries no claim_date. Family #1/#3: the guard's refusal branches are both
# escapable, and no test exercises the wrapper.
# tools/sources/cmefedfut.py::attach_market_implied
# ===========================================================================

def test_r11_04_wrapper_cannot_attach_unprovenanced_aftermarket_benchmark():
    from tools.sources.cmefedfut import attach_market_implied
    q = NS(question_id="q1", claim_date=date(2026, 1, 28), market_implied=None)
    skipped = attach_market_implied([q], {"q1": 0.9})
    assert q.market_implied is None, (
        "attach_market_implied attached an unprovenanced probability to a "
        f"question resolving later — W5 leakage guard bypassed (skipped={skipped})")


def test_r11_05_provenanced_future_benchmark_refused_without_claim_date():
    from tools.sources.cmefedfut import attach_from_derived
    d = {"probability_of_change": 0.9,
         "derived_from": {"class": "PRIMARY", "trade_date": "20260201",
                          "fetch": {"url": "x", "sha256": "y"}}}
    q = NS(question_id="q1", claim_date=None, market_implied=None)
    attach_from_derived([q], {"q1": d})
    assert q.market_implied is None, (
        "a benchmark trade-dated AFTER the event attached to a question with "
        "no claim_date — strict_dates did not fail closed on missing input")


# ===========================================================================
# MEDIUM R11-5 — federalregister._rename() never sees the real response
# shapes, so the published_at→publication_date contract repair is dead code.
# Branch fix/broken-sources (merged via batch3): FR /documents.json returns
# {"results":[...]} (its own health probe asserts this!) yet _rename walks
# payload["documents"]; /documents/{id}.json returns a bare dict, also
# untouched. Family #7: zero tests exercise the adapter class itself.
# ===========================================================================

class _FakeFRSource:
    def build_url(self, *a, **k):
        return "https://www.federalregister.gov/api/v1/documents.json"

    def get_json(self, url):
        rec = NS(url=url, content_sha256="h" * 64, fetched_at="now")
        return ({"results": [{"title": "t",
                              "publication_date": "2026-08-01"}]}, rec)


def test_r11_06_search_preserves_published_at_contract():
    from tools.sources.federalregister import FederalRegisterAdapter
    ad = FederalRegisterAdapter(_FakeFRSource())
    doc = ad.search(query_term="climate")["results"][0]
    assert "published_at" in doc, (
        f"adapter contract broken downstream of the rename: keys={sorted(doc)} — "
        "_rename() only inspects payload['documents'], which /documents.json "
        "never returns")


# ===========================================================================
# MEDIUM R11-7 — cross-run memory keyed by task_classifier buckets, which
# put every research question into 'default'. The finding's own worked
# example ("semiconductor supply chain resilience") shares a bucket with
# sports betting and price checks; chronic-null lessons learned about
# openalex for macro questions reorder scholarly fan-out for everything.
# Family #4-adjacent: the class label stands in for topical similarity.
# tools/pipeline/crossrun.py::question_class_for
# ===========================================================================

def test_r11_07_research_questions_do_not_all_share_one_bucket():
    from tools.pipeline.crossrun import question_class_for
    buckets = {
        question_class_for("what does recent scholarly research say about "
                           "semiconductor supply chain resilience?"),
        question_class_for("Is Bitcoin a good buy right now?"),
        question_class_for("Will the Bills win the Super Bowl?"),
    }
    assert len(buckets) >= 2, (
        f"different research domains share one memory bucket ({buckets}); "
        "cross-run memory cannot distinguish them, contradicting the "
        "'per CLASS, never per question' isolation claim")


# ===========================================================================
# HIGH R11-8 (pre-existing, re-pinned on current master bytes) — kelly_full
# rounds UP: round(fraction, 6) raises the stake an automated actor takes.
# Family #6. tools/kelly.py::kelly_full — sweep shows ~486,921 violating
# parameter cells (tests/test_redteam_money_path.py::test_m2_*).
# ===========================================================================

def test_r11_08_kelly_full_never_rounds_the_stake_up():
    from tools.kelly import kelly_full
    edge, odds = 0.0055, 101
    implied = 100.0 / 201.0
    p = implied + edge
    b = (101.0 / 100.0) - 1.0
    exact = max(0.0, (b * p - (1 - p)) / b)
    got = kelly_full(edge, odds)
    assert got <= exact + 1e-12, (
        f"kelly_full returned {got} > exact {exact}: an automated actor "
        "raising its own stake via round-half-up")


# ===========================================================================
# HIGH R11-9 (re-pin) — tools.calibration package un-importable on master.
# __init__ imports replay_chain; instrument.py has never defined it (it has
# replay_leaf_chain/replay_parent_chain). Family #2 at branch granularity +
# family #1 (the instrumented-run verifier nobody can construct).
# First pinned on review/ox-alpha-0823 (de176bd); that pin was never merged.
# ===========================================================================

def test_r11_10_calibration_package_imports():
    import importlib
    try:
        importlib.import_module("tools.calibration")
    except ImportError as e:
        pytest.fail(f"tools.calibration un-importable on master: {e}")


# ===========================================================================
# MEDIUM R11-11 (re-pin) — relevance-gate prefix hole: 15 characters of
# three-letter prefixes score 80% coverage and get admitted over the 25%
# default threshold. tools/pipeline/retrieval.py RelevanceGate.judge.
# ===========================================================================

def test_r11_12_prefix_junk_cannot_reach_admission_coverage():
    from tools.pipeline.retrieval import RelevanceGate, _tokens
    QUESTION = ("will semiconductor supply chain resilience improve foundry "
                "concentration")
    toks = sorted(_tokens(QUESTION))
    junk = {"abstract": " ".join(t[:3] for t in toks)}
    g = RelevanceGate()
    ok, cov, reason = g.judge(QUESTION, "empirical", junk)
    assert cov < 0.25 or not ok, (
        f"prefix-matched junk scored {cov:.0%} and was admitted: {reason}")
