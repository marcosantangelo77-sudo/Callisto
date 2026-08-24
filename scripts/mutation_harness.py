#!/usr/bin/env python3
"""Hand-rolled mutation testing harness for Callisto red-team pass.

Applies one operator mutation at a time to a target file, runs a targeted
pytest subset, records SURVIVED/KILLED, and always restores the original.

Usage: python3 scripts/mutation_harness.py
Writes results JSON to findings/mutation_results.json and prints a table.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# target file -> (pytest test files, list of (regex, replacement, description))
# regex with one capture group: the line must match; replacement uses \1.
TARGETS = {
    "agp/thresholds.py": {
        "tests": ["tests/test_redteam_confidence_laundering.py",
                  "tests/test_confidence.py",
                  "tests/test_fix_ckpt_confidence.py",
                  "tests/test_tier3_epi_provenance.py",
                  "tests/test_agp.py"],
        # Baseline-broken on redteam/mutation-testing @96e09c9 (stale tests
        # assert round-UP laundering that floor_conf deliberately removed).
        "deselect": [
            "tests/test_redteam_confidence_laundering.py::TestCitationLaundering::test_synthesis_best_class_laundering_in_group",
            "tests/test_redteam_confidence_laundering.py::TestSelfReviewEscape::test_panel_verdict_blocking_veto_returns_rounded_up_score",
        ],
    },
    "agp/provenance.py": {
        "tests": ["tests/test_tier3_epi_provenance.py",
                  "tests/test_redteam_confidence_laundering.py"],
        "deselect": [
            "tests/test_redteam_confidence_laundering.py::TestCitationLaundering::test_synthesis_best_class_laundering_in_group",
            "tests/test_redteam_confidence_laundering.py::TestSelfReviewEscape::test_panel_verdict_blocking_veto_returns_rounded_up_score",
        ],
    },
    "agp/adversary.py": {
        "tests": ["tests/test_build_r3_adversary.py",
                  "tests/test_agp_seal.py"],
    },
    "tools/pipeline/retrieval.py": {
        "tests": ["tests/test_build_w1_retrieval.py",
                  "tests/test_redteam_retrieval_relevance.py",
                  "tests/test_hypgen_retrieval.py"],
    },
    "tools/kelly.py": {
        "tests": ["tests/test_tier0_money_kelly.py",
                  "tests/test_redteam_money_path.py",
                  "tests/test_tier0_money_sizing_and_units.py",
                  "tests/test_bankroll_sim.py"],
    },
    "tools/edge_confidence.py": {
        "tests": ["tests/test_edge_confidence.py",
                  "tests/test_build_r5_edge.py"],
    },
}

# Mutation operators. Each: (id, pattern, repl, description).
# Applied per-line; only lines matching are mutated, one occurrence at a time.
OPERATORS = [
    ("MIN2MAX", r"\bmin\(", "max(", "min( -> max("),
    ("MAX2MIN", r"\bmax\(", "min(", "max( -> min("),
    ("LE2LT", r"<=", "<", "<= -> <"),
    ("GE2GT", r">=", ">", ">= -> >"),
    ("LT2LE", r"(?<![<>])<(?!=)", "<=", "< -> <="),
    ("GT2GE", r"(?<![<>])>(?!=)", ">=", "> -> >="),
    ("EQ2NE", r"(?<!=)==(?!=)", "!=", "== -> !="),
    ("FLOOR2ROUND", r"math\.floor\(", "round(", "floor -> round"),
    ("ADD2SUB", r"(?<![-+*/])\+(?![+=])", "-", "+ -> -"),
    ("SUB2ADD", r"(?<![-])-(?![->])", "+", "- -> +"),
    ("MUL2DIV", r"\*(?![*=])", "/", "* -> /"),
    ("DIV2MUL", r"/(?![/*=])", "*", "/ -> *"),
    ("AND2OR", r"\band\b", "or", "and -> or"),
    ("OR2AND", r"\bor\b", "and", "or -> and"),
    ("TRUE2FALSE", r"\bTrue\b", "False", "True -> False"),
]


def gen_mutants(src: str):
    """Yield (mutant_id, mutated_source)."""
    lines = src.split("\n")
    mid = 0
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        for op_name, pat, repl, desc in OPERATORS:
            for m in re.finditer(pat, line):
                # skip occurrences inside string literals (crude: skip lines with docstring-ish quotes containing the token)
                new = line[:m.start()] + repl + line[m.end():]
                mutated = list(lines)
                mutated[lineno - 1] = new
                mid += 1
                yield f"{op_name}-L{lineno}-{m.start()}", "\n".join(mutated), f"{desc} @ line {lineno}: {stripped[:90]}"
                break  # one mutant per operator per line
    _ = mid


def run_tests(tests, timeout=240):
    cmd = [sys.executable, "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:cacheprovider"] + tests
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout + p.stderr)[-400:]
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"


def run_tests_for(rel, cfg):
    tests = list(cfg["tests"])
    for d in cfg.get("deselect", []):
        tests += ["--deselect", d]
    return run_tests(tests)


def main():
    only = sys.argv[1:] or list(TARGETS)
    results = []
    for rel in only:
        cfg = TARGETS[rel]
        path = ROOT / rel
        orig = path.read_text()
        backup = path.with_suffix(".orig_mutbak")
        shutil.copy(path, backup)
        try:
            n = 0
            for mut_id, mutated_src, desc in gen_mutants(orig):
                n += 1
                path.write_text(mutated_src)
                t0 = time.time()
                ok, out = run_tests_for(rel, cfg)
                results.append({
                    "file": rel, "id": mut_id, "mutation": desc,
                    "status": "SURVIVED" if ok else "killed",
                    "secs": round(time.time() - t0, 1), "tail": out if ok else "",
                })
                status = results[-1]["status"]
                print(f"[{rel}] {mut_id} {status} ({results[-1]['secs']}s) {desc}", flush=True)
            print(f"== {rel}: {n} mutants ==")
        finally:
            shutil.copy(backup, path)
            backup.unlink()
            assert path.read_text() == orig, f"RESTORE FAILED for {rel}"
            print(f"restored {rel} cleanly")
    (ROOT / "findings" / "mutation_results.json").write_text(json.dumps(results, indent=1))
    surv = [r for r in results if r["status"] == "SURVIVED"]
    print(f"\nTOTAL {len(results)}  SURVIVED {len(surv)}  killed {len(results)-len(surv)}")


if __name__ == "__main__":
    main()
