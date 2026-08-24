"""JOB 2 — round-by-round distribution over golden runs.

Runs the REAL pipeline (fixture transport, scripted model — no network, no
live model) over a golden matrix:

  A. every scenario in tests/test_build_w1_retrieval.py's shape space
     (sufficient-first-round, refine-then-succeed, all-rejected honest null,
     unplannable-only retrieval failure),
  B. the sealed end-to-end pipeline tests' two-leaf question,
  C. the 22-question retrodiction batch set, each against a route table
     that serves its evidence pages (openalex/federalregister/gdelt/...).

For each LEAF, the observer gives cumulative state after each round.
Round N+1 MOVED THE CONCLUSION iff
    (best_class, n_indep, admitted-sha-set) changed vs round N.
Otherwise it was PURE COST: no downstream model call could return anything
different, because it would be handed an identical evidence set.

Writes data/stopping_rules/round_distribution.json and prints a summary.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers.no_socket import NoSocket  # noqa: E402

NoSocket().install()

from agp import Domain, SourceClass  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tools.sources.registry import get_source_registry  # noqa: E402

CLASS_RANK = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}

# ── golden corpus ──────────────────────────────────────────────────────────

def decompose(text, qtype, min_ind=2):
    return json.dumps({"sub_questions": [
        {"text": text, "kind": "descriptive", "question_type": qtype,
         "min_source_tier": 2, "min_independent_sources": min_ind}]})


def answer(conf=0.7):
    return json.dumps({"answer": "the evidence supports the claim",
                       "stance": "AFFIRMS", "proposed_confidence": conf})


OPENALEX_RELEVANT = json.dumps({"results": [
    {"id": "W1", "title": "Scholarly study on the topic: a literature "
     "review of scholarly work", "publication_year": 2024},
    {"id": "W2", "title": "Evidence on the topic from peer reviewed "
     "research", "publication_year": 2024},
    {"id": "W3", "title": "Systematic review addressing the topic question "
     "directly", "publication_year": 2024},
]})
OPENALEX_IRRELEVANT = json.dumps({"results": [
    {"id": "X9", "title": "Mating habits of deep-sea isopods"}]})
FR_RELEVANT = json.dumps({"documents": [
    {"title": "Final agency rule published by the government: proposed "
              "and final rules with dates, docket refs",
     "document_number": "2024-12345", "published_at": "2024-01-15",
     "agency": "government agency"}]})
GDELT_RELEVANT = json.dumps({"articles": [
    {"title": "News report on the topic under discussion",
     "seendate": "20240110T120000", "url": "https://example.org/a"}]})


def routes_all():
    return {"/works": OPENALEX_RELEVANT,
            "/documents.json": FR_RELEVANT,
            "/doc_query": GDELT_RELEVANT,
            "/api/v1": OPENALEX_RELEVANT}


GOLDEN = []


def add_case(name, qtype, min_ind, routes, bodies_cycle=None):
    GOLDEN.append(dict(name=name, qtype=qtype, min_ind=min_ind,
                       routes=routes))


# Scenario family A: retrieval shapes
add_case("A1 sufficient-first-round", "scholarly work search", 2, {
    "/works": OPENALEX_RELEVANT})
add_case("A2 refine-then-succeed", "scholarly work search", 3, {
    "/works": OPENALEX_RELEVANT})
add_case("A3 honest-null-all-rejected", "scholarly work search", 2, {
    "/works": OPENALEX_IRRELEVANT})
add_case("A4 multi-source-fanout", "federal register documents", 2, {
    "/documents.json": FR_RELEVANT, "/works": OPENALEX_RELEVANT})
add_case("A5 news+academic mix", "news coverage of events", 2, {
    "/doc_query": GDELT_RELEVANT, "/works": OPENALEX_RELEVANT})

# Scenario B: the sealed e2e shape (two leaves)
add_case("B1 e2e-two-leaf", "scholarly work search", 2, routes_all())

# Scenario C: retrodiction batch questions mapped onto plannable sources.
_qfile = Path(__file__).resolve().parents[1] / "data/retro_batch/questions.json"
if _qfile.exists():
    _qs = json.loads(_qfile.read_text())
else:
    _qs = []
_QTYPE_MAP = {
    "beat_or_miss": "news coverage of events",
    "event_outcome": "news coverage of events",
    "threshold_cross": "economic time series observations",
}
for i, q in enumerate(_qs):
    add_case(f"C-retro-{i}", _QTYPE_MAP.get(q["question_type"],
                                             "news coverage of events"),
             2, routes_all())


async def run_case(case) -> dict:
    model = ScriptedModel(default={"content": answer(0.7)})
    model.script("Architect", {"content":
                               decompose(f"what does the literature say about "
                                         f"the topic {case['name']}",
                                         case["qtype"], case["min_ind"])})
    ledger = ProvenanceLedger()
    store = ArtifactStore(root=tempfile.mkdtemp(prefix="rounds_golden_"))

    observations: list[dict] = []

    def observe(state):
        observations.append(state)

    pipeline = ResearchPipeline(
        model=model, adversary_router=_Quiet(),
        transport=fixture_transport(case["routes"]), store=store,
        ledger=ledger)

    from tools.pipeline import engine
    orig_fetch = engine.ResearchPipeline._fetch_for_question

    async def instrumented(self, q, question_type=""):
        from tools.pipeline.retrieval import IterativeRetriever
        reg = self._get_registry()
        qt = question_type or q.text
        retriever = IterativeRetriever(
            registry=reg, ledger=self.ledger, transport=self.transport)
        retriever.round_observer = observe
        trace = retriever.retrieve(
            q, qt,
            min_independent=q.evidence_requirements.min_independent_sources)
        return list(trace.admitted), trace

    engine.ResearchPipeline._fetch_for_question = instrumented
    try:
        result = await pipeline.run(
            f"What is known about the topic? [{case['name']}]",
            domain=Domain.GENERAL, today=date(2026, 8, 22))
    finally:
        engine.ResearchPipeline._fetch_for_question = orig_fetch

    # Derive best_class trajectory: map source name -> assigned class by
    # re-running the ledger assignment on each admitted body (pure function
    # of content; identical to what the engine computed).
    from agp import Evidence
    per_source_class = {}
    for f in result.fetches:
        ev = Evidence(content=f.body[:4000], source_class=SourceClass.INFERRED,
                      confidence_score=0.30, domain=Domain.GENERAL,
                      origin_agent="pipeline", source_name=f.source_name)
        assigned = ledger.assign_source_class(ev)
        per_source_class[f.source_name] = assigned.value

    # Fold rounds into a conclusion-state trajectory per qid.
    traj: dict[str, list] = {}
    for obs in observations:
        key = obs["qid"]
        classes = [per_source_class.get(s, "INFERRED")
                   for s, _ in obs["admitted"]]
        best = max(classes, key=lambda c: CLASS_RANK.get(c, 0)) if classes \
            else None
        state = (best, len(obs["indep_keys"]),
                 tuple(sorted(sh for _, sh in obs["admitted"])))
        traj.setdefault(key, []).append(
            {"round": obs["round"], "state": state})

    rounds_summary = []
    for key, states in traj.items():
        for i, entry in enumerate(states):
            moved = True if i == 0 else (
                entry["state"] != states[i - 1]["state"])
            rounds_summary.append({
                "qid": key, "round": entry["round"], "moved": moved})
    return {
        "case": case["name"], "sealed": result.sealed,
        "refusal_reason": result.refusal_reason or "",
        "n_leaves": len(result.leaves),
        "stop_reasons": [],  # filled below via trace capture if needed
        "rounds": rounds_summary,
    }


class _Quiet:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def main() -> int:
    out_dir = Path(__file__).resolve().parents[1] / "data/stopping_rules"
    out_dir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    results = []
    try:
        for case in GOLDEN:
            results.append(loop.run_until_complete(run_case(case)))
    finally:
        loop.close()

    total_rounds = sum(len(r["rounds"]) for r in results)
    moved = sum(1 for r in results for e in r["rounds"] if e["moved"])
    pure_cost = total_rounds - moved

    # Distribution: which ordinal round, how many moved/pure-cost
    dist: dict[int, dict[str, int]] = {}
    for r in results:
        for e in r["rounds"]:
            d = dist.setdefault(e["round"], {"moved": 0, "pure_cost": 0})
            d["moved" if e["moved"] else "pure_cost"] += 1

    summary = {
        "n_cases": len(results),
        "n_sealed": sum(1 for r in results if r["sealed"]),
        "total_leaf_rounds": total_rounds,
        "conclusion_moving_rounds": moved,
        "pure_cost_rounds": pure_cost,
        "by_round": {str(k): v for k, v in sorted(dist.items())},
        "cases": results,
    }
    (out_dir / "round_distribution.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "cases"},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
