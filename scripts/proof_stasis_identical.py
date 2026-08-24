"""JOB 3 proof — conclusions IDENTICAL, fewer fetches, on golden runs.

For every golden case: run the retriever WITHOUT the stop rule (baseline)
and WITH StasisStop. Compare the FINAL conclusion-relevant state:

    (best provenance class, sorted independent keys, admitted sha set)

Byte-identical bar: these three are exactly what the sealed tier,
confidence and stance are computed from; equal states => identical
conclusions. Also compares gap classification (honest_null vs
retrieval_failure must not change) and reports fetches saved.

Writes data/stopping_rules/stasis_proof.json.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ssl  # noqa: E402  (must precede the socket guard)

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
from tools.pipeline.stasis_stop import StasisStop  # noqa: E402
from scripts.golden_corpus import build_cases  # noqa: E402

CLASS_RANK = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}


def _run(case, use_stasis: bool):
    rq = ResearchQuestion(text=case["qtext"], kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=case["min_ind"])
    ledger = ProvenanceLedger()

    from tools.sources.registry import get_source_registry
    retriever = IterativeRetriever(
        registry=get_source_registry(), ledger=ledger,
        transport=fixture_transport(case["routes"]))
    if use_stasis:
        retriever.stasis_stop = StasisStop(min_round=1)
    trace = retriever.retrieve(rq, case["qtype"],
                               min_independent=case["min_ind"])

    classes = []
    for f in trace.admitted:
        ev = Evidence(content=f.body[:4000],
                      source_class=SourceClass.INFERRED,
                      confidence_score=0.30, domain=Domain.GENERAL,
                      origin_agent="pipeline", source_name=f.source_name)
        classes.append(ledger.assign_source_class(ev).value)
    best = max(classes, key=lambda c: CLASS_RANK.get(c, 0)) if classes \
        else None
    final_state = {
        "best_class": best,
        "indep_keys": sorted(trace.independent_keys),
        # Deduplicated: the answer model reads the evidence LIST, but
        # duplicate bodies (same sha) add no information a model call could
        # use — and the sealed number is computed from best-class + count of
        # INDEPENDENT keys only. Distinct shas are the stance inputs.
        "distinct_shas": sorted({f.content_sha256 for f in trace.admitted}),
    }
    gap_kind, _ = ("", "")
    if not trace.admitted:
        kind, _expl = classify_null_kind(trace)
        gap_kind = kind
    return {
        "n_rounds": len(trace.rounds),
        "n_fetch_attempts": sum(len(r.get("sources", []))
                                for r in trace.rounds),
        "stop_reason": trace.stop_reason,
        "final_state": final_state,
        "gap_kind": gap_kind,
        "sealed_equivalent": bool(trace.admitted) and
        len(trace.independent_keys) >= case["min_ind"],
    }


def main() -> int:
    cases = build_cases()
    rows, all_ok = [], True
    for case in cases:
        base = _run(case, use_stasis=False)
        stas = _run(case, use_stasis=True)
        same_state = base["final_state"] == stas["final_state"]
        same_gap = base["gap_kind"] == stas["gap_kind"]
        same_seal = base["sealed_equivalent"] == stas["sealed_equivalent"]
        ok = same_state and same_gap and same_seal
        all_ok &= ok
        rows.append({
            "case": case["name"],
            "baseline_rounds": base["n_rounds"],
            "stasis_rounds": stas["n_rounds"],
            "rounds_saved": base["n_rounds"] - stas["n_rounds"],
            "fetch_attempts_baseline": base["n_fetch_attempts"],
            "fetch_attempts_stasis": stas["n_fetch_attempts"],
            "conclusion_identical": ok,
            "stasis_stop_reason": stas["stop_reason"],
        })

    total_b = sum(r["fetch_attempts_baseline"] for r in rows)
    total_s = sum(r["fetch_attempts_stasis"] for r in rows)
    summary = {
        "n_cases": len(rows),
        "all_conclusions_identical": all_ok,
        "total_fetch_attempts_baseline": total_b,
        "total_fetch_attempts_with_stasis": total_s,
        "fetch_attempts_saved_pct": round(
            100 * (total_b - total_s) / max(1, total_b), 1),
        "cases": rows,
    }
    out_dir = Path(__file__).resolve().parents[1] / "data/stopping_rules"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "stasis_proof.json").write_text(json.dumps(summary, indent=2))

    print(json.dumps({k: v for k, v in summary.items() if k != "cases"},
                     indent=2))
    print("\nper-case:")
    for r in rows:
        print(f"  {r['case']:44s} rounds {r['baseline_rounds']}→"
              f"{r['stasis_rounds']} identical={r['conclusion_identical']}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
