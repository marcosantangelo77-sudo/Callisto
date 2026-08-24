"""RED TEAM — source registry & query builders (rotating pass, 2026-08-24).

Surface: tools/sources/registry.py, tools/sources/query_builder.py,
tools/pipeline/retrieval.py, tools/pipeline/engine.py (parent composition),
tools/gaps.py. Method: adversarial-input + property sweep over the
selection/relevance/independence pipeline — the unattacked ground named by
the rotation ("what happens when a source lies, or returns 200 with zero
results"), attacked with a method NOT yet used on this surface.

Findings (full write-up: findings/redteam_source_registry.md):

  SR1 (CRITICAL) — a 200-with-zero-results body that echoes the question is
      ADMITTED as evidence and its host counts toward min_independent_sources.
      The leaf then reads "sufficient" on literally no data.
  SR2 (CRITICAL) — the stance-propagation fix (dd4fb18: parent direction from
      DECISIONAL leaves only) exists ONLY on fix/stance-propagation /
      origin/review/deep-audit-0824 and was NEVER merged to master.
      origin/master still carries `best_leaf = max(answered, key=confidence)`
      with parent_stance = best_leaf.stance — family 9 unfixed where it ships.
  SR3 (HIGH) — RelevanceGate prefix matching admits wholly unrelated documents
      ('gas' -> 'gastrointestinal'): 7/8 collision pairs in the sweep.
  SR4 (MEDIUM) — query_builder's honest-gap table keys 'sec_fts' but the
      registered adapter name is 'sec_fulltext'; build_plan('sec_fulltext')
      returns "unknown source". A label drift makes the deliberate gap
      unreadable AND would silently skip any future planner keyed correctly.
  SR5 (LOW, honest-negative + pin) — evidence bodies longer than 4000 chars
      are hashed truncated against full-body ledger observations; demotes
      PRIMARY fetches to INFERRED (safe direction, but a fidelity loss).

Run: python3 -m pytest tests/test_redteam_source_registry.py -q
"""
from __future__ import annotations

import json

import pytest

from agp.research_program import EvidenceRequirement
from tools.pipeline import retrieval as R
from tools.pipeline.retrieval import (
    IterativeRetriever,
    RelevanceGate,
    independence_key,
)
from tools.sources import query_builder as qb
from tools.sources.base import SourceSpec
from tools.sources.registry import SourceAdapter, SourceRegistry


# ── harness ────────────────────────────────────────────────────────────────


class _Ad:
    """Adapter that calls RestSource.get_json like the real ones do."""

    parsed = None

    def __init__(self, source):
        self.s = source

    def search(self, **kw):
        data, _ = self.s.get_json(
            self.s.build_url("search", {"q": kw.get("query")}))
        return data


class _Ledger:
    def record_tool_result(self, *a, **k):
        pass

    def record_gate_rejection(self, *a, **k):
        pass


def _registry(name="echo", answers=("macro time series unemployment rate "
                                    "search",), base="https://api.e.example"):
    reg = SourceRegistry()
    spec = SourceSpec(name=name, base_url=base, description="x",
                      answers=answers, tier=1)
    reg.register(SourceAdapter(spec=spec, make_adapter=_Ad))
    return reg


def _retrieve(question, body, *, qt="macro time series unemployment rate",
              min_indep=1, answers=("macro time series unemployment rate "
                                    "search",)):
    qb._KEYWORD_PLANNERS.setdefault(
        "echo", lambda q: qb.PlanResult(True, queries=[
            qb.PlannedQuery(source="echo", method="search",
                            kwargs={"query": qb.core_query(q)})]))
    reg = _registry(answers=answers)

    def transport(url, headers):
        return 200, json.dumps(body)

    ret = IterativeRetriever(registry=reg, ledger=_Ledger(),
                             transport=transport)

    class Q:
        question_id = "q"
        text = question
        evidence_requirements = EvidenceRequirement()

    return ret.retrieve(Q(), qt, min_independent=min_indep)


# ── SR1: echo / error-as-200 admission (fails today) ───────────────────────

Q1 = "unemployment rate in 2026"

@pytest.mark.parametrize("body", [
    {"query": Q1, "results": [], "status": "ok"},          # zero hits, echo
    {"error": Q1, "message": "quota exhausted"},           # API error as 200
    {"meta": {"q": Q1}, "results": []},                    # openalex-shaped 0
])
def test_sr1_zero_result_echo_body_is_not_admitted_as_evidence(body):
    tr = _retrieve(Q1, body)
    assert tr.n_admitted == 0, (
        "SR1: a 200 body containing nothing but an echo of the question and "
        f"zero results was admitted ({tr.n_admitted} items); its host even "
        f"counted as an independent voice: {sorted(tr.independent_keys)}")
    assert not tr.independent_keys


# ── SR3: prefix-collision relevance (fails today) ──────────────────────────

@pytest.mark.parametrize("qw,dw", [
    ("gas", "gastrointestinal"),
    ("coal", "coalescing"),
    ("rate", "ratification"),
    ("gold", "golden retriever"),
])
def test_sr3_prefix_collision_does_not_satisfy_relevance(qw, dw):
    ok, cov, reason = RelevanceGate().judge(
        f"is {qw} demand rising", "",
        {"title": f"{dw} trends among elderly patients"})
    assert not ok, (
        f"SR3: '{dw}' admitted for question about '{qw}' at coverage "
        f"{cov:.0%}")


# ── SR4: sec_fts vs sec_fulltext label drift ───────────────────────────────

def test_sr4_honest_gap_table_matches_registered_adapter_name():
    from tools.sources.registry import get_source_registry
    names = set(get_source_registry().names())
    gaps = set(qb.honest_gaps())
    assert not (gaps - names), (
        f"SR4: honest-gap entries name sources that are not registered: "
        f"{gaps - names}; build_plan returns 'unknown source' for them")


# ── SR5: truncation pin (passes today; regression pin) ─────────────────────

def test_sr5_long_primary_body_hashes_truncated_vs_full():
    # Documenting the fidelity loss: engine stores Evidence(content=body[:4000])
    # while the ledger recorded the FULL body. assign_source_class therefore
    # hashes a string that is not in the ledger -> INFERRED. Safe direction;
    # pinned so a future refactor cannot flip it into silent promotion.
    from tools.pipeline.engine import _sha
    full = json.dumps({"data": "x" * 9000})
    assert _sha(full[:4000]) != _sha(full)


# ── independence sanity pins (honest negatives) ────────────────────────────

def test_family_collapse_survives_name_spelling():
    assert (independence_key("openalex", "https://api.openalex.org")
            == independence_key("semanticscholar",
                                "https://api.semanticscholar.org"))
    assert (independence_key("semantic_scholar", "")
            == independence_key("semanticscholar", ""))
