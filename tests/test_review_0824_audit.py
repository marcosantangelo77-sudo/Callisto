"""Review 2026-08-24 (second run) — audit of the AUDIT claims.

Every test here reproduces a defect found while auditing what ~60 agent
instances committed over Aug 21–24. Each is written to FAIL on the current
checkout for the documented reason (the defect is live); when a fix lands it
should flip to passing and be re-homed as a fix-pin.

Covers:
  A1  memory_epistemics.clamp_to_ceiling still rounds UP across the tier
      boundary on master — the fix claimed in findings/improve_memory_wiki.md
      (commit 4352ad1) exists only on review/rotating-0823-155500, never merged.
  A2  hermes_memory.record_learning still drops source_class/provenance_seal
      (same stranded commit) — every learning degrades to anonymous INFERRED.
  A3  knowledge_wiki compile admits learnings at confidence >= 0.5 with NO
      provenance gate — the write side AND the read side of the trust
      escalator fix are both absent from master.
  A4  tools.calibration un-importable: __init__ imports replay_chain (never
      defined) and bridge.py (not shipped on master).
  A5  kelly_full rounds the stake UP (0.010946 vs exact 0.0055/1.01 =
      0.005455): round(,6) can double the true fraction at tiny edges.
  A6  kelly_portfolio treats perfectly-correlated duplicates as
      diversification: two rho=1.0 duplicates total 1.414x one bet (M3a,
      open despite d638260 fixing only the cap-binding half).
  A7  clv_points accepts a crossed book on the claim side and mints signed
      CLV from book corruption alone (M6).
  A8  record_closing_line matches bets with no placement_point guard (F10):
      +3.5 and -2.0 bets both stamped with a single 7.5 close.
  A9  prereg score() defaults to AMENDED criteria after any amend().
  A10 cmefedfut W5 date guard fails open twice: wrapper forces
      allow_no_provenance=True; missing claim_date attaches post-dated
      benchmarks.
  A11 federalregister._rename() is dead code: walks payload["documents"] but
      /documents.json returns {"results": [...]}.
  A12 crossrun question_class_for collapses ALL research questions to
      'default'.
  A13 RelevanceGate admits pure junk built from 3-letter prefixes of the
      question's tokens.
  A14 agp.ensemble._same_weights reads two spellings of the same model family
      as independent reviewers ("Claude Sonnet 4" vs "claude-sonnet-4").
  A15 Empty adversary panel approves: parsed {"objections": []} or an
      all-junk panel yields zero objections and the seal proceeds (F6c).
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── A1: clamp rounds up across the boundary (stranded fix) ────────────────

def test_a01_clamp_to_ceiling_never_raises_across_boundary():
    from tools.memory_epistemics import clamp_to_ceiling
    # INFERRED ceiling is 0.55; 0.5497 must NOT be rounded up to 0.55.
    assert clamp_to_ceiling(0.5497, "INFERRED") <= 0.5497


# ── A2/A3: provenance columns never written; wiki read gate absent ─────────

def test_a02_record_learning_persists_provenance(tmp_path):
    import asyncio
    import inspect
    from tools import hermes_memory as H
    src = inspect.getsource(H)
    # The INSERT that persists a learning must carry the provenance columns
    # migration 015 adds, otherwise admit_learning's decision is discarded.
    inserts = [l for l in src.splitlines() if "INSERT INTO hermes_learnings" in l]
    assert inserts, "record_learning must insert"
    assert any("source_class" in l or "source_class" in src[max(0, src.find(l) - 200):src.find(l) + 400]
               for l in inserts), (
        "hermes_learnings INSERT omits source_class/provenance_seal: "
        "admit_learning's decision is computed then thrown away")


def test_a03_wiki_compile_gates_learnings_on_provenance():
    src = (Path("tools/knowledge_wiki.py")).read_text()
    # The learnings SELECT must gate on a provenance/class column, not just
    # confidence >= 0.5. (The session path has its own seal check; the
    # learning path on master has nothing.)
    sel_i = src.find("FROM hermes_learnings")
    assert sel_i != -1, "wiki compile must read hermes_learnings"
    select_block = src[max(0, sel_i - 300):sel_i + 600]
    assert "source_class" in select_block or "provenance_seal" in select_block, (
        "wiki _get_uncompiled_sources selects learnings at confidence>=0.5 "
        "with no provenance/class gate — the escalator is fully alive at "
        "the compile door")


# ── A4: calibration package un-importable ─────────────────────────────────

def test_a04_calibration_package_imports():
    import importlib
    importlib.reload(importlib.import_module("tools.calibration"))


# ── A5: kelly_full rounds the stake up ─────────────────────────────────────

def test_a05_kelly_full_never_rounds_up():
    from tools.kelly import kelly_full
    exact = 0.0055 / 1.01  # edge/b at +101
    got = kelly_full(edge=0.0055, odds=101)
    assert got <= exact + 1e-9, f"kelly_full returned {got} > exact {exact}"


# ── A6: portfolio kelly duplicates (M3a core) ──────────────────────────────

def test_a06_perfectly_correlated_duplicates_are_one_position():
    from tools.kelly import kelly_portfolio
    bet = {"edge": 0.05, "odds": -110, "correlation_with_others": 1.0}
    one = kelly_portfolio([bet])[0]["final_fraction"]
    two = sum(b["final_fraction"] for b in kelly_portfolio([bet] * 2))
    assert two <= one * 1.001, (
        f"two rho=1.0 duplicates sized {two:.6f} vs {one:.6f} single — "
        "docstring says treat as ONE position, code pays 1.41x for duplicating")


# ── A7: crossed claim book mints CLV (M6) ──────────────────────────────────

def test_a07_clv_points_refuses_crossed_book():
    from tools.edge import MarketQuote, clv_points
    # Claim book sums to 0.75 (crossed/stale); close is healthy 50/50.
    claim = MarketQuote(price=30, counter_price=45, kind="contract_cents",
                        source="stale-crossed")
    close = MarketQuote(price=50, counter_price=50, kind="contract_cents",
                        source="pinnacle")
    assert clv_points(claim, close) is None, (
        "CLV accepted a corrupted crossed claim book and produced signed CLV")


# ── A8: closing line matched without point guard (F10) ────────────────────

def test_a08_closing_line_update_includes_point_guard():
    src = Path("tools/clv_tracker.py").read_text()
    i = src.find("UPDATE bets SET")
    stmt = src[i:i + 500]
    assert "placement_point" in stmt, (
        "record_closing_line UPDATE matches on event/market/team only; "
        "+3.5 and -2.0 bets both absorb whichever close arrives first")


# ── A9: prereg scores against amended criteria by default ──────────────────

def test_a09_default_score_uses_sealed_originals():
    import agp.preregistration as P
    orig = P.Criteria(confirm_markers=["inflation fell"],
                      refute_markers=["inflation rose"],
                      min_evidence_items=3, min_source_class="SECONDARY")
    pr = P.Preregistration(query="Did inflation fall?", criteria=orig)
    pr.seal()
    loosened = P.Criteria(confirm_markers=["anything at all"],
                          refute_markers=["unrelated marker"],
                          min_evidence_items=1, min_source_class="INFERRED")
    pr.amend(loosened, reason="loosen the bar post hoc")
    out = pr.score(observed_text="anything at all", evidence_count=2,
                   best_source_class="INFERRED")
    assert out.verdict != P.Verdict.CONFIRMED or not getattr(
        out, "used_amended", True), (
        "default score() used the amendment, not the sealed originals")


# ── A10: cmefedfut W5 guard fails open ─────────────────────────────────────

class _Q:
    def __init__(self, qid, claim_date=None):
        self.question_id = qid
        self.claim_date = claim_date
        self.market_implied = None


def test_a10a_wrapper_cannot_attach_unprovenanced_benchmark():
    from tools.sources.cmefedfut import attach_market_implied
    q = _Q("q1", claim_date=None)
    attach_market_implied([q], {"q1": 0.9}, strict_dates=True)
    assert q.market_implied is None, (
        "attach_market_implied forced allow_no_provenance=True: an "
        "unprovenanced probability bypasses the W5 date guard entirely")


def test_a10b_missing_claim_date_fails_closed():
    import datetime as dt
    from tools.sources.cmefedfut import attach_from_derived
    q = _Q("q3", claim_date=None)
    skipped = attach_from_derived(
        [q], {"q3": {"probability_of_change": 0.7,
                     "derived_from": {"trade_date": "20261231"}}},
        strict_dates=True)
    assert q.market_implied is None, (
        f"a benchmark trade-dated AFTER anything attached because the "
        f"question had no claim_date; skipped={skipped}")


# ── A11: federalregister._rename is dead code ──────────────────────────────

def test_a11_rename_handles_real_response_shapes():
    from tools.sources.federalregister import FederalRegisterAdapter as FR
    list_shape = FR._rename({"results": [{"publication_date": "2026-08-01"}]})
    renamed_list = "published_at" in list_shape["results"][0]
    bare = FR._rename({"publication_date": "2026-08-01"})
    renamed_bare = "published_at" in bare
    assert renamed_list and renamed_bare, (
        "_rename only walks payload['documents']; the API returns "
        "{'results': [...]} for /documents.json and a bare dict for "
        "/documents/{id}.json — published_at is never restored")


# ── A12: crossrun class bucketing ──────────────────────────────────────────

def test_a12_research_questions_do_not_all_share_one_bucket():
    from tools.pipeline.crossrun import question_class_for
    qs = ["semiconductor supply chain resilience 2026?",
          "Is Bitcoin a good buy?",
          "Will the Bills win the Super Bowl?"]
    buckets = {question_class_for(q) for q in qs}
    assert len(buckets) >= 2, (
        "every research question maps to 'default': cross-run compounding "
        "'which sources were useless for THIS KIND of question' cannot work")


# ── A13: relevance-gate prefix junk ────────────────────────────────────────

def test_a13_prefix_junk_cannot_reach_admission():
    from tools.pipeline.retrieval import RelevanceGate
    g = RelevanceGate()
    q = "Will the Federal Reserve raise rates at the March FOMC meeting"
    toks = [t for t in q.split() if len(t) >= 3]
    junk = " ".join(t[:3] for t in toks)
    ok, cov, _ = g.judge(q, "factual", {"content": junk + " the the"})
    assert not ok, f"junk prefixes admitted at coverage {cov:.0%}"


# ── A14: model identity is spelling (F6b residue) ──────────────────────────

def test_a14_same_weights_family_spelling_variants_are_not_independent():
    from agp.ensemble import ReviewProvenance
    rp = ReviewProvenance(author_model="claude-sonnet-4",
                          reviewer_models=["Claude Sonnet 4"])
    assert not rp.independent, (
        "two spellings of one model family read as independent review; the "
        "self-review ceiling can never engage")


# ── A15: empty adversary panel approves (F6c) ──────────────────────────────

class _EmptyRouter:
    async def complete(self, *a, **k):
        return {"parsed_json": {"objections": []}, "model": "m1"}


class _JunkRouter:
    async def complete(self, *a, **k):
        return {"parsed_json": {"objections": [{"text": "   "}, "junk"]},
                "model": "m1"}


class _Ledger:
    def record_objection(self, ob):
        pass


@pytest.mark.parametrize("router", [_EmptyRouter, _JunkRouter])
def test_a15_empty_or_junk_panel_does_not_approve(router):
    import asyncio
    from agp.adversary import Adversary
    adv = Adversary(router=router(), ledger=_Ledger())
    objections = asyncio.run(adv.attack("c1", "conclusion", ["evidence"]))
    # A panel that said NOTHING (or nothing admissible) must not read as
    # approval — absence of objections is not evidence of soundness.
    assert objections, "empty/junk panel silently approved"
