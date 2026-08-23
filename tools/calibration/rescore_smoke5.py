"""Rescore the smoke5 retrodiction batch under estimate/ceiling separation.

The raw model estimates were DISCARDED at clamp time (engine.py:443-466,
finding #1 of the calibration pass), so this is a RECONSTRUCTION rescore:
the attribution trace proves every sealed run collapsed to confidence 0.34
via provenance_ceiling (−0.25) then adversary_penalty (−0.20) regardless of
the raw estimate in [0.55, 1.0] (data/retro_batch/diagnosis_underconfidence.json).
That many-to-one collapse is exactly why separation is testable here: we can
sweep the admissible estimate range and show the collapsed column is flat
while the separated column moves.

Run: python3 -m tools.calibration.rescore_smoke5 [--out PATH]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agp.estimate import EstimateCeiling

SMOKE5 = "data/retro_batch/results_smoke5.jsonl"
# Attribution of every sealed smoke5 row (diagnosis artifact): final 0.34 =
# min(estimate, 0.55) − 0.20 with the requirement cap contributing −0.01.
CEILING_AFTER_PROVENANCE = 0.55
ADVERSARY_PENALTY = 0.21   # 0.55 -> 0.34 observed; carry as ceiling penalty
SWEEP = [0.55, 0.65, 0.75, 0.80, 0.90, 1.00]


def load_rows(path: str) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text().splitlines()
            if l.strip() and json.loads(l).get("status") == "scored"]


def records_for(rows: list[dict], raw_estimate: float) -> list[dict]:
    """Reconstruct instrumented records from collapsed rows.

    For each scored row the collapsed reported probability p maps back to
    conf = 2|p − 0.5| and leans_yes = p >= 0.5. The ceiling carried forward
    is what the mechanism trace says survived everything except the final
    adversary penalty; the estimate is the swept reconstruction value.
    """
    out = []
    for r in rows:
        p = float(r["predicted_probability"])
        conf = round(2 * abs(p - 0.5), 4)
        out.append({
            "question_id": r["question_id"],
            "estimate": raw_estimate,
            # ceiling consistent with the observation: the reported number
            # must be reachable as min(estimate, ceiling); use max(conf*2
            # bounded by the pre-adversary ceiling) so the collapsed column
            # reproduces the observed prediction exactly.
            "ceiling": max(conf, CEILING_AFTER_PROVENANCE - ADVERSARY_PENALTY),
            "leans_yes": p >= 0.5,
            "outcome": bool(r["answer_binary"]),
        })
    return out


def run(path: str = SMOKE5) -> dict:
    rows = load_rows(path)
    sweep = []
    for est in SWEEP:
        res = __import__("agp.estimate", fromlist=["rescore"]).rescore(
            records_for(rows, est))
        sweep.append({"raw_estimate": est, **res})
    # Sensitivity verdict: does separation improve Brier for ANY admissible
    # reconstruction? With all five outcomes True and predictions pinned at
    # 0.33, the answer is determined by arithmetic — report it honestly.
    improvements = [s["brier_improvement"] for s in sweep]
    return {
        "batch": path,
        "n_scored": len(rows),
        "note": ("RECONSTRUCTION: raw estimates were discarded at clamp "
                 "time; estimate values swept over the admissible range "
                 "[0.55, 1.0] established by the mechanism attribution."),
        "collapsed_brier": sweep[0]["collapsed"]["brier"],
        "sweep": sweep,
        "separation_improves_for_any_reconstruction": any(
            i > 0 for i in improvements),
        "invariant": ("no stored confidence modified; sealable() semantics "
                      "unchanged; measurement only"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=SMOKE5)
    ap.add_argument("--out", default="data/retro_batch/rescore_estimate_vs_ceiling.json")
    args = ap.parse_args()
    rep = run(args.results)
    Path(args.out).write_text(json.dumps(rep, indent=2))
    print(json.dumps(rep["sweep"][3], indent=2))   # the 0.80 reconstruction row
    print(f"full rescore -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
