"""Mutation harness for Callisto.

Applies hand-curated mutations to COPIES of target modules (production source
is never modified), runs a fast subset of the suite against each mutant, and
records which mutants SURVIVE — i.e. defects the tests do not catch.

Usage:
    python3 scripts/mutation/run_mutations.py [--module agp/thresholds.py]
        [--fast] [--json OUT]

A mutation is (name, old_snippet, new_snippet). Each must occur exactly once
in the module; the harness refuses ambiguous or missing anchors.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Fast, high-signal test selection per module. These are the suites that
# directly exercise the module plus the cross-cutting seal/lifecycle suites.
TESTS_BY_MODULE = {
    "agp/thresholds.py": [
        "tests/test_agp.py", "tests/test_lifecycle_seal.py",
        "tests/test_tier0_money_kelly.py", "tests/test_build_b4_inheritance.py",
    ],
    "agp/provenance.py": [
        "tests/test_tier3_epi_provenance.py", "tests/test_lifecycle_seal.py",
        "tests/test_build_w1_retrieval.py", "tests/test_build_p2_claims.py",
    ],
    "agp/adversary.py": [
        "tests/test_build_r3_adversary.py", "tests/test_build_gaps.py",
        "tests/test_build_w4_cross_model.py", "tests/test_lifecycle_seal.py",
    ],
    "agp/ensemble.py": [
        "tests/test_build_w4_cross_model.py", "tests/test_build_r3_adversary.py",
    ],
    "tools/research_program.py": [
        "tests/test_build_b4_inheritance.py", "tests/test_build_b4_research_program.py",
        "tests/test_build_r1_scoring.py", "tests/test_build_p2_claims.py",
    ],
    "tools/pipeline/synthesis.py": [
        "tests/test_build_i3_synthesis.py", "tests/test_build_r2_seams.py",
    ],
    "tools/pipeline/retrieval.py": [
        "tests/test_build_w1_retrieval.py", "tests/test_build_i3_synthesis.py",
        "tests/test_build_i2_routable_coverage.py",
    ],
    "tools/edge.py": [
        "tests/test_build_r5_edge.py", "tests/test_tier0_money_kelly.py",
        "tests/test_clv_paper_trades.py", "tests/test_no_lookahead_regression.py",
    ],
    "tools/kelly.py": [
        "tests/test_tier0_money_kelly.py", "tests/test_tier0_money_sizing_and_units.py",
        "tests/test_portfolio_sizing.py", "tests/test_bankroll_sim.py",
        "tests/test_regime_sizing.py",
    ],
    "tools/hypothesis.py": [
        "tests/test_hypothesis.py", "tests/test_promotion_gates.py",
        "tests/test_adaptive_timeout.py", "tests/test_sidak_denominator.py",
        "tests/test_build_b1_base_rates.py",
    ],
}


def run_tests(cwd: Path, test_files: list[str], timeout: int = 600) -> tuple[bool, str]:
    """True = all selected tests pass."""
    cmd = [sys.executable, "-m", "pytest", "-x", "-q", "--no-header",
           "-p", "no:cacheprovider"] + test_files
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    out = (proc.stdout + proc.stderr)[-2000:]
    return proc.returncode == 0, out


def apply_mutation(src: str, old: str, new: str) -> str:
    if src.count(old) != 1:
        raise ValueError(f"anchor occurs {src.count(old)} times (need exactly 1): {old!r}")
    return src.replace(old, new)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", action="append", default=None)
    ap.add_argument("--fast", action="store_true",
                    help="skip baseline re-run per module")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    from mutation_catalog import MUTATIONS  # noqa: E402

    modules = args.module or sorted(MUTATIONS.keys())
    results: dict = {}
    overall_fail = False

    with tempfile.TemporaryDirectory(prefix="mut_") as tmp:
        tmp_repo = Path(tmp) / "repo"
        print(f"copying repo to {tmp_repo} ...", flush=True)
        shutil.copytree(REPO, tmp_repo,
                        ignore=shutil.ignore_patterns(
                            ".git", "__pycache__", ".venv", "*.pyc",
                            ".pytest_cache", "node_modules"))

        for mod in modules:
            muts = MUTATIONS.get(mod)
            if muts is None:
                print(f"unknown module {mod}", file=sys.stderr)
                return 2
            src_path = tmp_repo / mod
            original = src_path.read_text()
            entry = {"module": mod, "baseline_pass": None, "mutations": []}

            if not args.fast:
                ok, out = run_tests(tmp_repo, muts["tests"])
                entry["baseline_pass"] = ok
                if not ok:
                    print(f"[{mod}] BASELINE FAILS — results meaningless")
                    print(out)
                    overall_fail = True
                    continue

            for m in muts["mutations"]:
                mutated = apply_mutation(original, m["old"], m["new"])
                src_path.write_text(mutated)
                t0 = time.time()
                killed_ok, tail = run_tests(tmp_repo, muts["tests"])
                dt = time.time() - t0
                survived = killed_ok  # tests still passing == mutation survived
                rec = {"name": m["name"], "survived": survived,
                       "seconds": round(dt, 1)}
                entry["mutations"].append(rec)
                tag = "SURVIVED" if survived else "killed"
                print(f"[{mod}] {m['name']}: {tag} ({dt:.0f}s)", flush=True)
                if survived:
                    # restore before next mutant
                    src_path.write_text(original)

            src_path.write_text(original)
            results[mod] = entry

    killed = sum(1 for e in results.values() for r in e["mutations"]
                 if not r["survived"])
    total = sum(len(e["mutations"]) for e in results.values())
    print(f"\nTOTAL: {killed}/{total} killed "
          f"({(killed / total * 100 if total else 0):.0f}%)")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2))
    return 1 if overall_fail else 0


if __name__ == "__main__":
    sys.exit(main())
