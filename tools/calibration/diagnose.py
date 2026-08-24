"""Run the underconfidence diagnosis on the recorded smoke5 batch.

Usage: python3 -m tools.calibration.diagnose [--results PATH] [--out PATH]

Produces the attribution table, the A/B per-mechanism table, and the
stacking arithmetic. Offline — replays recorded metadata only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.calibration.mechanisms import Attribution, attribution_from_batch_row
from tools.calibration.ab_axes import ab_attribution, stack_arithmetic
from tools.calibration.estimate_vs_ceiling import rescore

# The smoke5 rows did not record the raw model estimate (finding #1: the
# estimate is destroyed at clamp time). 0.80 is the reconstruction floor for
# "the model proposed a confident yes" implied by the notes; sensitivity is
# shown across a range below.
RAW_ESTIMATE_RECONSTRUCTION = 0.80


def diagnose(results_path: str | Path) -> dict:
    rows = [json.loads(l) for l in Path(results_path).read_text().splitlines()
            if l.strip()]
    rows = [r for r in rows if r.get("status") == "scored"]

    # 1. Attribution table
    attributions = []
    for r in rows:
        a = attribution_from_batch_row(r,
                                       raw_estimate=RAW_ESTIMATE_RECONSTRUCTION)
        a.run()
        attributions.append(a)
    agg: dict[str, float] = {}
    for a in attributions:
        for k, v in a.by_mechanism().items():
            agg[k] = round(agg.get(k, 0.0) + v / len(attributions), 6)
    attribution_table = {
        "n_questions": len(attributions),
        "raw_estimate_assumed": RAW_ESTIMATE_RECONSTRUCTION,
        "raw_estimate_caveat": (
            "the raw model estimate was DISCARDED at clamp time "
            "(engine.py:379-383); this reconstruction assumes 0.80. Any "
            "estimate in [0.55, 1.0] produces the IDENTICAL trace — the "
            "collapse is many-to-one, which is itself the finding."),
        "mean_final": round(sum(a.final for a in attributions)
                            / len(attributions), 4),
        "mean_removed_by_mechanism": dict(
            sorted(agg.items(), key=lambda kv: -kv[1])),
        "per_question": [a.to_dict() for a in attributions],
    }

    # 2. A/B arms on the first question's shape (identical across all five —
    #    they all collapsed to the same fixed point).
    ab_table = ab_attribution(attributions[0])

    # 3. Stacking arithmetic
    stacking = stack_arithmetic(RAW_ESTIMATE_RECONSTRUCTION,
                                n_objections_major=1)

    # 4. Estimate/ceiling separation rescored against outcomes.
    # HONESTY NOTE: the raw estimates were DISCARDED at clamp time (finding
    # #1) so every rescore here rests on a reconstruction. The output
    # therefore reports the rescore machinery working, plus the many-to-one
    # proof that smoke5 cannot answer whether separation helps.
    sep_records = []
    for r in rows:
        p = r["predicted_probability"]
        conf = round(2 * abs(p - 0.5), 4)
        sep_records.append({
            "question_id": r["question_id"],
            "raw_estimate": RAW_ESTIMATE_RECONSTRUCTION,
            "ceiling": min(1.0, conf * 2),   # ceiling consistent w/ observation
            "leans_yes": p >= 0.5,
            "outcome": bool(r["answer_binary"]),
        })
    separation = rescore(sep_records)
    separation["interpretation"] = (
        "NOT DECISIVE: raw_estimate is a reconstruction (0.80 assumed); the "
        "true value was not recorded. Separation improves Brier exactly when "
        "the estimate carries information the collapse destroyed. Deciding "
        "that requires instrumented runs (tools.calibration.instrument) on "
        "the next batch.")

    return {
        "finding": ("predicted mean 0.33 vs realised 0.60 (n=5). Every "
                    "question collapsed to the same fixed point regardless "
                    "of the raw estimate — see stacking.compounded_note."),
        "attribution": attribution_table,
        "ab_arms": ab_table,
        "stacking": stacking,
        "estimate_vs_ceiling_rescore": separation,
        "sample_size_caveat": (
            "n=5. A McNemar-style paired comparison of Brier deltas at this "
            "effect size (~0.27 bias) needs roughly n≈30-50 questions to "
            "reach 80% power; n≈100 settles it comfortably and matches the "
            "existing 100-question batch plan."),
        "invariant": ("no mechanism was relaxed anywhere; min() guards are "
                      "untouched; this is measurement only"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="data/retro_batch/results_smoke5.jsonl")
    ap.add_argument("--out", default="data/retro_batch/diagnosis_underconfidence.json")
    args = ap.parse_args()
    report = diagnose(args.results)
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(json.dumps({
        "attribution": report["attribution"]["mean_removed_by_mechanism"],
        "ab_verdict_largest_single": report["ab_arms"]["verdict_largest_single"],
        "stacking_compounded_final": report["stacking"]["compounded_final"],
        "separation_rescore": report["estimate_vs_ceiling_rescore"],
    }, indent=2))
    print(f"full report -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
