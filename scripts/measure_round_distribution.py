"""JOB 1+2 — instrumented, then measured: retrieval-round marginal value.

For every golden case (scripts/golden_corpus.py) run the REAL
IterativeRetriever with the round observer attached. After each round the
observer reports the cumulative conclusion-relevant state. A leaf's sealed
(tier, confidence, stance inputs) depends exactly on:

  - best provenance class over admitted fetches,
  - len(independent_keys),
  - the admitted bodies (what the answer model would read).

So round N+1 MOVED THE CONCLUSION iff any of those changed vs round N;
otherwise it was PURE COST — the downstream model call would see an
identical evidence set and cannot return anything new.

Also records stop_reason and whether a null at the stopping point is an
HONEST NULL (searched competently, nothing there) or a RETRIEVAL FAILURE
(we never looked properly) via tools.gaps.classify_null_kind — the two are
different claims and this study must not collapse them.

Writes data/stopping_rules/round_distribution.json.
"""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ssl  # noqa: F401 — must be imported BEFORE the socket guard patches
           # socket.socket (ssl.py subclasses socket.socket at import time).

from tests.helpers.no_socket import NoSocket  # noqa: E402

NoSocket().install()

logging.disable(logging.CRITICAL)

from agp import Domain, Evidence, SourceClass  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.research_program import (  # noqa: E402
    EvidenceRequirement,
    QuestionKind,
    ResearchQuestion,
    SourceClassRank,
)
from tools.gaps import classify_null_kind  # noqa: E402
from tools.pipeline.engine import fixture_transport  # noqa: E402
from tools.pipeline.retrieval import IterativeRetriever  # noqa: E402
from scripts.golden_corpus import build_cases  # noqa: E402

CLASS_RANK = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}


def run_case(case: dict) -> dict:
    rq = ResearchQuestion(text=case["qtext"], kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=case["min_ind"])
    ledger = ProvenanceLedger()
    observations: list[dict] = []

    retriever = IterativeRetriever(
        registry=__import__("tools.sources.registry",
                            fromlist=["get_source_registry"]
                            ).get_source_registry(),
        ledger=ledger, transport=fixture_transport(case["routes"]))
    retriever.round_observer = observations.append
    trace = retriever.retrieve(rq, case["qtype"],
                               min_independent=case["min_ind"])

    # source -> provenance class for every admitted body (pure function of
    # content + ledger; identical to what the engine's _answer_leaf does).
    def body_class(source_name: str, body_sha: str):
        # Reconstruct from trace.admitted (FetchResult has .body).
        for f in trace.admitted:
            if f.source_name == source_name and \
                    f.content_sha256 == body_sha:
                ev = Evidence(content=f.body[:4000],
                              source_class=SourceClass.INFERRED,
                              confidence_score=0.30, domain=Domain.GENERAL,
                              origin_agent="pipeline",
                              source_name=f.source_name)
                return ledger.assign_source_class(ev).value
        return "INFERRED"

    traj = []
    prev_state = None
    moved_flags = []
    for obs in observations:
        classes = [body_class(s, sha) for s, sha in obs["admitted"]]
        best = max(classes, key=lambda c: CLASS_RANK.get(c, 0)) if classes \
            else None
        state = {
            "best_class": best,
            "n_indep": len(obs["indep_keys"]),
            "sha_set": sorted(sha for _, sha in obs["admitted"]),
        }
        comparable = {k: state[k] for k in ("best_class", "n_indep",
                                            "sha_set")}
        moved = True if prev_state is None else (
            comparable != prev_state)
        moved_flags.append(moved)
        prev_state = comparable
        traj.append({
            "round": obs["round"],
            "moved_conclusion": moved,
            "n_admitted": len(obs["admitted"]),
            "n_indep": state["n_indep"],
            "best_class": state["best_class"],
        })

    # Null classification at the point the run actually stopped.
    gap_kind, _gap_expl = ("", "")
    if not trace.admitted:
        from tools.gaps import NullKind
        kind, expl = classify_null_kind(trace)
        gap_kind, _gap_expl = kind, expl

    n_rounds = len(traj)
    pure_cost = sum(1 for m in moved_flags[1:] if not m) if n_rounds > 1 \
        else 0
    return {
        "case": case["name"],
        "stop_reason": trace.stop_reason,
        "gap_kind": gap_kind,
        "sealed_equivalent": bool(trace.admitted) and
        len(trace.independent_keys) >= case["min_ind"],
        "n_rounds": n_rounds,
        "pure_cost_rounds": pure_cost,
        "rounds": traj,
    }


def main() -> int:
    cases = build_cases()
    results = [run_case(c) for c in cases]

    total_rounds = sum(r["n_rounds"] for r in results)
    moving = sum(r["n_rounds"] - r["pure_cost_rounds"] for r in results)
    by_round: dict[int, Counter] = {}
    for r in results:
        for i, t in enumerate(r["rounds"]):
            d = by_round.setdefault(t["round"], Counter())
            d["moving" if t["moved_conclusion"] else "pure_cost"] += 1

    # The candidate rule: stop when a round added no new independent key
    # AND no better class (i.e. did not move the conclusion).
    saved = sum(r["pure_cost_rounds"] for r in results)
    conclusions_same = all(
        # final conclusion state identical whether or not we ran the
        # pure-cost tail: by construction it is, since those rounds changed
        # nothing in the state tuple.
        True for _ in results)

    summary = {
        "n_cases": len(results),
        "total_retrieval_rounds": total_rounds,
        "conclusion_moving_rounds": moving,
        "pure_cost_rounds": saved,
        "pct_pure_cost": round(100 * saved / max(1, total_rounds), 1),
        "by_ordinal_round": {
            str(k): {"moving": v.get("moving", 0),
                     "pure_cost": v.get("pure_cost", 0)}
            for k, v in sorted(by_round.items())},
        "null_split": dict(Counter(
            r["gap_kind"] or "(answered)" for r in results)),
        "cases": results,
    }
    out_dir = Path(__file__).resolve().parents[1] / "data/stopping_rules"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "round_distribution.json").write_text(
        json.dumps(summary, indent=2))

    print(json.dumps({k: v for k, v in summary.items() if k != "cases"},
                     indent=2))
    print("\nper-case:")
    for r in results:
        print(f"  {r['case']:44s} rounds={r['n_rounds']} "
              f"pure-cost={r['pure_cost_rounds']} "
              f"stop='{r['stop_reason'][:48]}' "
              f"gap={r['gap_kind'] or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
